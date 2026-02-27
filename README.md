# aw-watcher-git

An [ActivityWatch](https://activitywatch.net/) watcher that tracks git repository activity via filesystem monitoring. Editor-agnostic - works with any terminal, IDE, or editor.

## How it works

1. Uses [watchdog](https://github.com/gorakhargosh/watchdog) (inotify on Linux) to monitor directories for file changes
2. When a file changes inside a git repo, the watcher records which repo, branch, and file were active
3. Sends heartbeats to ActivityWatch every ~10s with `{repo, branch, file}`
4. AW's heartbeat merging handles duration - identical consecutive heartbeats merge into one event with extended duration
5. No file changes = no heartbeats = no false activity

## Install

```bash
git clone https://github.com/yourusername/aw-watcher-git.git
cd aw-watcher-git
pip install -e .
```

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
pulsetime = 60.0
ignore_dirs = [".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".next", ".cache", ".turbo", "coverage"]
ignore_extensions = [".pyc", ".pyo", ".swp", ".swo", ".tmp"]
```

- **directories** - parent directories to scan for git repos (scans one level deep)
- **poll_time** - how often to check for buffered changes and send heartbeats
- **pulsetime** - AW heartbeat merge window; events within this window with the same data get merged
- **ignore_dirs** - directory names to skip (matched against any path component)
- **ignore_extensions** - file extensions to ignore

## Event format

Events are sent to bucket `aw-watcher-git_{hostname}` with type `git.activity`:

```json
{
  "timestamp": "2026-02-27T14:30:00+00:00",
  "data": {
    "repo": "my-project",
    "branch": "feat/new-thing",
    "file": "src/components/card.tsx"
  }
}
```

## Verify it works

```bash
# start in testing mode
aw-watcher-git --testing --verbose

# edit a file in a watched repo, then check events
curl http://localhost:5666/api/0/buckets/aw-watcher-git_$(hostname)/events?limit=5
```

## License

MPL-2.0
