"""Shared helpers: preflight checks, git ops, context generation."""

import os
import re
import socket
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# -- WIP commit sentinel --

WIP_SENTINEL = "[advect:wip]"
WIP_COMMIT_FMT = "[skip ci] [advect:wip] {hostname} \u2192 {target}: {branch}"


# -- Preflight checks --

def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def check_tailscale() -> tuple[bool, str]:
    res = _run(["tailscale", "status", "--json"])
    if res.returncode != 0:
        return False, "Tailscale is not running. Start it with: tailscale up"
    return True, "Tailscale is running"


def check_host_reachable(host: str) -> tuple[bool, str]:
    res = _run(["tailscale", "ping", "-c", "1", "--timeout", "3s", host])
    if res.returncode != 0:
        return False, f"Cannot reach {host} on tailnet. Run: tailscale status"
    return True, f"{host} is reachable"


def check_ssh(host: str) -> tuple[bool, str]:
    res = _run(["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", host, "echo", "ok"])
    if res.returncode != 0:
        return False, f"SSH to {host} failed. Check ~/.ssh/config and tailscale auth"
    return True, f"SSH to {host} ok"


def check_git_repo() -> tuple[bool, str]:
    res = _run(["git", "rev-parse", "--show-toplevel"])
    if res.returncode != 0:
        return False, "Not in a git repository"
    return True, "Git repository detected"


def run_preflight(host: str) -> None:
    """Run all preflight checks in order, exit on first failure."""
    checks = [
        ("Tailscale", check_tailscale),
        ("Host reachable", lambda: check_host_reachable(host)),
        ("SSH", lambda: check_ssh(host)),
        ("Git repo", check_git_repo),
    ]
    for label, check_fn in checks:
        ok, msg = check_fn()
        symbol = "\u2713" if ok else "\u2717"
        print(f"  {symbol} {label}: {msg}")
        if not ok:
            sys.exit(1)


# -- Git context detection --

@dataclass
class ProjectContext:
    root: str           # git rev-parse --show-toplevel
    name: str           # basename of root
    branch: str         # git branch --show-current
    is_worktree: bool   # git-common-dir != git-dir
    worktree_dir: str   # actual cwd if worktree, else root
    is_dirty: bool      # git status --porcelain non-empty
    remote_url: str     # git remote get-url origin
    gh_owner: str       # parsed from remote_url
    parent_dir: str     # "work" or "projects" parsed from root path


def _git(*args: str) -> str:
    res = _run(["git"] + list(args))
    return res.stdout.strip()


def _parse_gh_owner(remote_url: str) -> str:
    """Extract GitHub owner from remote URL.

    Handles:
      git@github.com:owner/repo.git
      https://github.com/owner/repo.git
    """
    m = re.search(r"github\.com[:/]([^/]+)/", remote_url)
    return m.group(1) if m else ""


def _detect_parent_dir(root: str) -> str:
    """Determine parent_dir from root path relative to home."""
    home = str(Path.home())
    rel = os.path.relpath(root, home)
    parts = rel.split(os.sep)
    if parts and parts[0] in ("work", "projects", "hatnote"):
        return parts[0]
    return "work"  # default


def detect_project() -> ProjectContext:
    root = _git("rev-parse", "--show-toplevel")
    if not root:
        print("\u2717 Not in a git repository")
        sys.exit(1)

    common_dir = _git("rev-parse", "--git-common-dir")
    git_dir = _git("rev-parse", "--git-dir")
    # Resolve to absolute paths for comparison
    common_abs = os.path.realpath(os.path.join(root, common_dir))
    git_abs = os.path.realpath(os.path.join(root, git_dir))
    is_worktree = common_abs != git_abs

    branch = _git("branch", "--show-current")
    is_dirty = bool(_git("status", "--porcelain"))
    remote_url = _git("remote", "get-url", "origin")

    return ProjectContext(
        root=root,
        name=os.path.basename(root),
        branch=branch,
        is_worktree=is_worktree,
        worktree_dir=os.getcwd() if is_worktree else root,
        is_dirty=is_dirty,
        remote_url=remote_url,
        gh_owner=_parse_gh_owner(remote_url),
        parent_dir=_detect_parent_dir(root),
    )


# -- WIP commit management --

def commit_wip(ctx: ProjectContext, target: str) -> bool:
    """Commit all dirty state as a WIP commit. Returns True if a commit was made."""
    if not ctx.is_dirty:
        return False
    msg = WIP_COMMIT_FMT.format(
        hostname=socket.gethostname(),
        target=target,
        branch=ctx.branch,
    )
    _run(["git", "add", "-A"])
    _run(["git", "commit", "-m", msg])
    print(f"  \u2713 WIP commit: {msg}")
    return True


def has_wip_commit(branch: Optional[str] = None) -> bool:
    """Check if HEAD commit message contains the advect WIP sentinel."""
    msg = _git("log", "-1", "--format=%s")
    return WIP_SENTINEL in msg


def unwrap_wip() -> None:
    """Mixed reset HEAD~1, keeping changes in working tree."""
    _run(["git", "reset", "HEAD~1"])
    print("  \u2713 WIP commit unwrapped (changes preserved in working tree)")


# -- Branch push/pull --

def push_branch(branch: str) -> None:
    res = _run(["git", "push", "origin", branch])
    if res.returncode != 0:
        # Try setting upstream
        res2 = _run(["git", "push", "-u", "origin", branch])
        if res2.returncode != 0:
            print(f"  \u2717 Failed to push branch: {res2.stderr.strip()}")
            sys.exit(1)
        print(f"  \u2713 Pushed {branch} (set upstream)")
    else:
        print(f"  \u2713 Pushed {branch}")


def pull_branch(branch: str) -> None:
    res = _run(["git", "pull", "--ff-only", "origin", branch])
    if res.returncode != 0:
        print(f"  \u26a0 Fast-forward pull failed, doing regular pull")
        res2 = _run(["git", "pull", "origin", branch])
        if res2.returncode != 0:
            print(f"  \u2717 Pull failed: {res2.stderr.strip()}")
            sys.exit(1)
    print(f"  \u2713 Pulled {branch}")


# -- 10notes sync --

def _resolve_notes_dir() -> str:
    ws_dir = os.environ.get("WS_DIR", "")
    if ws_dir:
        # Strip /_Workstreams suffix
        notes = re.sub(r"/_Workstreams$", "", ws_dir)
        if os.path.isdir(notes):
            return notes
    fallback = os.path.expanduser("~/work/10notes")
    if os.path.isdir(fallback):
        return fallback
    return ""


def sync_notes(notes_dir: Optional[str] = None) -> None:
    if notes_dir is None:
        notes_dir = _resolve_notes_dir()
    if not notes_dir or not os.path.isdir(notes_dir):
        print("  \u26a0 Notes directory not found, skipping sync")
        return

    # Check if it's a git repo
    res = _run(["git", "rev-parse", "--is-inside-work-tree"], cwd=notes_dir)
    if res.returncode != 0:
        print(f"  \u26a0 {notes_dir} is not a git repo, skipping sync")
        return

    # Check if dirty
    status = _run(["git", "status", "--porcelain"], cwd=notes_dir)
    if not status.stdout.strip():
        return  # Clean, nothing to sync

    _run(["git", "add", "-A"], cwd=notes_dir)
    _run(["git", "commit", "-m", "[advect] notes sync"], cwd=notes_dir)
    _run(["git", "push", "origin", "master"], cwd=notes_dir)
    print("  \u2713 Notes synced")


# -- Workstream sweep --

def run_sweep() -> None:
    import shutil
    if not shutil.which("ws"):
        print("  \u26a0 ws not found, skipping workstream sweep")
        return

    print("  \u2299 Running workstream sweep...")
    res = _run(["ws", "sweep", "--no-review"])
    if res.returncode != 0:
        print(f"  \u26a0 ws sweep had issues: {res.stderr.strip()[:200]}")
    else:
        print("  \u2713 Workstream sweep complete")

    # Re-sync notes after sweep (sweep may have updated workstream files)
    sync_notes()


# -- Handoff context generation --

def generate_handoff(ctx: ProjectContext, message: str, wip_committed: bool) -> str:
    """Generate markdown content for .handoff.md."""
    hostname = socket.gethostname()
    timestamp = datetime.now(timezone.utc).isoformat()

    # Changed files
    if wip_committed:
        diff_stat = _git("diff", "--stat", "HEAD~1")
    else:
        diff_stat = _git("diff", "--stat", f"origin/{ctx.branch}..HEAD")
    if not diff_stat:
        diff_stat = "(no changes)"

    # Recent commits
    recent = _git("log", "--oneline", "-5")
    if not recent:
        recent = "(no commits)"

    # WIP text
    if wip_committed:
        wip_text = "yes \u2014 run `advect resume` or `git reset HEAD~1` to unwrap"
    else:
        wip_text = "no"

    # Active plans
    plans_text = _gather_plans(ctx.root)

    # Open PRs
    prs_text = _gather_prs(ctx)

    return f"""# Handoff: {ctx.name} / {ctx.branch}

**From:** {hostname} at {timestamp}
**Branch:** {ctx.branch}
**WIP commit:** {wip_text}

## Context

{message}

## Changed files

{diff_stat}

## Recent commits

{recent}

## Active plans

{plans_text}

## Open PRs

{prs_text}
"""


def _gather_plans(root: str) -> str:
    """List .plans/*.md and .workstream-plans/*.md with title/status."""
    lines = []
    for dirname in (".plans", ".workstream-plans"):
        plan_dir = os.path.join(root, dirname)
        if not os.path.isdir(plan_dir):
            continue
        for fname in sorted(os.listdir(plan_dir)):
            if not fname.endswith(".md"):
                continue
            fpath = os.path.join(plan_dir, fname)
            title, status = _parse_plan_frontmatter(fpath)
            if title:
                status_str = f" ({status})" if status else ""
                lines.append(f"- {title}{status_str}")
    return "\n".join(lines) if lines else "none found"


def _parse_plan_frontmatter(path: str) -> tuple[str, str]:
    """Extract title and status from YAML frontmatter."""
    try:
        with open(path) as f:
            content = f.read(2048)  # Only need the front
    except OSError:
        return "", ""

    if not content.startswith("---"):
        return os.path.basename(path), ""

    end = content.find("\n---", 3)
    if end == -1:
        return os.path.basename(path), ""

    fm = content[3:end]
    title = ""
    status = ""
    for line in fm.split("\n"):
        line = line.strip()
        if line.startswith("title:"):
            title = line[6:].strip().strip("\"'")
        elif line.startswith("status:"):
            status = line[7:].strip().strip("\"'")
    return title or os.path.basename(path), status


def _gather_prs(ctx: ProjectContext) -> str:
    """List open PRs for the current branch via gh CLI."""
    import shutil
    if not shutil.which("gh"):
        return "unavailable (gh not installed)"
    if ctx.gh_owner != "10-mahmoud":
        return "unavailable (personal repo or gh not configured)"

    res = _run([
        "gh", "pr", "list",
        "--head", ctx.branch,
        "--json", "title,url,state",
        "--limit", "3",
        "--jq", '.[] | "- [\\(.title)](\\(.url)) (\\(.state))"',
    ])
    if res.returncode != 0 or not res.stdout.strip():
        return "none"
    return res.stdout.strip()


def write_handoff(ctx: ProjectContext, content: str) -> str:
    """Write .handoff.md to project root. Returns the path."""
    path = os.path.join(ctx.root, ".handoff.md")
    with open(path, "w") as f:
        f.write(content)
    return path
