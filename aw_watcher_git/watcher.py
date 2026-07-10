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

from .attribution import AttributionEngine
from .config import load_config, parse_args
from .git_utils import (
    find_git_repos,
    get_branch,
    get_repo_root,
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
    """handles filesystem events and buffers active repo names."""

    def __init__(self, ignore_dirs: list[str], ignore_extensions: list[str]) -> None:
        super().__init__()
        self._ignore_dirs = set(ignore_dirs)
        self._ignore_extensions = set(ignore_extensions)
        self._lock = threading.Lock()
        self._buffer: set[str] = set()

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

        repo_root = get_repo_root(src_path)
        if repo_root is None:
            return

        with self._lock:
            self._buffer.add(os.path.basename(repo_root))

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

    def drain_buffer(self) -> set[str]:
        """return all repo names with buffered activity and clear the buffer."""
        with self._lock:
            repos = set(self._buffer)
            self._buffer.clear()
            return repos


class GitActivityWatcher:
    """main watcher class - sets up observer, signal sources, and the heartbeat loop."""

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
        self._repo_map: dict[str, str] = dict(self._config.get("repo_map", {}))

        self._running = False
        self._observer: Observer | None = None
        self._handler: FileChangeHandler | None = None
        self._repo_paths: dict[str, str] = {}

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

    def _remap(self, repo: str | None) -> str | None:
        """apply repo_map so satellite checkouts bill to their target repo."""
        if repo is None:
            return None
        return self._repo_map.get(repo, repo)

    def _add_repo(self, repo_path: str) -> str | None:
        """schedule a repo for watching; returns its name, or None on failure/collision."""
        repo_name = os.path.basename(repo_path)
        existing = self._repo_paths.get(repo_name)
        if existing is not None:
            if existing != repo_path:
                logger.warning(
                    "repo name collision: %s already watched at %s, skipping %s",
                    repo_name,
                    existing,
                    repo_path,
                )
            return None
        try:
            self._observer.schedule(self._handler, repo_path, recursive=True)
        except OSError as e:
            logger.warning("failed to watch %s: %s", repo_path, e)
            return None
        self._repo_paths[repo_name] = repo_path
        logger.info("watching: %s", repo_path)
        return repo_name

    def _discover_repos(self) -> list[str]:
        """scan configured directories and watch any repos not yet scheduled."""
        added: list[str] = []
        for directory in self._config["directories"]:
            expanded = os.path.expanduser(directory)
            for repo_path in find_git_repos(expanded):
                repo_name = self._add_repo(repo_path)
                if repo_name:
                    added.append(repo_name)
        return added

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
        self._handler = FileChangeHandler(
            ignore_dirs=self._config["ignore_dirs"],
            ignore_extensions=self._config.get("ignore_extensions", []),
        )
        self._observer = Observer()
        self._discover_repos()

        if not self._repo_paths:
            logger.warning("no git repos found to watch")

        self._observer.start()
        logger.info(
            "watching %d repos, polling every %.0fs",
            len(self._repo_paths),
            self._poll_time,
        )

        personal_repos = set(self._config.get("personal_repos", []))

        # window cross-referencer (title parsing + idle detection)
        crossref = None
        if self._config.get("window_crossref", True):
            try:
                from .window_crossref import WindowCrossReferencer

                crossref = WindowCrossReferencer(
                    client=self._client,
                    hostname=socket.gethostname(),
                    watched_repos=list(self._repo_paths.keys()),
                    repo_aliases=self._config.get("repo_aliases", {}),
                    idle_stale_seconds=self._config.get("idle_stale_seconds", 60.0),
                )
                logger.info("window cross-referencing enabled")
            except Exception:
                logger.warning("failed to initialize window cross-ref", exc_info=True)

        # cpu-activity monitor for coding-agent processes (claude, codex, ...)
        agents = None
        agent_names = self._config.get("agent_process_names", [])
        if agent_names and sys.platform == "linux":
            from .proc_agents import ProcAgentMonitor

            agents = ProcAgentMonitor(
                process_names=agent_names,
                repo_path_to_name={
                    os.path.realpath(path): name
                    for name, path in self._repo_paths.items()
                    if name not in personal_repos
                },
            )
            logger.info("agent cpu detection enabled: %s", ", ".join(agent_names))

        engine = AttributionEngine(
            fs_signal_window=self._config.get("fs_signal_window", 60.0),
            tail_seconds=self._config.get("tail_seconds", 300.0),
        )

        afk_aware = self._config.get("afk_aware", True)
        suppress_on_call = self._config.get("suppress_on_call", True)
        rescan_interval = self._config.get("rescan_interval", 300.0)
        last_rescan = time.time()
        mic_suppressed_prev = False

        try:
            while self._running:
                now_ts = time.time()
                now = datetime.now(timezone.utc)

                # suppression signals: real file saves always count, but
                # inferred attribution (window, agents, tail) is paused while
                # on a call or while input has stopped.
                mic_active = suppress_on_call and _mic_in_use()
                if mic_active and not mic_suppressed_prev:
                    logger.info("microphone in use (call detected) — pausing inferred attribution")
                elif not mic_active and mic_suppressed_prev:
                    logger.info("microphone released — resuming inferred attribution")
                mic_suppressed_prev = mic_active

                idle = afk_aware and crossref is not None and crossref.is_idle()
                suppress_inferred = mic_active or idle

                fs_repos = {self._remap(r) for r in self._handler.drain_buffer()}

                window_repo = None
                agent_repos: set[str] = set()
                if not suppress_inferred:
                    if crossref is not None:
                        try:
                            window_repo = self._remap(crossref.get_window_repo())
                        except Exception:
                            logger.debug("window cross-ref failed", exc_info=True)
                    if agents is not None:
                        try:
                            agent_repos = {
                                self._remap(r) for r in agents.sample(now_ts)
                            }
                        except Exception:
                            logger.debug("agent cpu sampling failed", exc_info=True)

                attribution = engine.decide(
                    now=now_ts,
                    fs_repos=fs_repos,
                    window_repo=window_repo,
                    agent_repos=agent_repos,
                    suppress_inferred=suppress_inferred,
                )

                if attribution is not None:
                    repo_path = self._repo_paths.get(attribution.repo)
                    branch = get_branch(repo_path) if repo_path else "unknown"
                    event = Event(
                        timestamp=now,
                        data={"repo": attribution.repo, "branch": branch},
                    )
                    self._client.heartbeat(
                        self._bucket_id,
                        event,
                        pulsetime=self._pulsetime,
                        queued=False,
                    )
                    logger.info(
                        "heartbeat [%s]: %s @ %s",
                        attribution.reason,
                        attribution.repo,
                        branch,
                    )

                # pick up repos cloned since startup
                if now_ts - last_rescan >= rescan_interval:
                    last_rescan = now_ts
                    for repo_name in self._discover_repos():
                        if crossref is not None:
                            crossref.add_repo(repo_name)
                        if agents is not None and repo_name not in personal_repos:
                            agents.add_repo(self._repo_paths[repo_name], repo_name)

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
