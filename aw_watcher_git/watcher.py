"""core watcher - monitors filesystem changes and sends heartbeats to ActivityWatch."""

import glob
import logging
import os
import signal
import socket
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from aw_core.models import Event
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .config import load_config, parse_args
from .git_utils import (
    find_git_repos,
    get_branch,
    get_repo_info,
    has_dirty_worktree,
    invalidate_branch_cache,
)

logger = logging.getLogger("aw-watcher-git")

WATCHER_NAME = "aw-watcher-git"
EVENT_TYPE = "git.activity"


def _mic_in_use() -> bool:
    """detect whether any application is actively capturing the microphone.

    reads alsa capture-stream status files on linux; a state of "RUNNING"
    means some process (a browser on a video call, zoom, teams) holds an
    open capture stream. used as a focus-independent "on a call" signal.
    returns False on non-linux or any error so a detection failure never
    suppresses legitimate tracking.
    """
    if sys.platform != "linux":
        return False
    try:
        for status_path in glob.glob("/proc/asound/card*/pcm*c/sub*/status"):
            try:
                with open(status_path) as f:
                    if "RUNNING" in f.read():
                        return True
            except OSError:
                continue
    except OSError:
        pass
    return False


class FileChangeHandler(FileSystemEventHandler):
    """handles filesystem events and buffers activity per repo."""

    def __init__(self, ignore_dirs: list[str], ignore_extensions: list[str]) -> None:
        super().__init__()
        self._ignore_dirs = set(ignore_dirs)
        self._ignore_extensions = set(ignore_extensions)
        self._lock = threading.Lock()
        self._buffer: dict[str, dict[str, str]] = {}

    def _should_ignore(self, path: str) -> bool:
        """check if a path should be ignored based on directory or extension."""
        parts = Path(path).parts
        for part in parts:
            if part in self._ignore_dirs:
                return True

        _, ext = os.path.splitext(path)
        if ext in self._ignore_extensions:
            return True

        return False

    def _handle_change(self, src_path: str) -> None:
        """process a file change event."""
        # branch change detection must happen before ignore check
        # because .git is in ignore_dirs but we still need to invalidate the cache
        path_parts = Path(src_path).parts
        if ".git" in path_parts:
            git_idx = list(path_parts).index(".git")
            if git_idx > 0:
                repo_root = str(Path(*path_parts[:git_idx]))
                invalidate_branch_cache(repo_root)
            return

        if self._should_ignore(src_path):
            return

        info = get_repo_info(src_path)
        if info is None:
            return

        with self._lock:
            repo_key = info["repo"]
            self._buffer[repo_key] = info

    def on_modified(self, event: FileSystemEvent) -> None:
        """called when a file is modified."""
        if not event.is_directory:
            self._handle_change(event.src_path)

    def on_created(self, event: FileSystemEvent) -> None:
        """called when a file is created."""
        if not event.is_directory:
            self._handle_change(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        """called when a file is moved/renamed."""
        if not event.is_directory:
            self._handle_change(event.dest_path)

    def on_deleted(self, event: FileSystemEvent) -> None:
        """called when a file is deleted."""
        if not event.is_directory:
            self._handle_change(event.src_path)

    def on_closed(self, event: FileSystemEvent) -> None:
        """called when a writable file is closed (linux inotify IN_CLOSE_WRITE)."""
        if not event.is_directory:
            self._handle_change(event.src_path)

    def drain_buffer(self) -> list[dict[str, str]]:
        """return all buffered activity and clear the buffer."""
        with self._lock:
            events = list(self._buffer.values())
            self._buffer.clear()
            return events


class GitActivityWatcher:
    """main watcher class - sets up observer and heartbeat loop."""

    def __init__(
        self,
        testing: bool = False,
        verbose: bool = False,
        host: str | None = None,
        port: int | None = None,
        poll_time: float | None = None,
    ) -> None:
        self._testing = testing
        self._config = load_config()

        self._poll_time = poll_time or self._config["poll_time"]
        self._pulsetime = self._config["pulsetime"]

        self._running = False
        self._observer: Observer | None = None

        # setup aw-client
        from aw_client import ActivityWatchClient

        client_args: dict = {"testing": testing}
        if host:
            client_args["host"] = host
        if port:
            client_args["port"] = port

        self._client = ActivityWatchClient(WATCHER_NAME, **client_args)

        hostname = socket.gethostname()
        self._bucket_id = f"{WATCHER_NAME}_{hostname}"

    def run(self) -> None:
        """start the watcher - blocks until shutdown."""
        self._running = True
        signal.signal(signal.SIGINT, self._handle_signal)
        if sys.platform != "win32":
            signal.signal(signal.SIGTERM, self._handle_signal)

        # wait for AW server
        logger.info("waiting for ActivityWatch server...")
        while self._running:
            try:
                self._client.get_info()
                break
            except Exception:
                time.sleep(3)

        if not self._running:
            return

        logger.info("connected to ActivityWatch server")

        # create bucket
        self._client.create_bucket(
            self._bucket_id, event_type=EVENT_TYPE, queued=False
        )
        logger.info("bucket created: %s", self._bucket_id)

        # find repos and start watching
        handler = FileChangeHandler(
            ignore_dirs=self._config["ignore_dirs"],
            ignore_extensions=self._config.get("ignore_extensions", []),
        )
        self._observer = Observer()

        repo_paths: dict[str, str] = {}
        for directory in self._config["directories"]:
            expanded = os.path.expanduser(directory)
            repos = find_git_repos(expanded)
            for repo_path in repos:
                try:
                    self._observer.schedule(handler, repo_path, recursive=True)
                    repo_name = os.path.basename(repo_path)
                    repo_paths[repo_name] = repo_path
                    logger.info("watching: %s", repo_path)
                except OSError as e:
                    logger.warning("failed to watch %s: %s", repo_path, e)

        if not repo_paths:
            logger.warning("no git repos found to watch")

        self._observer.start()
        logger.info(
            "watching %d repos, polling every %.0fs", len(repo_paths), self._poll_time
        )

        # initialize window cross-referencer
        crossref = None
        if self._config.get("window_crossref", True):
            try:
                from .window_crossref import WindowCrossReferencer

                hostname = socket.gethostname()
                crossref = WindowCrossReferencer(
                    client=self._client,
                    hostname=hostname,
                    watched_repos=list(repo_paths.keys()),
                    repo_aliases=self._config.get("repo_aliases", {}),
                    repo_paths=repo_paths,
                    personal_repos=self._config.get("personal_repos", []),
                )
                logger.info("window cross-referencing enabled")
            except Exception:
                logger.warning("failed to initialize window cross-ref", exc_info=True)

        # heartbeat loop
        last_git_status_time = 0.0
        recent_active_repos: dict[str, float] = {}
        git_status_interval = self._config.get("git_status_interval", 60.0)
        afk_aware = self._config.get("afk_aware", True)
        suppress_on_call = self._config.get("suppress_on_call", True)
        mic_suppressed_prev = False

        try:
            while self._running:
                now_ts = time.time()
                now = datetime.now(timezone.utc)
                fs_repo_names: set[str] = set()

                # suppression signals: real file saves (phase 1) always count,
                # but inferred activity (phases 2 & 3) is paused while on a call
                # or while the afk watcher reports the user is away.
                mic_active = suppress_on_call and _mic_in_use()
                if mic_active and not mic_suppressed_prev:
                    logger.info("microphone in use (call detected) — pausing inferred heartbeats")
                elif not mic_active and mic_suppressed_prev:
                    logger.info("microphone released — resuming inferred heartbeats")
                mic_suppressed_prev = mic_active

                afk = False
                if afk_aware and crossref is not None:
                    afk = crossref.is_afk()

                suppress_inferred = mic_active or afk

                # phase 1: drain filesystem events (existing behavior)
                events = handler.drain_buffer()
                for event_data in events:
                    fs_repo_names.add(event_data["repo"])
                    recent_active_repos[event_data["repo"]] = now_ts
                    event = Event(timestamp=now, data=event_data)
                    self._client.heartbeat(
                        self._bucket_id,
                        event,
                        pulsetime=self._pulsetime,
                        queued=False,
                    )
                    logger.info(
                        "heartbeat [fs]: %s @ %s",
                        event_data["repo"],
                        event_data["branch"],
                    )

                # phase 2: window cross-referencing
                if crossref is not None and not suppress_inferred:
                    try:
                        # afk already handled above; avoid a duplicate bucket query.
                        # pass repos with recent *real* file activity so the /proc
                        # claude-code fallback only attributes to a repo you've
                        # actually been editing — a background claude session in a
                        # watched repo no longer gets billed on any focused ✳ tab.
                        recent_fs_repos = {
                            r
                            for r, t in recent_active_repos.items()
                            if now_ts - t <= 300
                        }
                        window_repo = crossref.get_active_repo(
                            afk_aware=False, recent_repos=recent_fs_repos
                        )
                        if window_repo and window_repo not in fs_repo_names:
                            repo_path = repo_paths.get(window_repo)
                            if repo_path:
                                branch = get_branch(repo_path)
                                event_data = {"repo": window_repo, "branch": branch}
                                # do NOT refresh recent_active_repos here: inferred
                                # window activity must not sustain phase 3 git-status
                                # polling, or phases 2 & 3 keep each other alive with
                                # no real edits (unbounded inferred over-count).
                                event = Event(timestamp=now, data=event_data)
                                self._client.heartbeat(
                                    self._bucket_id,
                                    event,
                                    pulsetime=self._pulsetime,
                                    queued=False,
                                )
                                logger.info(
                                    "heartbeat [window]: %s @ %s",
                                    window_repo,
                                    branch,
                                )
                    except Exception:
                        logger.debug("window cross-ref failed", exc_info=True)

                # phase 3: git-status polling (less frequent)
                if not suppress_inferred and now_ts - last_git_status_time >= git_status_interval:
                    last_git_status_time = now_ts
                    for repo_name, last_active in list(recent_active_repos.items()):
                        if now_ts - last_active > 300:
                            continue
                        if repo_name in fs_repo_names:
                            continue
                        repo_path = repo_paths.get(repo_name)
                        if repo_path and has_dirty_worktree(repo_path):
                            branch = get_branch(repo_path)
                            event_data = {"repo": repo_name, "branch": branch}
                            event = Event(timestamp=now, data=event_data)
                            self._client.heartbeat(
                                self._bucket_id,
                                event,
                                pulsetime=self._pulsetime,
                                queued=False,
                            )
                            logger.info(
                                "heartbeat [git-status]: %s @ %s",
                                repo_name,
                                branch,
                            )

                # clean up stale entries
                cutoff = now_ts - 600
                recent_active_repos = {
                    k: v for k, v in recent_active_repos.items() if v > cutoff
                }

                time.sleep(self._poll_time)
        except Exception:
            logger.exception("error in heartbeat loop")
        finally:
            self._shutdown()

    def _handle_signal(self, signum: int, frame: object) -> None:
        """handle termination signals."""
        logger.info("received signal %d, shutting down...", signum)
        self._running = False

    def _shutdown(self) -> None:
        """clean shutdown of observer and client."""
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
        logger.info("stopped")


def main() -> None:
    """entry point for aw-watcher-git."""
    args = parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    logger.info("starting aw-watcher-git (testing=%s)", args.testing)

    watcher = GitActivityWatcher(
        testing=args.testing,
        verbose=args.verbose,
        host=args.host,
        port=args.port,
        poll_time=args.poll_time,
    )

    try:
        watcher.run()
    except KeyboardInterrupt:
        logger.info("interrupted")
        sys.exit(0)
