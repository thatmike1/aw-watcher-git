# aw-watcher-git debug guide

## quick API checks

hostname: `thatmike1-MS-7B86`
bucket: `aw-watcher-git_thatmike1-MS-7B86`
server: python aw-server (not rust) on port 5600

```bash
# server info
curl -s http://localhost:5600/api/0/info

# list all buckets
curl -s http://localhost:5600/api/0/buckets/ | python3 -c "import sys,json; [print(k) for k in sorted(json.load(sys.stdin).keys())]"

# get events for a specific date range (2026-03-03)
curl -s "http://localhost:5600/api/0/buckets/aw-watcher-git_thatmike1-MS-7B86/events?start=2026-03-03T00:00:00Z&end=2026-03-04T00:00:00Z&limit=-1" | python3 -m json.tool

# event count only
curl -s "http://localhost:5600/api/0/buckets/aw-watcher-git_thatmike1-MS-7B86/events?start=2026-03-03T00:00:00Z&end=2026-03-04T00:00:00Z&limit=-1" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))"

# bucket metadata (created, last_updated, event_count)
curl -s "http://localhost:5600/api/0/buckets/aw-watcher-git_thatmike1-MS-7B86"
```

## event data format

```json
{
  "id": 1066257,
  "timestamp": "2026-02-27T17:32:55.750Z",
  "duration": 50.074,
  "data": {
    "repo": "cez-ems",
    "branch": "12-kalendar-provozu",
    "file": "src/components/card.tsx"
  }
}
```

- `duration > 0` means heartbeats merged (pulsetime=60s)
- `duration = 0` means a single heartbeat that didn't merge with anything

## watcher process

```bash
# check if running
ps aux | grep aw-watcher-git | grep -v grep

# watcher binary location
/home/thatmike1/.local/bin/aw-watcher-git

# watcher source code
/home/thatmike1/git/aw-watcher-git/aw_watcher_git/

# key files
# - watcher.py — FileChangeHandler (watchdog) + GitActivityWatcher (heartbeat loop)
# - config.py — TOML config + CLI args
# - git_utils.py — repo detection, branch lookup

# watcher config
cat ~/.config/activitywatch/aw-watcher-git/aw-watcher-git.toml

# logs (aw-qt captures stdout)
# no dedicated log file — run manually with --verbose to see output:
# aw-watcher-git --verbose
```

## watched directories

configured in the toml above, default is `["~/git"]`. the watcher scans one level deep for git repos inside those directories.

## comparing with other watchers

```bash
# afk events for same day (to see if user was active)
curl -s "http://localhost:5600/api/0/buckets/aw-watcher-afk_thatmike1-MS-7B86/events?start=2026-03-03T00:00:00Z&end=2026-03-04T00:00:00Z&limit=-1" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'{len(d)} events, total not-afk: {sum(e[\"duration\"] for e in d if e[\"data\"].get(\"status\")==\"not-afk\"):.0f}s')"

# window events (to see if editors were open)
curl -s "http://localhost:5600/api/0/buckets/aw-watcher-window_thatmike1-MS-7B86/events?start=2026-03-03T00:00:00Z&end=2026-03-04T00:00:00Z&limit=-1" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'{len(d)} window events')"
```

## aw-qt autostart config

```
/home/thatmike1/.config/activitywatch/aw-qt/aw-qt.toml
```

aw-watcher-git is in the autostart_modules list.

## aw-server config (custom visualization)

- python server: `~/.config/activitywatch/aw-server/aw-server.toml`
- rust server: `~/.config/activitywatch/aw-server-rust/config.toml`

currently running the python server.
