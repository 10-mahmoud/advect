# advect

Rapid agentic work handoff between machines over [Tailscale](https://tailscale.com/).

When you're mid-feature and need to close the laptop, `advect push` commits your dirty state, pushes the branch, syncs notes, and launches an [omp](https://github.com/mahmoud/omp) agent session on the remote in under 2 minutes. `advect pull` does the reverse — commits remote work, pulls it down, and offers to unwrap the WIP commit.

## Install

```bash
pipx install git+https://github.com/10-mahmoud/advect.git
```

Or for development:

```bash
git clone git@github.com:10-mahmoud/advect.git
cd advect
poetry install
```

## Prerequisites

- [Tailscale](https://tailscale.com/) running on both machines
- SSH access to the remote host (via `~/.ssh/config`)
- Git repo with an `origin` remote
- [agent-env](https://github.com/mahmoud/agent-env) on the remote (optional, for tmux/omp session)

## Usage

### Push work to a remote

```bash
advect push glob "finishing the auth refactor"
```

The default target is `glob`. Any Tailscale-reachable hostname works:

```bash
advect push myserver "rebasing the auth flow"
```

What happens:
1. Preflight checks — Tailscale running, host reachable, SSH works, in a git repo
2. WIP commit — `git add -A && git commit` with `[skip ci] [advect:wip]` sentinel
3. Push branch to origin
4. Sync notes repo (if `$WS_DIR` or `$ADVECT_NOTES_DIR` is set) and run `ws sweep --no-review` (if `ws` is installed)
5. Generate `.handoff.md` with context, changed files, recent commits, active plans, open PRs
6. Pull the branch on the remote
7. Run `.advect/on-arrive.sh` hook (if present in the project)
8. Transfer `.handoff.md` to the remote
9. Start `omp` in a tmux session inside agent-env

### Pull work back

```bash
advect pull glob
```

Driven entirely from the laptop — no advect needed on the remote. Commits any dirty state on the remote, pushes, pulls locally, and offers to unwrap the WIP commit.

### Resume

```bash
advect resume
```

For use after a manual `git pull`. Shows the `.handoff.md` and offers to unwrap any WIP commit at HEAD.

### Init

```bash
advect init
```

Adds `.handoff.md` to the project's `.gitignore`.

## WIP commits

advect uses a sentinel format for WIP commits:

```
[skip ci] [advect:wip] macbook → glob: feat/auth
```

- `[skip ci]` prevents GitHub Actions from running on the WIP push
- `[advect:wip]` lets advect detect and unwrap the commit with `git reset HEAD~1`

The unwrap preserves all changes in the working tree.

## Project hooks

Create `.advect/on-arrive.sh` (executable) in your repo for project-specific post-pull setup. It runs on the remote after `advect push` pulls the branch. Commit it to the repo — it's project config, not ephemeral state.

Example (worktree .env check):

```bash
#!/bin/bash
if [ "$(git rev-parse --git-common-dir)" != "$(git rev-parse --git-dir)" ]; then
  [ ! -f .env ] && echo "⚠ Worktree missing .env"
fi
```

## How it works

```
laptop                          remote (glob)
──────                          ─────────────
advect push glob "msg"
  ├─ preflight checks ─────────── tailscale ping, ssh
  ├─ git add -A && commit [wip]
  ├─ git push origin branch
  ├─ sync notes, ws sweep
  ├─ generate .handoff.md
  ├─ ssh glob ──────────────────── git fetch && checkout && pull
  ├─ scp .handoff.md ──────────── .handoff.md lands in project root
  └─ ssh glob ──────────────────── tmux new-session 'omp'

advect pull glob
  ├─ preflight checks
  ├─ ssh glob ──────────────────── git add -A && commit [wip] && push
  ├─ ssh glob ──────────────────── sync notes, ws sweep
  ├─ scp .handoff.md ◄──────────── retrieve handoff
  ├─ git pull --ff-only
  └─ unwrap WIP? [Y/n]
```
