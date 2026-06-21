---
name: advect
description: |
  Hand off in-progress work to a remote machine (or pull it back) using advect.
  Use when asked to "hand off", "push to slate", "transfer work", "advect push",
  "close the laptop", "continue on slate", "pull from slate", or "resume".
  Covers the full push/pull/resume lifecycle.
version: 0.1.0
---

# advect — Agentic Work Handoff

Hand off in-progress work between machines over Tailscale + SSH. The `advect`
CLI commits dirty state, pushes the branch, syncs notes, generates a handoff
context file, pulls the branch on the remote, and starts an omp session in
tmux inside agent-env.

## When to activate

User says anything like:
- "hand this off to slate"
- "push to slate" / "advect push"
- "I need to close the laptop"
- "continue this on slate" / "transfer to slate"
- "pull from slate" / "advect pull"
- "resume" / "advect resume"

## Commands

### Push (hand off to remote)

```bash
advect push [target] "<message>"
```

- `target` defaults to `slate` (any Tailscale hostname works)
- `message` is **required** — a concise description of what's in progress and
  what needs to happen next. This is the main context the receiving agent gets.

**Write a good message.** The message goes into `.handoff.md` which the
receiving omp session reads. Include:
1. What you were working on (the goal)
2. Where you left off (what's done, what's not)
3. What to do next (specific next steps)
4. Any blockers or context the next agent needs

Example:

```bash
advect push slate "Implementing pulse AM/PM cron jobs. The scheduler is done and tested. Still need to: 1) wire up the email template for PM digest, 2) add the render cron job config. The PM template should mirror the AM one in fsrv/email/templates/."
```

### Pull (bring work back from remote)

```bash
advect pull [remote]
```

- Driven entirely from the laptop — no advect needed on the remote
- Commits any dirty state on the remote, pushes, pulls locally
- Displays the handoff context
- Offers to unwrap the WIP commit (`git reset HEAD~1`)

### Resume (after manual pull)

```bash
advect resume
```

- Shows `.handoff.md` if present
- Offers to unwrap WIP commit at HEAD if detected

## What push does (full sequence)

1. Preflight checks — Tailscale, SSH, git repo
2. WIP commit — `git add -A && git commit` with `[skip ci] [advect:wip]` sentinel
3. Push branch to origin
4. Sync notes repo (if `$WS_DIR` or `$ADVECT_NOTES_DIR` is set)
5. Run `ws sweep --no-review` (if `ws` is on PATH)
6. Generate `.handoff.md` with context, changed files, recent commits, plans, PRs
7. Pull the branch on the remote via SSH
8. Run `.advect/on-arrive.sh` hook on remote (if present)
9. Transfer `.handoff.md` to remote
10. Start `omp` in a tmux session inside agent-env

## How to execute a handoff from inside a session

When the user asks you to hand off, do this:

1. **Synthesize the message.** Summarize the current session: what was the goal,
   what's been done, what remains, and any important context. Be specific — file
   names, function names, test results, decisions made. This is the most important
   part. The message should be self-contained enough that a fresh agent can pick
   up the work.

2. **Run the command:**
   ```bash
   advect push slate "<your synthesized message>"
   ```
   If the user specified a different target, use that instead.

3. **Report the result.** Show the user the summary output — session name,
   reconnect command, etc.

## WIP commits

advect uses `[skip ci] [advect:wip]` in commit messages:
- `[skip ci]` prevents CI from running on the WIP push
- `[advect:wip]` is the sentinel for detecting and unwrapping (`git reset HEAD~1`)

## Project hooks

Projects can have `.advect/on-arrive.sh` (executable, committed to repo) for
post-pull setup on the remote. It runs automatically after the remote pull.

## Environment variables

| Variable | Purpose |
|---|---|
| `WS_DIR` | Workstream directory; parent is used as notes repo |
| `ADVECT_NOTES_DIR` | Explicit notes repo path (fallback if `WS_DIR` unset) |

## Troubleshooting

- **Tailscale not running:** `tailscale up`
- **SSH fails:** Check `~/.ssh/config` for the target host
- **agent-env not functional:** SSH to remote, `cd ~/work/agent-env && ./build.sh && ./dev.sh --recreate -d`
- **tmux session already exists:** advect warns and skips creation; kill manually with `tmux kill-session -t <name>` if needed
