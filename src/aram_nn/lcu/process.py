"""Locate the running League Client and read its auth credentials.

Two strategies, tried in order:
  1. Parse LeagueClientUx.exe command-line args (reliable on Windows, same-user processes).
     Args are read from the list directly — no join-then-regex, so tokens with special
     chars work correctly.
  2. Read the lockfile at the default install path, verify the PID is still alive.
     Lockfile format: LeagueClient:PID:PORT:PASSWORD:https
     PASSWORD may contain ':'; we split with maxsplit=4 to handle that.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

_LOCKFILE_PATHS = [
    Path(r"C:\Riot Games\League of Legends\lockfile"),
    Path(r"C:\Program Files\Riot Games\League of Legends\lockfile"),
    Path(r"C:\Program Files (x86)\Riot Games\League of Legends\lockfile"),
]


@dataclass
class LCUCredentials:
    port: int
    token: str


def _from_cmdline() -> LCUCredentials | None:
    try:
        import psutil
    except ImportError:
        return None

    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            name = proc.info["name"] or ""
            if "LeagueClientUx" not in name:
                continue
            port_val: str | None = None
            token_val: str | None = None
            for arg in (proc.info["cmdline"] or []):
                # Parse each arg directly from the list — avoids join-then-regex issues
                # with tokens that contain spaces, quotes, or other special characters.
                if arg.startswith("--app-port="):
                    port_val = arg.split("=", 1)[1]
                elif arg.startswith("--remoting-auth-token="):
                    token_val = arg.split("=", 1)[1].strip("\"'")
            if port_val and token_val:
                return LCUCredentials(int(port_val), token_val)
        except Exception:
            continue
    return None


def _from_lockfile() -> LCUCredentials | None:
    try:
        import psutil
        _has_psutil = True
    except ImportError:
        _has_psutil = False

    for path in _LOCKFILE_PATHS:
        try:
            if not path.exists():
                continue
            # Split with maxsplit=4 so passwords containing ':' stay intact.
            # Format: LeagueClient:PID:PORT:PASSWORD:https
            parts = path.read_text().strip().split(":", 4)
            if len(parts) < 4:
                continue
            pid = int(parts[1])
            port = int(parts[2])
            password = parts[3]
            # Verify the process is still alive — avoids returning stale credentials
            # from a leftover lockfile after League crashes.
            if _has_psutil and not psutil.pid_exists(pid):
                continue
            return LCUCredentials(port, password)
        except Exception:
            continue
    return None


def get_credentials() -> LCUCredentials | None:
    """Return LCU credentials if the League client is running, else None."""
    return _from_cmdline() or _from_lockfile()
