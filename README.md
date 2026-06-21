# advect

Rapid agentic work handoff between machines.

When you're mid-feature-work and need to close the laptop, `advect push` captures your in-progress state and launches an agent session on a remote machine. `advect pull` brings work back.

## Install

```bash
pip install git+https://github.com/mahmoud/advect.git
```

Or for development:

```bash
git clone https://github.com/mahmoud/advect.git
cd advect
poetry install
```

## Usage

### Push work to a remote machine

```bash
advect push glob "finishing the pulse scheduler"
```

This will:
1. Run preflight checks (Tailscale, SSH, git)
2. Commit any dirty state as a WIP commit
3. Push the branch
4. Sync notes and run workstream sweep
5. Generate a handoff context file
6. Pull the branch on the remote
7. Start an `omp` session in tmux inside agent-env

### Pull work back from a remote machine

```bash
advect pull glob
```

### Resume after a manual pull

```bash
advect resume
```

### Initialize advect in a project

```bash
advect init
```

Adds `.handoff.md` to `.gitignore`.

## Project hooks

Create `.advect/on-arrive.sh` (executable) in your project for post-pull setup. It runs on the remote after `advect push` pulls the branch.
