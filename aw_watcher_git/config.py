"""configuration loading and argument parsing for aw-watcher-git."""

import argparse
import logging
import os

logger = logging.getLogger(__name__)

DEFAULT_CONFIG = {
    "directories": ["~/git"],
    "poll_time": 10.0,
    "pulsetime": 120.0,
    "ignore_dirs": [
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "dist",
        "build",
        ".next",
        ".cache",
        ".turbo",
        "coverage",
        ".playwright-mcp",
        ".beads",
        ".claude",
    ],
    "ignore_extensions": [
        ".pyc",
        ".pyo",
        ".swp",
        ".swo",
        ".tmp",
    ],
    "window_crossref": True,
    "afk_aware": True,
    "suppress_on_call": True,
    "repo_aliases": {},
    "personal_repos": [],
    # attribute one repo's fs activity to another, e.g. a VM or build-output
    # checkout that is really part of work on the target repo:
    # [aw-watcher-git.repo_map] \n OSX-KVM = "pracino"
    "repo_map": {},
    # collapse linked worktrees onto their main checkout, so several worktrees of
    # one project read as one repo (branch still distinguishes them)
    "group_worktrees": True,
    # process names counted as coding agents for /proc cpu-activity detection
    "agent_process_names": ["claude", "codex", "opencode"],
    # not-afk event end older than this (seconds) counts as idle (input stopped)
    "idle_stale_seconds": 60.0,
    # keep attributing the current repo this long after its last direct signal
    "tail_seconds": 300.0,
    # fs activity keeps a repo eligible for attribution this long (seconds)
    "fs_signal_window": 60.0,
    # hold the attributed repo this long before re-deciding, so competing signals
    # produce one block per window instead of flip-flopping every tick; 0 disables
    "commit_seconds": 60.0,
    # how often to rescan configured directories for newly cloned repos
    "rescan_interval": 300.0,
}


def load_config() -> dict:
    """load config from aw-core TOML config, falling back to defaults."""
    config = dict(DEFAULT_CONFIG)

    try:
        from aw_core.dirs import get_config_dir
        import tomlkit

        config_dir = get_config_dir("aw-watcher-git")
        config_file = os.path.join(config_dir, "aw-watcher-git.toml")

        if os.path.isfile(config_file):
            with open(config_file) as f:
                toml_config = tomlkit.load(f)
            # aw config files use [aw-watcher-git] as the section header
            section = toml_config.get("aw-watcher-git", toml_config)
            for key, value in section.items():
                config[key] = value
            logger.info("loaded config from %s", config_file)
    except ImportError:
        logger.info("tomlkit not available, using defaults")
    except Exception as e:
        logger.warning("failed to load TOML config: %s, using defaults", e)

    return config


def parse_args() -> argparse.Namespace:
    """parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="ActivityWatch watcher for git repository activity"
    )
    parser.add_argument(
        "--testing",
        action="store_true",
        help="run in testing mode (uses port 5666)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="enable verbose/debug logging",
    )
    parser.add_argument(
        "--host",
        type=str,
        default=None,
        help="ActivityWatch server host",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="ActivityWatch server port",
    )
    parser.add_argument(
        "--poll-time",
        type=float,
        default=None,
        help="seconds between heartbeat checks",
    )
    return parser.parse_args()
