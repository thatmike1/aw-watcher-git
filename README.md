# aw-watcher-git

An [ActivityWatch](https://activitywatch.net/) watcher that tracks git repository activity via filesystem monitoring. Editor-agnostic - works with any terminal, IDE, or editor.

## How it works

Every `poll_time` seconds (default 10s) the watcher gathers signals and asks a pure **attribution engine** which single repo (if any) earns the tick. At most one heartbeat is emitted per tick, so the AW bucket stays one coherent timeline — concurrent activity in several repos can't interleave and break AW's heartbeat merging (`pulsetime` collapses consecutive heartbeats with identical `{repo, branch}` into one continuous event; interleaved repos would otherwise all end up as zero-duration events).

**Signals gathered per tick:**

- **Filesystem events** (score 3, lingers `fs_signal_window` seconds). [watchdog](https://github.com/gorakhargosh/watchdog) (inotify on Linux) monitors each watched repo recursively; modify/create/move/delete/close-write events on non-ignored files mark the repo active. Writes inside `.git/` invalidate the cached branch so checkouts are picked up immediately.
- **Focused window** (score 2). If `window_crossref` is enabled, the current `aw-watcher-window` event is mapped to a repo: Cursor/VS Code titles (`"file - project - Cursor"`), terminal titles (`~/git/<repo>` paths, aliases auto-scraped from Warp launch configs, explicit `repo_aliases`, substring match), and low-confidence word-boundary browser-title matches. Stale window events (dead watcher) are ignored.
- **CPU-active agent processes** (score 1). Processes named in `agent_process_names` (claude, codex, opencode by default) are sampled via `/proc`; a session that burned CPU since the last tick attributes to the repo its working directory sits in. This is title-independent, so it tracks reading/planning agent sessions and survives terminal layout changes. Repos in `personal_repos` are excluded.
- **Stickiness** (+0.5). The currently attributed repo wins ties, so two concurrently active repos never flip-flop; a switch requires a genuinely stronger signal (usually window focus).

**Suppression.** While the user is idle or on a call (mic capture detected), only real file saves from that exact tick count — inferred signals pause. Idle is detected from the `aw-watcher-afk` bucket by *staleness*: the not-afk event's end stops advancing the moment input stops, which catches breaks in ~`idle_stale_seconds` (default 60s) instead of waiting for the ~180s afk timeout.

**Tail.** When all signals go quiet (thinking, reading docs), the current repo keeps earning time for up to `tail_seconds` (default 300s) after its last direct signal, then attribution stops. The tail can't sustain itself.

**Remapping.** `repo_map` re-attributes one repo's activity to another — e.g. a VM image checkout or build-output worktree whose writes are really work on the main project. Mapped this way, satellite writes merge into the main repo's blocks instead of fragmenting them.

No signal = no heartbeat = no false activity. Branch names are cached per repo (invalidated on `.git/` writes); detached HEAD reports as `detached:<shorthash>`. Configured directories are rescanned every `rescan_interval` seconds so freshly cloned repos get picked up without a restart.

## Install

```bash
git clone https://github.com/thatmike1/aw-watcher-git.git
cd aw-watcher-git
pip install -e .
```

Requires Python 3.10+. For phase 2 (window cross-reference), [`aw-watcher-window`](https://github.com/ActivityWatch/aw-watcher-window) and optionally [`aw-watcher-afk`](https://github.com/ActivityWatch/aw-watcher-afk) need to be running.

## Usage

```bash
# normal mode (requires aw-server running)
aw-watcher-git

# testing mode (uses AW testing instance on port 5666)
aw-watcher-git --testing --verbose
```

### CLI options

| Flag | Description |
|------|-------------|
| `--testing` | Use AW testing instance (port 5666) |
| `--verbose` | Enable debug logging |
| `--host HOST` | AW server host |
| `--port PORT` | AW server port |
| `--poll-time N` | Seconds between heartbeat checks (default: 10) |

## Configuration

Config is loaded from `~/.config/activitywatch/aw-watcher-git/aw-watcher-git.toml` (created on first run with defaults):

```toml
directories = ["~/git"]
poll_time = 10.0
pulsetime = 120.0
ignore_dirs = [".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".next", ".cache", ".turbo", "coverage", ".playwright-mcp", ".beads", ".claude"]
ignore_extensions = [".pyc", ".pyo", ".swp", ".swo", ".tmp"]

window_crossref = true
afk_aware = true
suppress_on_call = true
repo_aliases = {}
personal_repos = []
agent_process_names = ["claude", "codex", "opencode"]
idle_stale_seconds = 60.0
tail_seconds = 300.0
fs_signal_window = 60.0
rescan_interval = 300.0

[aw-watcher-git.repo_map]
# "OSX-KVM" = "pracino"
```

- **directories** — parent directories to scan for git repos (scans one level deep)
- **poll_time** — how often the heartbeat loop runs (seconds)
- **pulsetime** — AW heartbeat merge window; consecutive heartbeats with the same `{repo, branch}` within this window get merged into one event
- **ignore_dirs** — directory names to skip (matched against any path component)
- **ignore_extensions** — file extensions to ignore
- **window_crossref** — cross-reference `aw-watcher-window` to detect activity in IDE/terminal without file writes
- **afk_aware** — suppress inferred attribution while the user is idle (see `idle_stale_seconds`)
- **suppress_on_call** — suppress inferred attribution while the microphone is captured (video call)
- **repo_aliases** — map terminal/window title fragments to canonical repo names, e.g. `{ "drmax" = "dr-max-kariera" }`. Overlays the aliases auto-scraped from Warp launch configs.
- **personal_repos** — repo names to exclude from the agent-process `/proc` matcher
- **agent_process_names** — process names sampled for CPU activity as coding agents
- **idle_stale_seconds** — a not-afk event whose end lags now by more than this counts as idle
- **tail_seconds** — how long the current repo keeps earning time after its last direct signal
- **fs_signal_window** — how long a file save keeps its repo eligible for attribution
- **rescan_interval** — how often to rescan `directories` for newly cloned repos
- **repo_map** — attribute one repo's activity to another (satellite VM/build checkouts)

## Event format

Events are sent to bucket `aw-watcher-git_{hostname}` with type `git.activity`:

```json
{
  "timestamp": "2026-02-27T14:30:00+00:00",
  "duration": 42.0,
  "data": {
    "repo": "my-project",
    "branch": "feat/new-thing"
  }
}
```

Heartbeats only carry `repo` and `branch` — individual file paths are not stored (and the winning signal is not stored either, since differing data would break AW's merge chains). Logs tag each heartbeat with its signal: `[fs]`, `[window]`, `[agent]`, or `[tail]`.

## Verify it works

```bash
# start in testing mode
aw-watcher-git --testing --verbose

# edit a file in a watched repo, then check events
curl http://localhost:5666/api/0/buckets/aw-watcher-git_$(hostname)/events?limit=5
```

## Visualization

aw-watcher-git includes a custom visualization dashboard that shows git activity broken down by repo, branch, and file. It runs inside the AW web UI as a custom visualization.

### Setup

1. Add the following to `~/.config/activitywatch/aw-server/aw-server.toml`:

```toml
[server.custom_static]
aw-watcher-git = "/full/path/to/aw-watcher-git/visualization"
```

2. Restart aw-server (or restart aw-qt)

3. In the AW web UI, go to **Activity** → **Edit View** → **Add Visualization** → **Custom Visualization** and enter `aw-watcher-git`

### Features

- **Summary** — stats cards, repo bar chart, top branches doughnut, hourly activity chart
- **Repos** — time per repo with click-to-drilldown into branch breakdown
- **Branches** — time per branch with repo filter
- **Timeline** — gantt-style view of branch activity over time with tooltips
- **Files** — ranked table of most-edited files with duration bars

## License

MPL-2.0
