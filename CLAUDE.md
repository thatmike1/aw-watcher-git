# aw-watcher-git

ActivityWatch watcher that tracks git repo activity via filesystem monitoring (watchdog/inotify).

## Architecture

- **config.py** - TOML config loading via `aw_core.config` + argparse CLI overrides
- **git_utils.py** - repo root detection, branch lookup (cached), repo discovery
- **watcher.py** - core logic: `FileChangeHandler` buffers fs events, `GitActivityWatcher` runs the heartbeat loop
- **__init__.py / __main__.py** - entry points

## Key patterns

- heartbeats sent to AW every `poll_time` seconds (default 10s), only when there's buffered activity
- `pulsetime=60s` means rapid edits in the same repo/branch merge into continuous blocks
- branch cache is invalidated when `.git/HEAD` changes (branch switch)
- `find_git_repos()` scans one level deep inside configured directories
- thread-safe buffer between watchdog callbacks and the heartbeat loop

## Dependencies

- `aw-client` - ActivityWatch client lib (heartbeats, buckets)
- `watchdog` - filesystem monitoring (inotify on linux)
- `aw-core` - optional, for TOML config loading

## Dev notes

- no tests, manual testing only
- python 3.10+ required
- bucket name: `aw-watcher-git_{hostname}`, event type: `git.activity`
- testing mode uses AW port 5666
