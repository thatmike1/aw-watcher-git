# aw-watcher-git

ActivityWatch watcher that tracks git repo activity via filesystem monitoring (watchdog/inotify) plus window/agent-process signals, resolved to one repo per tick by a pure attribution engine.

## Architecture

- **config.py** - TOML config loading via `aw_core.config` + argparse CLI overrides
- **git_utils.py** - repo root detection, read-through branch cache, repo discovery
- **attribution.py** - pure `AttributionEngine`: scores signals (fs 3, window 2, agent 1, +0.5 stickiness) and returns at most one repo per tick; no I/O, fully unit-tested
- **proc_agents.py** - `ProcAgentMonitor`: /proc CPU-delta sampling of coding-agent processes (claude/codex/opencode), cwd matched to watched repos
- **window_crossref.py** - `WindowCrossReferencer`: window-title → repo parsing, staleness-based `is_idle()`
- **watcher.py** - `FileChangeHandler` buffers fs events, `GitActivityWatcher` gathers signals and emits the engine's single heartbeat
- **__init__.py / __main__.py** - entry points (lazy import so pure modules work without aw deps)

## Key patterns

- exactly ONE heartbeat per tick — multiple repos interleaving in one AW bucket break heartbeat merging and produce zero-duration events; never emit for more than one repo per tick
- event data is exactly `{repo, branch}` — adding fields (e.g. signal source) breaks AW merge chains
- inferred signals (window, agent, tail) suppressed while idle (afk-event staleness, not just afk status) or on a call (mic capture)
- real fs events count even while idle (agent working autonomously = trackable work)
- linked worktrees collapse onto their main checkout via `git rev-parse --git-common-dir`; the heartbeat's branch still comes from the worktree that produced the signal, tracked in `_last_source` so tail ticks don't flip it
- `repo_map` re-attributes satellite checkouts (VM images, build worktrees) to their main repo
- branch cache is read-through, invalidated when `.git/` changes (branch switch)
- `find_git_repos()` scans one level deep; rescanned every `rescan_interval` for new clones
- thread-safe buffer between watchdog callbacks and the heartbeat loop

## Dependencies

- `aw-client` - ActivityWatch client lib (heartbeats, buckets)
- `watchdog` - filesystem monitoring (inotify on linux)
- `aw-core` - optional, for TOML config loading

## Dev notes

- `python3 -m pytest tests/` — engine tests are pure (no aw deps needed); everything else is manual testing
- installed via `pipx install -e .` (editable) — restart the watcher to pick up changes
- python 3.10+ required
- bucket name: `aw-watcher-git_{hostname}`, event type: `git.activity`
- testing mode uses AW port 5666
