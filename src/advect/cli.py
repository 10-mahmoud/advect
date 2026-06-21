"""CLI command tree for advect: push, pull, resume, init."""

import os
import socket

from face import Command, Flag, UsageError, echo

from advect.core import (
    ProjectContext,
    check_git_repo,
    commit_wip,
    detect_project,
    generate_handoff,
    has_wip_commit,
    pull_branch,
    push_branch,
    run_preflight,
    run_sweep,
    sync_notes,
    unwrap_wip,
    write_handoff,
    _run,
)
from advect.remote import (
    ensure_agent_env,
    scp_from,
    scp_to,
    ssh_run,
)


def _ensure_handoff_ignored(ctx: ProjectContext, verbose: bool = True) -> None:
    """Ensure .handoff.md is gitignored in the current project."""
    res = _run(["git", "check-ignore", "-q", ".handoff.md"])
    if res.returncode == 0:
        return  # already ignored

    gitignore_path = os.path.join(ctx.root, ".gitignore")
    if os.path.exists(gitignore_path):
        with open(gitignore_path, "a") as f:
            f.write("\n# advect handoff\n.handoff.md\n")
    else:
        with open(gitignore_path, "w") as f:
            f.write("# advect handoff\n.handoff.md\n")

    if verbose:
        echo("\u2713 .handoff.md added to .gitignore")


def _make_session_name(project: str, branch: str) -> str:
    """Generate a tmux session name from project and branch."""
    name = f"{project}-{branch}".replace("/", "-")
    return name[:40]


def _remote_project_path(ctx: ProjectContext) -> str:
    """Determine remote project path from local context."""
    if ctx.is_worktree:
        return f"~/{ctx.parent_dir}/{os.path.basename(ctx.worktree_dir)}"
    return f"~/{ctx.parent_dir}/{ctx.name}"


def init_():
    """Initialize advect in the current project (adds .handoff.md to .gitignore)."""
    ok, msg = check_git_repo()
    if not ok:
        raise UsageError(msg)
    ctx = detect_project()
    _ensure_handoff_ignored(ctx)


def _push_remote(target: str, ctx: ProjectContext, handoff_path: str, force: bool = False) -> str:
    """Execute remote operations for push. Returns the tmux session name.

    Raises UsageError on any remote failure so the caller can roll back.
    """
    remote_path = _remote_project_path(ctx)
    echo(f"  Remote path: {remote_path}")

    # Agent-env setup
    ae_check = _run(["ssh", target, "test -d ~/work/agent-env"])
    if ae_check.returncode == 0:
        ensure_agent_env(target)

    # Pull on remote (clone if repo doesn't exist)
    worktree_basename = os.path.basename(ctx.worktree_dir) if ctx.is_worktree else ""
    clone_url = ctx.remote_url
    pull_script = f"""
set -e
if cd {remote_path} 2>/dev/null; then
    git fetch origin
    git checkout {ctx.branch} 2>/dev/null || git checkout -b {ctx.branch} origin/{ctx.branch}
    # If HEAD is an advect WIP commit, it's disposable — hard reset to origin
    if git log -1 --format=%s | grep -q '\\[advect:wip\\]'; then
        git reset --hard origin/{ctx.branch}
    else
        git pull --ff-only origin {ctx.branch} || git reset --hard origin/{ctx.branch}
    fi
elif [ -d ~/{ctx.parent_dir}/{ctx.name} ] && [ -n "{worktree_basename}" ]; then
    cd ~/{ctx.parent_dir}/{ctx.name}
    git fetch origin
    git worktree add ../{worktree_basename} {ctx.branch}
    cd {remote_path}
else
    mkdir -p ~/{ctx.parent_dir}
    git clone {clone_url} {remote_path}
    cd {remote_path}
    git checkout {ctx.branch} 2>/dev/null || true
fi
"""
    try:
        ssh_run(target, pull_script)
        echo("  ✓ Remote repo updated")
    except RuntimeError as e:
        raise UsageError(f"Remote pull failed: {e}")

    # Run project hooks if they exist
    _run(["ssh", target,
          f"test -x {remote_path}/.advect/on-arrive.sh && cd {remote_path} && ./.advect/on-arrive.sh"])

    # Transfer handoff file
    scp_to(target, handoff_path, f"{remote_path}/.handoff.md")
    echo("  ✓ Handoff file transferred")

    # Start omp in tmux inside agent-env
    session = _make_session_name(ctx.name, ctx.branch)
    container_path = remote_path.replace("~", "/home/dev")

    if force:
        _run(["ssh", target,
              f"docker exec agent-env tmux kill-session -t {session} 2>/dev/null"])

    tmux_check = _run([
        "ssh", target,
        f"docker exec agent-env tmux has-session -t {session} 2>/dev/null"
    ])
    if tmux_check.returncode == 0:
        echo(f"  ⚠ tmux session '{session}' already exists on {target}. Skipping creation.")
    else:
        _run([
            "ssh", target,
            f"docker exec -u dev -w {container_path} agent-env tmux new-session -d -s {session} 'omp'"
        ])
        echo(f"  ✓ omp session started: {session}")

    return session


def push(posargs_, force=False):
    """Push current work to a remote machine and start an agent session.

    Positional args: [target] <message>
      target   Remote host (default: slate)
      message  What you're working on (required)
    """
    posargs = list(posargs_)
    if len(posargs) == 0:
        raise UsageError("Message is required: advect push [target] \"what you're working on\"")
    elif len(posargs) == 1:
        target = "slate"
        message = posargs[0]
    else:
        target = posargs[0]
        message = " ".join(posargs[1:])

    echo(f"Pushing {os.path.basename(os.getcwd())} to {target}...")
    echo("")

    # 1. Preflight
    run_preflight(target)
    echo("")

    # 2. Detect project
    ctx = detect_project()
    echo(f"  Project: {ctx.name} ({ctx.branch})")

    # 3. Ensure .handoff.md is ignored
    _ensure_handoff_ignored(ctx, verbose=False)

    # 4. WIP commit if dirty
    wip = commit_wip(ctx, target)

    # 5. Push branch
    push_branch(ctx.branch)

    # 6. Sync notes
    sync_notes()

    # 7. Workstream sweep
    run_sweep()

    # 8-9. Generate and write handoff
    content = generate_handoff(ctx, message, wip)
    handoff_path = write_handoff(ctx, content)
    echo("  ✓ Handoff context written to .handoff.md")

    # 10-13: remote operations — rollback WIP on failure
    try:
        session = _push_remote(target, ctx, handoff_path, force=force)
    except UsageError:
        if wip:
            echo("")
            echo("  ⟲ Rolling back WIP commit...")
            unwrap_wip()
            res = _run(["git", "push", "--force-with-lease", "origin", ctx.branch])
            if res.returncode == 0:
                echo("  ⟲ Origin restored to pre-WIP state")
            else:
                echo(f"  ⚠ Could not restore origin. Manual fix: git push --force-with-lease origin {ctx.branch}")
        raise

    # 14. Summary
    echo("")
    echo(f"✓ Handoff complete: {ctx.name}/{ctx.branch} → {target}")
    echo("")
    echo(f"  Agent session: {session}")
    echo("")
    echo("  Reconnect:")
    echo(f"    ssh {target} -t \"docker exec -it -u dev agent-env tmux attach -t {session}\"")
    echo("")
    echo("  Or via dev.sh:")
    echo(f"    ssh {target}   # then: cd ~/work/agent-env && ./dev.sh")
    echo(f"    tmux attach -t {session}")


def pull(posargs_):
    """Pull in-progress work from a remote machine.

    Positional args: [remote]
      remote   Remote host (default: slate)
    """
    posargs = list(posargs_)
    remote = posargs[0] if posargs else "slate"

    echo(f"Pulling from {remote}...")
    echo("")

    # 1. Preflight
    run_preflight(remote)
    echo("")

    # 2. Local project context
    ctx = detect_project()
    echo(f"  Project: {ctx.name} ({ctx.branch})")

    # 3. Remote path
    remote_path = _remote_project_path(ctx)

    # 4. SSH into remote -- commit and push dirty state
    local_hostname = socket.gethostname()
    commit_script = f"""
cd {remote_path} || exit 1
if [ -n "$(git status --porcelain)" ]; then
    git add -A
    git commit -m "[skip ci] [advect:wip] $(hostname) \u2192 {local_hostname}: $(git branch --show-current)"
    git push origin $(git branch --show-current)
    echo "WIP_COMMITTED=true"
else
    echo "WIP_COMMITTED=false"
fi
"""
    try:
        res = ssh_run(remote, commit_script)
        wip_committed = "WIP_COMMITTED=true" in res.stdout
        if wip_committed:
            echo("  \u2713 Remote WIP committed and pushed")
        else:
            echo("  \u2713 Remote is clean")
    except RuntimeError as e:
        raise UsageError(f"Remote commit/push failed: {e}")

    # 5. SSH into remote -- sync notes and sweep
    notes_script = """
NOTES_DIR=$(echo "$WS_DIR" | sed 's|/_Workstreams$||')
[ -z "$NOTES_DIR" ] && NOTES_DIR=~/work/10notes
if [ -d "$NOTES_DIR" ] && git -C "$NOTES_DIR" rev-parse 2>/dev/null; then
    cd "$NOTES_DIR"
    if [ -n "$(git status --porcelain)" ]; then
        git add -A && git commit -m "[advect] notes sync" && git push origin master
    fi
fi
command -v ws >/dev/null && ws sweep --no-review 2>&1 || true
# Re-sync notes after sweep
if [ -d "$NOTES_DIR" ] && git -C "$NOTES_DIR" rev-parse 2>/dev/null; then
    cd "$NOTES_DIR"
    if [ -n "$(git status --porcelain)" ]; then
        git add -A && git commit -m "[advect] ws sweep results" && git push origin master
    fi
fi
"""
    try:
        ssh_run(remote, notes_script, check=False)
        echo("  \u2713 Remote notes synced")
    except RuntimeError:
        echo("  \u26a0 Remote notes sync had issues (continuing)")

    # 6. SSH into remote -- generate handoff
    wip_text = "yes" if wip_committed else "no"
    handoff_script = f"""cd {remote_path}
cat > .handoff.md <<'HANDOFF_EOF'
# Handoff: {ctx.name} / $(git branch --show-current)

**From:** $(hostname) at $(date -Iseconds)
**Branch:** $(git branch --show-current)
**WIP commit:** {wip_text}

## Changed files
$(git diff --stat HEAD~1 2>/dev/null || echo "n/a")

## Recent commits
$(git log --oneline -5)
HANDOFF_EOF
"""
    try:
        ssh_run(remote, handoff_script)
    except RuntimeError:
        echo("  \u26a0 Could not generate handoff on remote (continuing)")

    # 7. SCP handoff from remote
    local_handoff = os.path.join(ctx.root, ".handoff.md")
    try:
        scp_from(remote, f"{remote_path}/.handoff.md", local_handoff)
        echo("  \u2713 Handoff file retrieved")
    except SystemExit:
        echo("  \u26a0 Could not retrieve handoff file (continuing)")

    # 8. Pull locally
    pull_branch(ctx.branch)

    # 9. Sync notes locally
    sync_notes()

    # 10. Print handoff
    echo("")
    if os.path.exists(local_handoff):
        with open(local_handoff) as f:
            echo(f.read())
    echo("")

    # 11. Offer to unwrap WIP
    if wip_committed and has_wip_commit():
        response = input("Unwrap WIP commit? [Y/n] ").strip().lower()
        if response in ("", "y", "yes"):
            unwrap_wip()


def resume():
    """Resume work after a manual pull. Shows handoff context and unwraps WIP if present."""
    ok, msg = check_git_repo()
    if not ok:
        raise UsageError(msg)

    ctx = detect_project()
    handoff_path = os.path.join(ctx.root, ".handoff.md")
    found_something = False

    if os.path.exists(handoff_path):
        with open(handoff_path) as f:
            echo(f.read())
        found_something = True

    if has_wip_commit():
        response = input("Unwrap WIP commit? [Y/n] ").strip().lower()
        if response in ("", "y", "yes"):
            unwrap_wip()
        found_something = True

    if not found_something:
        echo("No handoff state found. Nothing to resume.")


def _get_skill_source() -> str:
    """Return the path to SKILL.md shipped with this package."""
    return os.path.join(os.path.dirname(__file__), "skill", "SKILL.md")


_OMP_SKILL_DIR = os.path.expanduser("~/.omp/agent/skills/advect")
_OMP_SKILL_PATH = os.path.join(_OMP_SKILL_DIR, "SKILL.md")


def setup():
    """Install or update the advect omp skill (symlinks into ~/.omp/agent/skills/)."""
    source = _get_skill_source()
    if not os.path.exists(source):
        raise UsageError(f"Skill source not found at {source}")

    # Check current state
    if os.path.islink(_OMP_SKILL_PATH):
        current_target = os.readlink(_OMP_SKILL_PATH)
        if os.path.realpath(current_target) == os.path.realpath(source):
            echo(f"\u2713 Skill already installed and up to date")
            echo(f"  {_OMP_SKILL_PATH} \u2192 {source}")
            return
        # Stale symlink — remove and re-create
        os.unlink(_OMP_SKILL_PATH)
        echo(f"  Updated symlink (was \u2192 {current_target})")
    elif os.path.exists(_OMP_SKILL_PATH):
        # Regular file — back it up and replace with symlink
        backup = _OMP_SKILL_PATH + ".bak"
        os.rename(_OMP_SKILL_PATH, backup)
        echo(f"  Backed up existing file to {backup}")

    os.makedirs(_OMP_SKILL_DIR, exist_ok=True)
    os.symlink(source, _OMP_SKILL_PATH)
    echo(f"\u2713 Skill installed")
    echo(f"  {_OMP_SKILL_PATH} \u2192 {source}")

def main():
    cmd = Command(name="advect", func=None, doc="Rapid agentic work handoff between machines.")
    cmd.add(init_, name="init")
    cmd.add(setup, name="setup")
    push_cmd = Command(push, name="push", posargs=True)
    push_cmd.add(Flag('--force', char='-f', parse_as=True, missing=False,
                       doc='Force: kill existing tmux session, reset remote state'))
    cmd.add(push_cmd)
    cmd.add(pull, name="pull", posargs=True)
    cmd.add(resume, name="resume")
    cmd.run()
