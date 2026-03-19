"""cross-references aw-watcher-window and aw-watcher-afk to detect dev activity in tracked repos."""

import logging
import os
import re
from collections import defaultdict
from pathlib import Path

logger = logging.getLogger("aw-watcher-git")

DEV_APPS: set[str] = {
    "dev.warp.Warp",
    "Cursor",
    "cursor",
    "Code",
    "code",
    "com.mitchellh.ghostty",
    "kitty",
    "alacritty",
    "org.wezfurlong.wezterm",
    "Gedit",
    "gedit",
}

_CURSOR_PROJECT_RE = re.compile(r" - (.+) - Cursor$")
_WARP_CWD_RE = re.compile(r"~/git/([^/\s]+)")
_WARP_LAUNCH_DIR = Path.home() / ".local" / "share" / "warp-terminal" / "launch_configurations"


def _scan_warp_launch_configs() -> dict[str, str]:
    """scan warp terminal launch configs to build a tab-title-to-repo-name mapping.

    parses yaml files in ~/.local/share/warp-terminal/launch_configurations/
    to extract tab titles and their associated cwds. skips ambiguous titles
    that map to multiple repos.
    """
    if not _WARP_LAUNCH_DIR.is_dir():
        return {}

    # lightweight yaml parsing - these files have a simple structure
    title_to_repos: dict[str, set[str]] = defaultdict(set)

    for yaml_path in _WARP_LAUNCH_DIR.glob("*.yaml"):
        try:
            content = yaml_path.read_text()
        except OSError:
            continue

        # extract title/cwd pairs from the yaml
        # the structure is consistent: title and cwd appear as sibling fields under tabs
        current_title = None
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("- title:"):
                current_title = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("cwd:") and current_title:
                cwd = stripped.split(":", 1)[1].strip()
                repo_name = os.path.basename(cwd)
                if repo_name and current_title:
                    # normalize the title: strip leading "N. " prefix and trailing numbers/spaces
                    normalized = re.sub(r"^\d+\.\s*", "", current_title)
                    normalized = re.sub(r"[\s\d]+$", "", normalized).lower()
                    if normalized:
                        title_to_repos[normalized].add(repo_name)
                current_title = None

    # only keep unambiguous mappings (title maps to exactly one repo)
    aliases: dict[str, str] = {}
    for title, repos in title_to_repos.items():
        if len(repos) == 1:
            repo = next(iter(repos))
            aliases[title] = repo
            # also add with -cc suffix stripped
            base = re.sub(r"-cc$", "", title)
            if base != title and base not in aliases:
                aliases[base] = repo
            # also add with "claude" suffix stripped
            base = re.sub(r"claude$", "", title)
            if base != title and base not in aliases:
                aliases[base] = repo

    return aliases


class WindowCrossReferencer:
    """queries aw-watcher-window and aw-watcher-afk buckets to detect dev activity."""

    def __init__(
        self,
        client: object,
        hostname: str,
        watched_repos: list[str],
        repo_aliases: dict[str, str],
    ) -> None:
        self._client = client
        self._window_bucket = f"aw-watcher-window_{hostname}"
        self._afk_bucket = f"aw-watcher-afk_{hostname}"
        self._watched_repos = set(watched_repos)
        self._watched_repos_lower = {r.lower(): r for r in watched_repos}

        # auto-scan warp launch configs, then overlay explicit config aliases
        auto_aliases = _scan_warp_launch_configs()
        merged = {k: v for k, v in auto_aliases.items()}
        merged.update({k.lower(): v for k, v in repo_aliases.items()})
        self._repo_aliases = merged
        if auto_aliases:
            logger.info(
                "loaded %d aliases from warp launch configs", len(auto_aliases)
            )

    def get_active_repo(self, afk_aware: bool = True) -> str | None:
        """return the repo name if the user is actively working on a tracked repo, else None.

        queries the window watcher for the current window and optionally
        checks afk status before attempting to extract a repo name from
        the window title.
        """
        if afk_aware:
            try:
                afk_events = self._client.get_events(self._afk_bucket, limit=1)
                if afk_events and afk_events[0].data.get("status") == "afk":
                    return None
            except Exception:
                logger.debug("failed to query afk bucket", exc_info=True)

        try:
            window_events = self._client.get_events(self._window_bucket, limit=1)
        except Exception:
            logger.debug("failed to query window bucket", exc_info=True)
            return None

        if not window_events:
            return None

        data = window_events[0].data
        app = data.get("app", "")
        title = data.get("title", "")

        return self._extract_repo(app, title)

    def _extract_repo(self, app: str, title: str) -> str | None:
        """try to extract a repo name from the app and window title."""
        # cursor/vscode: "{file} - {project} - Cursor"
        if app in ("Cursor", "cursor", "Code", "code"):
            repo = self._parse_cursor_title(title)
            if repo:
                return repo

        # terminal apps
        if app in DEV_APPS:
            repo = self._parse_terminal_title(title)
            if repo:
                return repo

        # browser: low-confidence word-boundary match
        if app in ("firefox", "Google-chrome", "chromium", "brave"):
            repo = self._parse_browser_title(title)
            if repo:
                return repo

        return None

    def _parse_cursor_title(self, title: str) -> str | None:
        """extract project name from cursor/vscode title bar.

        format: "{filename} [{diff info}] - {project-name} - Cursor"
        the project name matches the repo folder name exactly.
        """
        match = _CURSOR_PROJECT_RE.search(title)
        if match:
            project = match.group(1).strip()
            return self._resolve_repo_name(project)
        return None

    def _parse_terminal_title(self, title: str) -> str | None:
        """extract repo from terminal window title.

        tries in order:
        1. cwd pattern like ~/git/{repo-name}
        2. alias lookup (e.g. "drmax-cc 1" -> alias "drmax" -> repo "dr-max-kariera")
        3. substring match against watched repo names
        """
        # cwd pattern: ~/git/repo-name
        cwd_match = _WARP_CWD_RE.search(title)
        if cwd_match:
            candidate = cwd_match.group(1)
            resolved = self._resolve_repo_name(candidate)
            if resolved:
                return resolved

        # alias lookup: normalize the title to match how warp config aliases are built
        title_lower = title.lower().strip()
        # strip leading "N. " prefix and trailing digits/spaces
        alias_candidate = re.sub(r"^\d+\.\s*", "", title_lower)
        alias_candidate = re.sub(r"[\s\d]+$", "", alias_candidate)
        resolved = self._resolve_alias(alias_candidate)
        if resolved:
            return resolved

        # also try stripping common suffixes like "-cc"
        base = re.sub(r"-cc$", "", alias_candidate)
        if base != alias_candidate:
            resolved = self._resolve_alias(base)
            if resolved:
                return resolved

        # substring match: check if any watched repo name appears in the title
        for repo_lower, repo_name in self._watched_repos_lower.items():
            if repo_lower in title_lower:
                return repo_name

        return None

    def _parse_browser_title(self, title: str) -> str | None:
        """extract repo from browser title using word-boundary matching.

        low confidence - only matches if a watched repo name appears as
        a distinct word in the title.
        """
        title_lower = title.lower()
        for repo_lower, repo_name in self._watched_repos_lower.items():
            # skip very short names to avoid false positives
            if len(repo_lower) < 4:
                continue
            pattern = r"\b" + re.escape(repo_lower) + r"\b"
            if re.search(pattern, title_lower):
                return repo_name
        return None

    def _resolve_repo_name(self, candidate: str) -> str | None:
        """resolve a candidate string to a watched repo name.

        checks exact match first, then alias lookup.
        """
        if candidate in self._watched_repos:
            return candidate

        return self._resolve_alias(candidate.lower())

    def _resolve_alias(self, alias_lower: str) -> str | None:
        """look up an alias in the repo_aliases config."""
        repo = self._repo_aliases.get(alias_lower)
        if repo and repo in self._watched_repos:
            return repo
        return None
