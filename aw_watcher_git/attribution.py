"""pure attribution engine - decides which single repo (if any) earns this tick.

the engine holds no I/O: the watcher loop gathers signals (filesystem events,
window title, cpu-active agent processes, idle state) and calls decide() once
per tick. at most one repo is returned, so the AW bucket stays a single
coherent timeline and heartbeat merge chains are never broken by interleaved
repos.

scoring per tick:
  - fs activity within the fs window   -> 3   (strongest: real writes)
  - focused window resolves to repo    -> 2   (where the user is looking)
  - cpu-active agent process in repo   -> 1   (claude/codex working)
  - currently attributed repo          -> +0.5 (stickiness, breaks ties)

scores are not acted on tick by tick. they accumulate into a commit window
(commit_seconds, default 60s): the repo committed at the start of a window is
held for its whole duration, and the window's accumulated leader wins the next
one. two repos with live signals therefore produce one long block per minute
instead of alternating every tick - which is what shreds an AW bucket into
zero-duration events, since a merge chain breaks the moment {repo, branch}
changes. the cost is deliberate: a detour shorter than the window is absorbed
into whatever dominated it rather than becoming its own event.

a committed repo that goes entirely silent within a window is released
immediately - that is a real switch, not flapping, and there is nothing to
absorb.

when the user is idle or on a call (suppress_inferred), only repos with real
fs events in this exact tick are eligible - lingering signals and the tail do
not apply, so idle time is never inflated by inference. the commit window is
bypassed there too: holding a repo that is not being written to would bill
idle time on inference, which is exactly what suppression exists to prevent.

when no signal fires at all, attribution "tails" on the current repo for up to
tail_seconds after its last direct signal (reading docs, thinking) and then
stops.
"""

from dataclasses import dataclass

FS_SCORE = 3.0
WINDOW_SCORE = 2.0
AGENT_SCORE = 1.0
STICKY_BONUS = 0.5


@dataclass(frozen=True)
class Attribution:
    """the engine's verdict for one tick."""

    repo: str
    reason: str  # "fs" | "window" | "agent" | "hold" | "tail"


class AttributionEngine:
    """decides at most one active repo per tick from all available signals."""

    def __init__(
        self,
        fs_signal_window: float = 60.0,
        tail_seconds: float = 300.0,
        commit_seconds: float = 60.0,
    ) -> None:
        self._fs_signal_window = fs_signal_window
        self._tail_seconds = tail_seconds
        self._commit_seconds = commit_seconds
        self._last_fs: dict[str, float] = {}
        self._current: str | None = None
        # last time the current repo won on a direct signal (not tail)
        self._last_direct: dict[str, float] = {}
        # per-repo score accumulated since the current commit window opened
        self._window_scores: dict[str, float] = {}
        self._window_start: float | None = None
        # last tick each repo carried any score, for releasing a stale hold
        self._last_seen: dict[str, float] = {}

    def decide(
        self,
        now: float,
        fs_repos: set[str],
        window_repo: str | None,
        agent_repos: set[str],
        suppress_inferred: bool = False,
    ) -> Attribution | None:
        """return the single repo attribution for this tick, or None."""
        for repo in fs_repos:
            self._last_fs[repo] = now
        self._prune(now)

        if suppress_inferred:
            # a held repo would keep earning time without being written to, so
            # the window is dropped rather than carried across the idle stretch
            self._reset_window()
            if not fs_repos:
                return None
            chosen = self._pick(fs_repos, {r: FS_SCORE for r in fs_repos})
            return self._commit(chosen, now, "fs")

        scores: dict[str, float] = {}
        for repo, seen in self._last_fs.items():
            if now - seen <= self._fs_signal_window:
                scores[repo] = scores.get(repo, 0.0) + FS_SCORE
        if window_repo:
            scores[window_repo] = scores.get(window_repo, 0.0) + WINDOW_SCORE
        for repo in agent_repos:
            scores[repo] = scores.get(repo, 0.0) + AGENT_SCORE
        if self._current in scores:
            scores[self._current] += STICKY_BONUS

        if scores:
            chosen, held = self._apply_commit_window(now, scores)
            reason = "hold" if held else self._reason(chosen, fs_repos, window_repo, agent_repos)
            # a held tick must not refresh the tail: the tail should still be
            # measured from the repo's own last real signal
            return self._commit(chosen, now, reason, direct=not held)

        # tail: no signal anywhere - keep attributing the current repo for a
        # bounded window after its last direct win, then go quiet. the tail
        # never refreshes _last_direct, so it cannot sustain itself.
        if (
            self._current is not None
            and now - self._last_direct.get(self._current, 0.0) <= self._tail_seconds
        ):
            return Attribution(self._current, "tail")

        self._current = None
        self._reset_window()
        return None

    def _apply_commit_window(self, now: float, scores: dict[str, float]) -> tuple[str, bool]:
        """return (repo billed this tick, whether it was held rather than freshly won)."""
        if self._commit_seconds <= 0:
            return self._pick(set(scores), scores), False

        for repo, score in scores.items():
            self._window_scores[repo] = self._window_scores.get(repo, 0.0) + score
            self._last_seen[repo] = now

        if self._current is not None and self._window_start is not None:
            still_open = now - self._window_start < self._commit_seconds
            # a hold is only worth keeping while the repo is still nearby: gone
            # for a tick or two is alternating focus and should be absorbed,
            # gone for half a window is a departure and holding it would bill
            # a repo nothing is happening in
            absent = now - self._last_seen.get(self._current, 0.0)
            if still_open and absent <= self._commit_seconds / 2:
                # only a tick where the held repo has no signal of its own is a
                # true hold; otherwise it is winning on its own merits
                return self._current, self._current not in scores

        # window closed (or vacated): its accumulated leader takes the next one
        totals = dict(self._window_scores)
        if self._current in totals:
            totals[self._current] += STICKY_BONUS
        winner = self._pick(set(totals), totals)

        # the new window starts from this tick alone, so the closing window's
        # history can't keep voting after it ended
        self._window_scores = dict(scores)
        self._window_start = now
        return winner, winner not in scores

    def _reset_window(self) -> None:
        """drop the commit window so the next signal opens a fresh one."""
        self._window_scores = {}
        self._window_start = None

    def _commit(self, repo: str, now: float, reason: str, direct: bool = True) -> Attribution:
        """record a win and return the attribution; only direct wins feed the tail."""
        self._current = repo
        if direct:
            self._last_direct[repo] = now
        return Attribution(repo, reason)

    def _pick(self, candidates: set[str], scores: dict[str, float]) -> str:
        """pick the winner: highest score, then current repo, then freshest fs, then name."""
        return max(
            candidates,
            key=lambda r: (
                scores.get(r, 0.0),
                r == self._current,
                self._last_fs.get(r, 0.0),
                r,
            ),
        )

    def _reason(
        self,
        chosen: str,
        fs_repos: set[str],
        window_repo: str | None,
        agent_repos: set[str],
    ) -> str:
        """name the strongest signal that backed the chosen repo, for logging."""
        if chosen in fs_repos:
            return "fs"
        if chosen == window_repo:
            return "window"
        if chosen in agent_repos:
            return "agent"
        # won on lingering fs recency alone
        return "fs"

    def _prune(self, now: float) -> None:
        """drop signal bookkeeping that can no longer influence a decision."""
        horizon = max(self._fs_signal_window, self._tail_seconds) + 60.0
        self._last_fs = {r: t for r, t in self._last_fs.items() if now - t <= horizon}
        self._last_direct = {
            r: t for r, t in self._last_direct.items() if now - t <= horizon
        }
        self._last_seen = {r: t for r, t in self._last_seen.items() if now - t <= horizon}
