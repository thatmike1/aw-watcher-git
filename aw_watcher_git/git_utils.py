"""utilities for resolving git repo info from file paths."""

import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_repo_root_cache: dict[str, str] = {}
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


def get_worktree_main(repo_path: str) -> str | None:
    """return the main checkout's path if repo_path is a linked worktree, else None.

    every worktree of a repo shares one .git directory, and
    `git rev-parse --git-common-dir` points at it, so its parent is the main
    checkout. this lets sibling worktrees of one project collapse to a single
    repo identity without any per-project config.

    returns None for main checkouts, submodules (their common dir lives under
    .git/modules), bare repos, and anything git can't resolve.
    """
    # linked worktrees and submodules use a .git *file*; a main checkout has a
    # directory, so this skips the subprocess for the common case
    if not (Path(repo_path) / ".git").is_file():
        return None

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.debug("failed to resolve common dir for %s: %s", repo_path, e)
        return None

    common = result.stdout.strip()
    if not common:
        return None

    common_path = Path(common)
    if not common_path.is_absolute():
        common_path = Path(repo_path) / common_path
    try:
        common_path = common_path.resolve()
    except OSError:
        return None

    # a submodule's common dir is <super>/.git/modules/<name>, a bare repo's is
    # the repo itself - neither has a main checkout to attribute to
    if common_path.name != ".git":
        return None

    main = common_path.parent
    if main == Path(repo_path).resolve():
        return None
    return str(main)


def get_repo_root(file_path: str) -> str | None:
    """given a file path, find the git repo root it belongs to. roots are cached."""
    path = Path(file_path).resolve()

    for parent in [path] + list(path.parents):
        parent_str = str(parent)
        if parent_str in _repo_root_cache:
            return _repo_root_cache[parent_str]

        git_dir = parent / ".git"
        if git_dir.is_dir() or git_dir.is_file():
            _repo_root_cache[parent_str] = parent_str
            return parent_str

    # negative results are not cached: watched paths almost always resolve to a
    # repo, and caching per-file misses would grow without bound
    return None


def get_branch(repo_root: str) -> str:
    """get the current branch name for a repo, cached until the repo's .git changes.

    the cache is invalidated by invalidate_branch_cache() when a write inside
    .git/ is observed (branch switch, checkout), so a hit is always current.
    returns HEAD short hash if detached, "unknown" if git fails with no cache.
    """
    cached = _branch_cache.get(repo_root)
    if cached is not None:
        return cached

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
            detached = f"detached:{short_hash}"
            _branch_cache[repo_root] = detached
            return detached
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.debug("failed to get branch for %s: %s", repo_root, e)

    return "unknown"


def invalidate_branch_cache(repo_root: str) -> None:
    """clear cached branch for a repo so it's re-read on next access."""
    _branch_cache.pop(repo_root, None)
