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
    "git_status_interval": 60.0,
    "repo_aliases": {},
    "personal_repos": [],
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
