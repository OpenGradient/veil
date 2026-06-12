"""Run the local server detached, with a pidfile + log file so it can be stopped.

By default ``veil`` runs the interactive setup in the foreground, then
re-launches ``veil serve`` as a detached child whose output goes to a log
file, recording the pid so a later ``veil stop`` can terminate it. Pass
``--foreground`` to block instead (e.g. under systemd or in a container).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

from og_local.config import config_home


def pid_path() -> Path:
    return config_home() / "server.pid"


def log_path() -> Path:
    return config_home() / "server.log"


def running_pid() -> int | None:
    """Return the pid of the background server if one is alive, else None."""
    path = pid_path()
    if not path.exists():
        return None
    try:
        pid = int(path.read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)  # signal 0 just checks the process exists
    except OSError:
        path.unlink(missing_ok=True)  # stale pidfile
        return None
    return pid


def start_background(serve_flags: list[str]) -> int:
    """Launch ``veil serve --skip-setup <flags>`` detached; return its pid.

    Setup (login + host) must already be done in the foreground by the caller.
    """
    existing = running_pid()
    if existing is not None:
        raise RuntimeError(f"a background server is already running (pid {existing})")

    log = open(log_path(), "a", buffering=1)  # noqa: SIM115 — handed to the child
    cmd = [sys.executable, "-m", "og_local", "serve", "--skip-setup", *serve_flags]
    proc = subprocess.Popen(
        cmd,
        stdout=log,
        stderr=log,
        stdin=subprocess.DEVNULL,
        start_new_session=True,  # detach from this terminal/process group
        env=os.environ.copy(),
    )
    pid_path().write_text(str(proc.pid))
    return proc.pid


def stop_background() -> int | None:
    """Stop the background server if running; return the pid that was stopped."""
    pid = running_pid()
    if pid is None:
        return None
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    pid_path().unlink(missing_ok=True)
    return pid
