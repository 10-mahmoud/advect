"""CLI command tree for advect: push, pull, resume, init."""

import os
import socket
import sys

from face import Command, echo

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
        echo(f"\u2717 {msg}")
        sys.exit(1)
    ctx = detect_project()
    _ensure_handoff_ignored(ctx)


def push(posargs_):
    """Push current work to a remote machine and start an agent session.

    Positional args: [target] <message>
      target   Remote host (default: slate)
      message  What you're working on (required)
    """
    posargs = list(posargs_)
    if len(posargs) == 0:
        echo("\u2717 Message is required: advect push [target] \"what you're working on\"")
        sys.exit(1)
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
    echo("  \u2713 Handoff context written to .handoff.md")

    # 10. Determine remote path
    remote_path = _remote_project_path(ctx)
    echo(f"  Remote path: {remote_path}")

    # 11. Remote setup
    # Check if agent-env exists on target
    ae_check = _run(["ssh", target, "test -d ~/work/agent-env"])
    if ae_check.returncode == 0:
        ensure_agent_env(target)

    # Pull on remote
    worktree_basename = os.path.basename(ctx.worktree_dir) if ctx.is_worktree else ""
    pull_script = f"""
cd {remote_path} 2>/dev/null || {{
    cd ~/{ctx.parent_dir}/{ctx.name} 2>/dev/null && \
    git fetch origin && \
    git worktree add ../{worktree_basename} {ctx.branch}
    cd {remote_path}
}}
git fetch origin
git checkout {ctx.branch} 2>/dev/null || git checkout -b {ctx.branch} origin/{ctx.branch}
git pull --ff-only origin {ctx.branch} || git pull origin {ctx.branch}
"""
    try:
        ssh_run(target, pull_script)
        echo("  \u2713 Remote repo updated")
    except RuntimeError as e:
        echo(f"  \u2717 Remote pull failed: {e}")
        sys.exit(1)

    # Run project hooks if they exist
    _run(["ssh", target,
          f"test -x {remote_path}/.advect/on-arrive.sh && cd {remote_path} && ./.advect/on-arrive.sh"])

    # 12. Transfer handoff file
    scp_to(target, handoff_path, f"{remote_path}/.handoff.md")
    echo("  \u2713 Handoff file transferred")

    # 13. Start omp in tmux inside agent-env
    session = _make_session_name(ctx.name, ctx.branch)
    # Resolve container working dir (same path structure as host)
    container_path = remote_path.replace("~", "/home/dev")

    # Check if tmux session already exists
    tmux_check = _run([
        "ssh", target,
        f"docker exec agent-env tmux has-session -t {session} 2>/dev/null"
    ])
    if tmux_check.returncode == 0:
        echo(f"  \u26a0 tmux session '{session}' already exists on {target}. Skipping creation.")
    else:
        _run([
            "ssh", target,
            f"docker exec -u dev -w {container_path} agent-env tmux new-session -d -s {session} 'omp'"
        ])
        echo("  \u2713 omp session started: {session}")

    # 14. Summary
    echo("")
    echo(f"\u2713 Handoff complete: {ctx.name}/{ctx.branch} \u2192 {target}")
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
        echo(f"  \u2717 Remote commit/push failed: {e}")
        sys.exit(1)

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
        echo(f"\u2717 {msg}")
        sys.exit(1)

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


def main():
    cmd = Command(name="advect", func=None, doc="Rapid agentic work handoff between machines.")
    cmd.add(init_, name="init")
    cmd.add(push, name="push", posargs=True)
    cmd.add(pull, name="pull", posargs=True)
    cmd.add(resume, name="resume")
    cmd.run()
