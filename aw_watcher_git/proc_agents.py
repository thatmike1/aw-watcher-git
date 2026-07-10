"""detect cpu-active coding-agent processes (claude, codex, ...) via /proc.

replaces the old warp-title "✳" glyph gate: instead of guessing which agent
session is focused from window titles, sample each agent process's cpu time
between ticks. an idle session sits at ~0 cpu; the session actually generating
burns cpu, whether the user is watching it or it works autonomously. this is
title-independent, so it survives warp layout changes and covers any cli agent
whose process name is listed in config (agent_process_names).
"""

import logging
import os
import sys

logger = logging.getLogger("aw-watcher-git")


class ProcAgentMonitor:
    """samples /proc each tick and reports repos with a cpu-active agent process."""

    def __init__(
        self,
        process_names: list[str],
        repo_path_to_name: dict[str, str],
        cpu_threshold: float = 0.02,
    ) -> None:
        self._process_names = set(process_names)
        # realpath of repo root -> repo name (personal repos already excluded)
        self._repo_path_to_name = dict(repo_path_to_name)
        self._cpu_threshold = cpu_threshold
        self._clk_tck = os.sysconf("SC_CLK_TCK") if sys.platform == "linux" else 100
        # pid -> (cpu_seconds, sample_ts); a pid needs two samples before it
        # can be judged active, so the first tick after startup reports nothing
        self._prev: dict[int, tuple[float, float]] = {}

    def add_repo(self, repo_path: str, repo_name: str) -> None:
        """register a newly discovered repo for cwd matching."""
        self._repo_path_to_name[os.path.realpath(repo_path)] = repo_name

    def sample(self, now: float) -> set[str]:
        """return repo names containing an agent process that burned cpu since last tick."""
        if sys.platform != "linux" or not self._repo_path_to_name:
            return set()

        active_repos: set[str] = set()
        seen_pids: set[int] = set()
        try:
            entries = os.listdir("/proc")
        except OSError:
            return set()

        for entry in entries:
            if not entry.isdigit():
                continue
            pid = int(entry)
            try:
                if not self._is_agent(pid):
                    continue
                cpu = self._cpu_seconds(pid)
                seen_pids.add(pid)
                prev = self._prev.get(pid)
                self._prev[pid] = (cpu, now)
                if prev is None:
                    continue
                prev_cpu, prev_ts = prev
                elapsed = now - prev_ts
                if elapsed <= 0:
                    continue
                if (cpu - prev_cpu) / elapsed < self._cpu_threshold:
                    continue
                cwd = os.path.realpath(os.readlink(f"/proc/{pid}/cwd"))
                repo_name = self._match_repo(cwd)
                if repo_name:
                    active_repos.add(repo_name)
            except (OSError, PermissionError, UnicodeDecodeError, ValueError):
                continue

        # forget pids that exited so the baseline map doesn't grow
        self._prev = {p: v for p, v in self._prev.items() if p in seen_pids}
        return active_repos

    def _is_agent(self, pid: int) -> bool:
        """check whether a pid's command line names a known agent binary."""
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmdline = f.read()
        # argv is null-separated; check the first two entries so node-wrapped
        # clis ("node /path/to/claude") match too
        parts = cmdline.split(b"\x00")[:2]
        for part in parts:
            name = os.path.basename(part.decode("utf-8", errors="replace"))
            if name in self._process_names:
                return True
        return False

    def _cpu_seconds(self, pid: int) -> float:
        """total user+system cpu seconds consumed by a pid."""
        with open(f"/proc/{pid}/stat") as f:
            stat = f.read()
        # comm can contain spaces/parens; fields resume after the last ')'
        rest = stat.rpartition(")")[2].split()
        utime, stime = int(rest[11]), int(rest[12])
        return (utime + stime) / self._clk_tck

    def _match_repo(self, cwd: str) -> str | None:
        """match a working directory against watched repo roots (cwd may be a subdir)."""
        path = cwd
        while True:
            repo_name = self._repo_path_to_name.get(path)
            if repo_name:
                return repo_name
            parent = os.path.dirname(path)
            if parent == path:
                return None
            path = parent
