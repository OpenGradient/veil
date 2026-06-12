"""Map the friendly ``opengradient.inference`` hostname to loopback.

So agents can use a readable ``http://opengradient.inference/v1`` base URL instead
of a bare ``127.0.0.1`` address. The server always *binds* loopback; this only
adds a name -> 127.0.0.1 entry to the OS hosts file. Editing the hosts file needs
elevated privileges, so :func:`add_entry` reports a clear manual command when it
can't write the file itself.
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

# Marker so we can add/remove our line idempotently without clobbering the file.
_MARKER = "# added by og-local (opengradient-local)"


def hosts_path() -> Path:
    if sys.platform.startswith("win"):
        return Path(r"C:\Windows\System32\drivers\etc\hosts")
    return Path("/etc/hosts")


def resolves_to_loopback(hostname: str) -> bool:
    """True if ``hostname`` already resolves to a loopback address."""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False
    return any(str(info[4][0]) in ("127.0.0.1", "::1") for info in infos)


def entry_present(hostname: str) -> bool:
    """True if the hosts file already maps ``hostname`` (by anyone)."""
    path = hosts_path()
    if not path.exists():
        return False
    try:
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            parts = stripped.split()
            if len(parts) >= 2 and hostname in parts[1:]:
                return True
    except OSError:
        return False
    return False


def add_entry(hostname: str) -> tuple[bool, str]:
    """Add ``127.0.0.1 <hostname>`` to the hosts file.

    Returns ``(added, message)``. ``added`` is False (with an instructive message)
    when the entry already exists or the file isn't writable without elevation.
    """
    if entry_present(hostname):
        return False, f"{hostname} is already mapped in {hosts_path()}"

    line = f"127.0.0.1\t{hostname}\t{_MARKER}\n"
    path = hosts_path()
    try:
        existing = path.read_text() if path.exists() else ""
        prefix = "" if existing.endswith("\n") or not existing else "\n"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(prefix + line)
    except PermissionError:
        return False, (
            f"can't write {path} without elevated privileges. Add this line manually:\n"
            f"    127.0.0.1  {hostname}\n"
            f"  e.g.  echo '127.0.0.1  {hostname}' | sudo tee -a {path}"
        )
    except OSError as exc:
        return False, f"could not update {path}: {exc}"
    return True, f"mapped {hostname} -> 127.0.0.1 in {path}"
