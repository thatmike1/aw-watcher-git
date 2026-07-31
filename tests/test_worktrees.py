"""tests for worktree detection - pure git_utils, no aw deps needed."""

import subprocess
from pathlib import Path

import pytest

from aw_watcher_git.git_utils import get_worktree_main


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def main_repo(tmp_path: Path) -> Path:
    """a repo with one commit, so worktrees can be added from it."""
    repo = tmp_path / "project"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    (repo / "file.txt").write_text("hello")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")
    return repo


def test_main_checkout_is_not_a_worktree(main_repo: Path) -> None:
    assert get_worktree_main(str(main_repo)) is None


def test_linked_worktree_resolves_to_main(main_repo: Path, tmp_path: Path) -> None:
    worktree = tmp_path / "project-feature"
    _git(main_repo, "worktree", "add", "-q", str(worktree), "-b", "feature")

    assert get_worktree_main(str(worktree)) == str(main_repo.resolve())


def test_worktrees_of_one_repo_share_a_main(main_repo: Path, tmp_path: Path) -> None:
    first = tmp_path / "project-a"
    second = tmp_path / "project-b"
    _git(main_repo, "worktree", "add", "-q", str(first), "-b", "a")
    _git(main_repo, "worktree", "add", "-q", str(second), "-b", "b")

    assert get_worktree_main(str(first)) == get_worktree_main(str(second))


def test_submodule_is_not_collapsed(main_repo: Path, tmp_path: Path) -> None:
    """a submodule also has a .git file, but belongs to no worktree set."""
    outer = tmp_path / "outer"
    outer.mkdir()
    _git(outer, "init", "-q")
    _git(outer, "config", "user.email", "test@example.com")
    _git(outer, "config", "user.name", "test")
    _git(outer, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(main_repo), "sub")

    assert get_worktree_main(str(outer / "sub")) is None


def test_non_repo_directory(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert get_worktree_main(str(plain)) is None
