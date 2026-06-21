"""SSH execution helpers for remote operations."""

import subprocess

from face import UsageError


def ssh_run(host: str, command: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a command on a remote host via SSH (stdin pipe, non-interactive)."""
    res = subprocess.run(
        ["ssh", host, "bash", "-s"],
        input=command,
        capture_output=True,
        text=True,
    )
    if check and res.returncode != 0:
        raise RuntimeError(
            f"SSH command failed on {host} (exit {res.returncode}):\n"
            f"  stderr: {res.stderr.strip()}\n"
            f"  command: {command[:200]}"
        )
    return res


def ssh_run_interactive(host: str, command: str) -> None:
    """Run a command on a remote host with TTY allocation (for tmux, etc.)."""
    subprocess.run(["ssh", "-t", host, command])


def scp_to(host: str, local_path: str, remote_path: str) -> None:
    """Copy a local file to a remote host."""
    res = subprocess.run(
        ["scp", local_path, f"{host}:{remote_path}"],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        raise UsageError(f"scp to {host} failed: {res.stderr.strip()}")


def scp_from(host: str, remote_path: str, local_path: str) -> None:
    """Copy a file from a remote host to local."""
    res = subprocess.run(
        ["scp", f"{host}:{remote_path}", local_path],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        raise UsageError(f"scp from {host} failed: {res.stderr.strip()}")


def ensure_agent_env(host: str) -> None:
    """Start agent-env container on remote host if not already running."""
    # Start agent-env
    res = subprocess.run(
        ["ssh", host, "cd ~/work/agent-env && ./dev.sh -d 2>/dev/null"],
        capture_output=True,
        text=True,
    )
    # Verify it's functional
    verify = subprocess.run(
        ["ssh", host, "docker exec agent-env echo ok"],
        capture_output=True,
        text=True,
    )
    if verify.returncode != 0:
        raise UsageError(
            f"agent-env container is not functional on {host}. "
            f"SSH in and run: cd ~/work/agent-env && ./build.sh && ./dev.sh --recreate -d"
        )
    print(f"  \u2713 agent-env is running on {host}")
