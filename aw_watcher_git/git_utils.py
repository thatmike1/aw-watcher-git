"""utilities for resolving git repo info from file paths."""

import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_repo_root_cache: dict[str, str | None] = {}
_branch_cache: dict[str, str] = {}


def find_git_repos(directory: str) -> list[str]:
    """walk a directory and return paths of all git repositories found."""
    repos: list[str] = []
    root = os.path.expanduser(directory)
    if not os.path.isdir(root):
        logger.warning("directory does not exist: %s", root)
        return repos

    for entry in os.scandir(root):
        if entry.is_dir(follow_symlinks=False):
            git_dir = os.path.join(entry.path, ".git")
            if os.path.isdir(git_dir) or os.path.isfile(git_dir):
                repos.append(entry.path)
    return repos


def get_repo_root(file_path: str) -> str | None:
    """given a file path, find the git repo root it belongs to. results are cached."""
    path = Path(file_path).resolve()

    for parent in [path] + list(path.parents):
        parent_str = str(parent)
        if parent_str in _repo_root_cache:
            return _repo_root_cache[parent_str]

        git_dir = parent / ".git"
        if git_dir.is_dir() or git_dir.is_file():
            _repo_root_cache[parent_str] = parent_str
            return parent_str

    _repo_root_cache[str(path)] = None
    return None


def get_branch(repo_root: str) -> str:
    """get the current branch name for a repo. returns HEAD hash if detached."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=5,
        )
        branch = result.stdout.strip()
        if branch and branch != "HEAD":
            _branch_cache[repo_root] = branch
            return branch

        # detached HEAD - return short hash
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=5,
        )
        short_hash = result.stdout.strip()
        if short_hash:
            _branch_cache[repo_root] = f"detached:{short_hash}"
            return f"detached:{short_hash}"
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.debug("failed to get branch for %s: %s", repo_root, e)

    return _branch_cache.get(repo_root, "unknown")


def get_repo_info(file_path: str) -> dict[str, str] | None:
    """resolve repo name and branch for a changed file."""
    repo_root = get_repo_root(file_path)
    if repo_root is None:
        return None

    repo_name = os.path.basename(repo_root)
    branch = get_branch(repo_root)

    return {
        "repo": repo_name,
        "branch": branch,
    }


def has_dirty_worktree(repo_root: str) -> bool:
    """check if a repo has uncommitted changes (staged or unstaged)."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return bool(result.stdout.strip())
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.debug("failed to check git status for %s: %s", repo_root, e)
        return False


def invalidate_branch_cache(repo_root: str) -> None:
    """clear cached branch for a repo so it's re-read on next access."""
    _branch_cache.pop(repo_root, None)
