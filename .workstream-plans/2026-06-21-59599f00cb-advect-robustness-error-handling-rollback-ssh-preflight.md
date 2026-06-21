---
id: 2026-06-21-59599f00cb
title: "advect robustness: error handling, rollback, SSH preflight"
status: draft
workstream: ""
repo: advect
created: 2026-06-21
updated: 2026-06-21
tabled_note: ""
---
# advect robustness: error handling, rollback, SSH preflight

## Context

During the first real-world test of `advect push`, four issues were discovered: (1) `sys.exit(1)` throughout core.py and remote.py makes rollback/cleanup impossible, (2) partial push failures (WIP committed + pushed to origin, but remote pull fails) leave orphaned state with no cleanup, (3) SSH preflight fails on first Tailscale SSH connection because `BatchMode=yes` suppresses the interactive auth check, and (4) no `--force` flag for retries after partial failure. The WIP commit is already at HEAD with quick fixes for auto-clone and remote WIP divergence; this plan addresses the deeper structural issues on top of that.

## Approach

### Step 1: Replace `sys.exit(1)` with `UsageError` in core.py and remote.py

**Independent.** All existing tests continue to pass after this step because tests don't call these error paths through `face`.

The `face` CLI framework provides `UsageError(msg, code=1)` — a subclass of `CommandLineError(FaceException, SystemExit)`. When raised inside a `Command.run()` handler, face catches it, prints a readable error, and exits. When uncaught, it acts like `SystemExit`. This is the idiomatic face error path.

**In `src/advect/core.py`:**

- `run_preflight` (line 67): Replace `sys.exit(1)` with `raise UsageError(msg)`. The `msg` is already formatted on line 65 (`f"  {symbol} {label}: {msg}"`). Keep the `echo()` for the check mark, raise after. Add `from face import UsageError` to imports (line 3 area — existing imports are `os`, `re`, `socket`, `subprocess`, `sys`, `dataclasses`, `pathlib`, `typing`).
- `detect_project` (line 115): Replace `sys.exit(1)` with `raise UsageError("Not in a git repository")`. Remove the `print(...)` on line 114 — `UsageError` handles the message.
- `push_branch` (line 179): Replace `sys.exit(1)` with `raise UsageError(f"Failed to push branch: {res2.stderr.strip()}")`. Remove the `print(...)` on line 178.
- `pull_branch` (line 192): Replace `sys.exit(1)` with `raise UsageError(f"Pull failed: {res2.stderr.strip()}")`. Remove the `print(...)` on line 191.

**In `src/advect/remote.py`:**

- `scp_to` (line 38): Replace `sys.exit(1)` with `raise UsageError(f"scp to {host} failed: {res.stderr.strip()}")`. Remove the `print(...)` on line 37. Add `from face import UsageError` to imports.
- `scp_from` (line 50): Replace `sys.exit(1)` with `raise UsageError(f"scp from {host} failed: {res.stderr.strip()}")`. Remove the `print(...)` on line 49.
- `ensure_agent_env` (line 72): Replace `sys.exit(1)` with `raise UsageError(...)`. Remove the `print(...)` on lines 68-71; fold that message into the UsageError.

**In `src/advect/cli.py`:**

- `init_` (line 69): Replace `sys.exit(1)` with `raise UsageError(msg)`. The `echo(...)` on line 68 should be removed; fold the message into UsageError.
- `push` arg validation (line 84): Replace `sys.exit(1)` with `raise UsageError("Message is required: advect push [target] \"what you're working on\"")`. Remove the `echo(...)` on line 83.
- `push` remote pull failure (line 164): **Keep as-is for now** — Step 2 replaces this with rollback logic.
- `pull` remote commit/push failure (line 252): Replace `sys.exit(1)` with `raise UsageError(f"Remote commit/push failed: {e}")`. Remove the `echo(...)` on line 251.
- `resume` preflight failure (line 334): Replace `sys.exit(1)` with `raise UsageError(msg)`. Remove the `echo(...)` on line 333.
- `setup` missing source (line 369): Replace `sys.exit(1)` with `raise UsageError(f"Skill source not found at {source}")`. Remove the `echo(...)` on line 368.

After all replacements: remove `import sys` from any file that no longer uses it. `core.py` still uses `sys` in `run_preflight` for the loop; check if it's the only use. `remote.py` likely no longer needs `sys`. `cli.py` still needs `sys` for other things — leave it.

### Step 2: Add rollback on partial push failure

**Depends on Step 1.** The key invariant: if `push` created a WIP commit and pushed it to origin, but the remote pull/setup fails, undo both (unwrap locally, force-push to restore origin).

In `src/advect/cli.py`, refactor the `push` function body (lines 95–204):

1. After step 5 (`push_branch`), record that we've reached the "committed and pushed" state: `pushed = True`.
2. Wrap steps 11–13 (remote pull, hooks, SCP transfer, tmux session) in a `try/except UsageError` block.
3. In the `except` handler, if `wip` is True (a WIP commit was created in step 4):
   - Call `unwrap_wip()` (already exists in `core.py`, does `git reset HEAD~1`)
   - Call `_run(["git", "push", "--force-with-lease", "origin", ctx.branch])` to restore origin
   - `echo("  ⟲ Rolled back WIP commit and origin push")`
   - Re-raise the `UsageError` so face still prints the error and exits

The rollback sequence is: `unwrap_wip()` → `force-push` → re-raise. If force-push itself fails (e.g., network gone), print a warning but still re-raise the original error — the user can manually fix origin.

Concrete structure:
```python
    # 4. WIP commit if dirty
    wip = commit_wip(ctx, target)

    # 5. Push branch
    push_branch(ctx.branch)

    # Steps 6-7 (notes sync, sweep) are non-critical — leave as-is

    # 8-9. Generate and write handoff
    content = generate_handoff(ctx, message, wip)
    handoff_path = write_handoff(ctx, content)
    echo("  ✓ Handoff context written to .handoff.md")

    # 10-13: remote operations — rollback WIP on failure
    try:
        _push_remote(target, ctx, handoff_path)
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
```

Extract steps 10–13 (remote path determination through tmux session creation, lines 123–191) into a new function `_push_remote(target: str, ctx: ProjectContext, handoff_path: str) -> str` that returns the session name. This function contains the `remote_path` computation, agent-env check, pull script SSH, hooks, SCP, and tmux creation. It raises `UsageError` on any failure (which it now does naturally from Step 1 changes, except the remote pull failure at line 164 which currently uses `sys.exit` — convert that one too: replace `echo(...) + sys.exit(1)` with `raise UsageError(f"Remote pull failed: {e}")`).

The summary block (lines 193–204) stays in `push()`, after the try/except. It uses the session name returned by `_push_remote`.

### Step 3: Improve SSH preflight for Tailscale

**Independent of Steps 1–2.**

The problem: `check_ssh` uses `BatchMode=yes` which suppresses Tailscale SSH's interactive auth check on first connection. The check fails even though a subsequent non-BatchMode SSH would succeed and cache the auth.

Fix in `src/advect/core.py`, function `check_ssh` (line 40):

Replace the single SSH attempt with a two-phase check:
1. Try with `BatchMode=yes` + `ConnectTimeout=5` (current behavior — fast, no interactive prompts).
2. If that fails, retry **without** `BatchMode` but with `ConnectTimeout=10` (allows Tailscale's interactive auth to complete and cache).

```python
def check_ssh(host: str) -> tuple[bool, str]:
    # Fast path: BatchMode (no interactive prompts)
    res = _run(["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", host, "echo", "ok"])
    if res.returncode == 0:
        return True, f"SSH to {host} ok"
    # Slow path: allow interactive auth (Tailscale SSH check-mode)
    res = _run(["ssh", "-o", "ConnectTimeout=10", host, "echo", "ok"])
    if res.returncode == 0:
        return True, f"SSH to {host} ok (auth refreshed)"
    return False, f"SSH to {host} failed. Check ~/.ssh/config and tailscale auth"
```

Edge case: the non-BatchMode fallback could prompt for a password if SSH keys aren't configured. This is acceptable — `_run` uses `capture_output=True` which connects stdin to a pipe (not a TTY), so password prompts will fail immediately rather than hang. The user gets the existing error message pointing them to check SSH config.

### Step 4: Add `--force` flag to `push`

**Depends on Step 2** (uses `_push_remote` extraction).

In `src/advect/cli.py`:

1. Change `push` signature from `push(posargs_)` to `push(posargs_, force=False)`. Register the flag in `main()` where the push subcommand is added (line ~395 area). Looking at the current `main()`:

```python
def main():
    cmd = Command(name="advect", func=None, doc="Rapid agentic work handoff between machines.")
    cmd.add(init_, name="init")
    cmd.add(setup)
    cmd.add(push, posargs=True)
    cmd.add(pull, posargs=True)
    cmd.add(resume)
    cmd.run()
```

Add a `Flag` for force. face uses `Flag('--force', char='-f', parse_as=True)`:

```python
from face import Command, Flag, echo
# ...
def main():
    cmd = Command(name="advect", func=None, doc="Rapid agentic work handoff between machines.")
    cmd.add(init_, name="init")
    cmd.add(setup)
    push_cmd = Command(push, posargs=True)
    push_cmd.add(Flag('--force', char='-f', parse_as=True, missing=False,
                       doc='Force: kill existing tmux session, reset remote state'))
    cmd.add(push_cmd)
    cmd.add(pull, posargs=True)
    cmd.add(resume)
    cmd.run()
```

Face injects flag values by parameter name, so `push(posargs_, force=False)` receives the `--force` value automatically.

2. In `_push_remote`, accept `force: bool = False` parameter. When `force` is True:
   - Before the tmux `has-session` check (currently line ~180), kill the existing session unconditionally:
     ```python
     if force:
         _run(["ssh", target,
               f"docker exec agent-env tmux kill-session -t {session} 2>/dev/null"])
     ```
   - Then proceed to create the session (skip the "already exists" warning branch when force is True).

3. Pass `force` through from `push()` to `_push_remote()`.

The `--force` flag does NOT affect the git operations (WIP commit, push, remote pull) — those are already idempotent after Step 2's WIP detection fix. It only affects the tmux session (kill + recreate).

### Step 5: Unwrap the existing WIP commit into a proper commit

**Do last, after all other changes are made and tested.**

The current HEAD is a WIP commit (`[skip ci] [advect:wip] onyx → slate: main`) containing the quick fixes from the first test session. After applying all changes from Steps 1–4:

1. `git reset HEAD~1` to unwrap the WIP
2. Stage and commit all changes (quick fixes + robustness improvements) as a single proper commit: `"Add error handling, rollback, SSH fallback, and --force flag"`
3. `git push --force-with-lease origin main` to replace the WIP on origin

## Critical files & anchors

| File | Region | Why |
|---|---|---|
| `src/advect/core.py` | `run_preflight` (line 54), `check_ssh` (line 40), `push_branch` (line 172) | Preflight exit-on-fail loop, SSH check to add fallback, push that needs to raise not exit |
| `src/advect/cli.py` | `push` (line 74), `main` (line 392) | Rollback wrapping + `_push_remote` extraction; flag registration |
| `src/advect/remote.py` | `scp_to` (line 29), `ensure_agent_env` (line 53) | sys.exit → UsageError conversion |

## Verification

All commands run from `~/work/advect` with `poetry run` prefix.

1. **Existing tests still pass:**
   ```
   poetry run python -m pytest tests/test_core.py -v
   ```

2. **UsageError propagation (Step 1):** Verify that running `advect push` with no message still exits with a readable error (not a traceback):
   ```
   poetry run advect push
   ```
   Expected: prints `error: Message is required: advect push [target] "what you're working on"` and exits 1.

3. **SSH fallback (Step 3):** This is hard to test mechanically since it depends on Tailscale auth cache state. Verify the code path exists by reading `check_ssh` after the edit. A real test: if you have access to a Tailscale host with expired SSH auth, run `advect push <host> "test"` and confirm the preflight passes on the second attempt internally (you'd see `SSH to <host> ok (auth refreshed)`).

4. **`--force` flag (Step 4):**
   ```
   poetry run advect push --help
   ```
   Expected: `--force / -f` appears in the flags section.

5. **Full end-to-end push to slate** (exercises rollback, clone, force):
   ```
   advect push --force slate "Testing robustness fixes"
   ```
   Expected: all steps pass. If a tmux session exists from the previous test, `--force` kills it and creates a new one (no "already exists" warning).

6. **Rollback test (Step 2):** Temporarily break the remote pull by SSHing to slate and making the repo path inaccessible, then run push. Expected: WIP commit is created, pushed, remote pull fails, WIP is rolled back locally, origin is restored, error is printed. This is a manual/destructive test — only run if convenient.

## Assumptions & contingencies

- **face `Flag` API for `parse_as=True`**: The plan assumes `Flag('--force', parse_as=True, missing=False)` is the correct face API for boolean flags. If face uses a different API (e.g. `Flag('--force', default=False)` or similar), check `poetry run python3 -c "import face; help(face.Flag)"` and adapt. The parameter name in `push(posargs_, force=False)` must match the flag's long name minus dashes.
- **`_run` stdin behavior**: The SSH fallback in Step 3 relies on `capture_output=True` (which implies `stdin=PIPE`) preventing password prompts from hanging. If a deployment uses keyboard-interactive SSH that hangs even without a TTY, add `-o`, `PasswordAuthentication=no` to the fallback SSH command.
