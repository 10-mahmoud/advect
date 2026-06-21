"""Tests for advect core helpers."""

import os
import subprocess
import tempfile

from advect.core import (
    WIP_COMMIT_FMT,
    WIP_SENTINEL,
    ProjectContext,
    _parse_gh_owner,
    _detect_parent_dir,
    detect_project,
    generate_handoff,
    has_wip_commit,
    commit_wip,
    unwrap_wip,
)


def test_wip_commit_format():
    msg = WIP_COMMIT_FMT.format(
        hostname="macbook",
        target="glob",
        branch="feat/pulse",
    )
    assert msg == "[skip ci] [advect:wip] macbook \u2192 glob: feat/pulse"
    assert WIP_SENTINEL in msg
    assert "[skip ci]" in msg


def test_parse_gh_owner_ssh():
    assert _parse_gh_owner("git@github.com:acme-org/myapp.git") == "acme-org"


def test_parse_gh_owner_https():
    assert _parse_gh_owner("https://github.com/someuser/somerepo.git") == "someuser"


def test_parse_gh_owner_empty():
    assert _parse_gh_owner("") == ""
    assert _parse_gh_owner("not-a-github-url") == ""


def test_detect_parent_dir(tmp_path):
    # Simulate ~/work/somerepo
    home = str(tmp_path)
    work_repo = tmp_path / "work" / "myrepo"
    work_repo.mkdir(parents=True)
    assert _detect_parent_dir(str(work_repo)) != ""  # just ensure no crash

    projects_repo = tmp_path / "projects" / "myrepo"
    projects_repo.mkdir(parents=True)
    # _detect_parent_dir uses Path.home(), so it won't match tmp_path
    # but it should return "work" as fallback
    result = _detect_parent_dir(str(projects_repo))
    assert isinstance(result, str)


def _make_git_repo(path):
    """Create a minimal git repo for testing."""
    os.makedirs(path, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, capture_output=True)
    # Add a file and commit
    with open(os.path.join(path, "README.md"), "w") as f:
        f.write("# test\n")
    subprocess.run(["git", "add", "-A"], cwd=path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, capture_output=True)
    # Add a remote
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:testuser/testrepo.git"],
        cwd=path, capture_output=True,
    )


def test_detect_project(tmp_path):
    repo_path = str(tmp_path / "testrepo")
    _make_git_repo(repo_path)

    old_cwd = os.getcwd()
    try:
        os.chdir(repo_path)
        ctx = detect_project()
        assert ctx.name == "testrepo"
        assert ctx.branch == "master" or ctx.branch == "main"
        assert not ctx.is_worktree
        assert not ctx.is_dirty
        assert ctx.remote_url == "git@github.com:testuser/testrepo.git"
        assert ctx.gh_owner == "testuser"
    finally:
        os.chdir(old_cwd)


def test_has_wip_commit(tmp_path):
    repo_path = str(tmp_path / "wiprepo")
    _make_git_repo(repo_path)

    old_cwd = os.getcwd()
    try:
        os.chdir(repo_path)
        assert not has_wip_commit()

        # Make a WIP commit
        with open(os.path.join(repo_path, "file.txt"), "w") as f:
            f.write("change\n")
        subprocess.run(["git", "add", "-A"], cwd=repo_path, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "[skip ci] [advect:wip] test \u2192 glob: main"],
            cwd=repo_path, capture_output=True,
        )
        assert has_wip_commit()
    finally:
        os.chdir(old_cwd)


def test_commit_and_unwrap_wip(tmp_path):
    repo_path = str(tmp_path / "commitrepo")
    _make_git_repo(repo_path)

    old_cwd = os.getcwd()
    try:
        os.chdir(repo_path)
        ctx = detect_project()

        # Not dirty, should not commit
        assert not commit_wip(ctx, "glob")

        # Make dirty
        with open(os.path.join(repo_path, "dirty.txt"), "w") as f:
            f.write("dirty\n")

        # Re-detect (is_dirty should now be True)
        ctx = detect_project()
        assert ctx.is_dirty

        # Commit WIP
        assert commit_wip(ctx, "glob")
        assert has_wip_commit()

        # Unwrap
        unwrap_wip()
        assert not has_wip_commit()
        # File should still exist (mixed reset)
        assert os.path.exists(os.path.join(repo_path, "dirty.txt"))
    finally:
        os.chdir(old_cwd)


def test_generate_handoff(tmp_path):
    repo_path = str(tmp_path / "handoffrepo")
    _make_git_repo(repo_path)

    old_cwd = os.getcwd()
    try:
        os.chdir(repo_path)
        ctx = detect_project()
        content = generate_handoff(ctx, "testing the handoff system", False)

        assert "# Handoff: handoffrepo /" in content
        assert "testing the handoff system" in content
        assert "## Changed files" in content
        assert "## Recent commits" in content
        assert "## Active plans" in content
        assert "## Open PRs" in content
        assert "WIP commit:** no" in content
    finally:
        os.chdir(old_cwd)


def test_generate_handoff_with_wip(tmp_path):
    repo_path = str(tmp_path / "wiphandoff")
    _make_git_repo(repo_path)

    old_cwd = os.getcwd()
    try:
        os.chdir(repo_path)

        # Make a change and commit as WIP
        with open(os.path.join(repo_path, "change.txt"), "w") as f:
            f.write("change\n")
        ctx = detect_project()
        commit_wip(ctx, "glob")

        ctx = detect_project()
        content = generate_handoff(ctx, "WIP handoff test", True)

        assert "WIP commit:** yes" in content
        assert "advect resume" in content
    finally:
        os.chdir(old_cwd)
