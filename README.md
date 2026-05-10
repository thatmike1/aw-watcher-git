# aw-watcher-git

An [ActivityWatch](https://activitywatch.net/) watcher that tracks git repository activity via filesystem monitoring. Editor-agnostic - works with any terminal, IDE, or editor.

## How it works

Every `poll_time` seconds (default 10s) the watcher runs three detection phases and emits a heartbeat for each tracked repo that looks active. AW's heartbeat merging (`pulsetime`, default 300s) collapses consecutive heartbeats with identical `{repo, branch}` into one continuous event.

**Phase 1 — filesystem events.** [watchdog](https://github.com/gorakhargosh/watchdog) (inotify on Linux) monitors each watched repo recursively. Modify/create/move/delete/close-write events on non-ignored files get buffered, deduplicated per repo, and drained each tick into `{repo, branch}` heartbeats. Writes inside `.git/` invalidate the cached branch so checkouts are picked up immediately.

**Phase 2 — window cross-reference.** If `window_crossref` is enabled, the watcher queries the local `aw-watcher-window` (and `aw-watcher-afk`, if `afk_aware`) buckets and tries to map the current window to a tracked repo:
- Cursor/VS Code: parses the project name from the title (`"file - project - Cursor"`).
- Terminals (Warp, Ghostty, kitty, alacritty, WezTerm, gedit): matches `~/git/<repo>` in the title, then falls back to aliases auto-scraped from Warp launch configs / explicit `repo_aliases`, then to substring matches against watched repo names.
- Browsers: low-confidence word-boundary match on watched repo names.
- Warp showing a Claude Code session (`✳ …` title): scans `/proc/*/cwd` for running `claude` processes and matches their working directory against watched repos.

This catches activity in the IDE/terminal even when no file is being written to disk. Repos listed in `personal_repos` are excluded from the Claude Code `/proc` matcher.

**Phase 3 — git status polling.** Every `git_status_interval` seconds (default 60s), for each repo that's been active in the last 5 minutes but didn't tick this cycle, the watcher runs `git status --porcelain`. A dirty worktree emits a heartbeat — so long thinking/reading sessions with uncommitted changes still register.

No activity from any phase = no heartbeats = no false activity. Branch names are cached per repo (invalidated on `.git/HEAD` writes); detached HEAD reports as `detached:<shorthash>`.

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
pulsetime = 300.0
ignore_dirs = [".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".next", ".cache", ".turbo", "coverage", ".playwright-mcp", ".beads", ".claude"]
ignore_extensions = [".pyc", ".pyo", ".swp", ".swo", ".tmp"]

window_crossref = true
afk_aware = true
git_status_interval = 60.0
repo_aliases = {}
personal_repos = []
```

- **directories** — parent directories to scan for git repos (scans one level deep)
- **poll_time** — how often the heartbeat loop runs (seconds)
- **pulsetime** — AW heartbeat merge window; consecutive heartbeats with the same `{repo, branch}` within this window get merged into one event
- **ignore_dirs** — directory names to skip (matched against any path component)
- **ignore_extensions** — file extensions to ignore
- **window_crossref** — enable phase 2 (cross-reference `aw-watcher-window` to detect activity in IDE/terminal without file writes)
- **afk_aware** — when window cross-ref is on, suppress heartbeats while `aw-watcher-afk` reports AFK
- **git_status_interval** — how often phase 3 runs `git status` on recently active repos (seconds)
- **repo_aliases** — map terminal/window title fragments to canonical repo names, e.g. `{ "drmax" = "dr-max-kariera" }`. Overlays the aliases auto-scraped from Warp launch configs.
- **personal_repos** — repo names to exclude from the Claude Code `/proc`-based fallback matcher

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

Heartbeats only carry `repo` and `branch` — individual file paths are not stored. Logs tag each heartbeat with its source: `[fs]`, `[window]`, or `[git-status]`.

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
