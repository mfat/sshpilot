"""
Helpers for interacting with an SSH agent socket specified by the user (e.g. Bitwarden).

This module centralises reading the configured socket path, validating it, and
running common operations against the agent using the configured `SSH_AUTH_SOCK`.

API:
- get_configured_socket(app_config=None) -> Optional[str]
- socket_exists(path) -> bool
- env_with_socket(app_config=None, base_env=None) -> Dict[str,str]
- list_identities(app_config=None, timeout=5) -> List[str]
- add_key(key_path, app_config=None, timeout=5) -> CompletedProcess
- run_with_socket(args, app_config=None, **subprocess_kwargs) -> CompletedProcess

Note: we prefer calling out to the OpenSSH tooling (ssh-add) rather than
implementing the agent protocol here.
"""
from __future__ import annotations

import os
import json
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

from .platform_utils import get_config_dir


def _read_config_json() -> Optional[dict]:
    try:
        cfg_file = Path(get_config_dir()) / "config.json"
        if cfg_file.exists():
            with cfg_file.open("r") as f:
                return json.load(f)
    except Exception:
        pass
    return None


def get_configured_socket(app_config: Optional[any] = None) -> Optional[str]:
    """Return the configured SSH_AUTH_SOCK from app config or fallback config.json."""
    try:
        if app_config is not None and hasattr(app_config, "get_setting"):
            sock = app_config.get_setting("security.ssh_auth_sock", None)
            if sock:
                return os.path.expanduser(str(sock))
    except Exception:
        pass

    cfg = _read_config_json()
    if cfg:
        try:
            sock = cfg.get("security", {}).get("ssh_auth_sock")
            if sock:
                return os.path.expanduser(str(sock))
        except Exception:
            pass
    return None


def socket_exists(path: Optional[str]) -> bool:
    if not path:
        return False
    try:
        return Path(path).exists() and Path(path).is_socket()
    except Exception:
        # Fallback to os.path
        try:
            return os.path.exists(path)
        except Exception:
            return False


def env_with_socket(app_config: Optional[any] = None, base_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = (base_env or os.environ.copy()).copy()
    env.pop("SSH_ASKPASS", None)
    env.pop("SSH_ASKPASS_REQUIRE", None)
    configured = get_configured_socket(app_config)
    if configured:
        env["SSH_AUTH_SOCK"] = configured
    return env


def list_identities(app_config: Optional[any] = None, timeout: int = 5) -> List[str]:
    """Return lines from `ssh-add -l` when using the configured socket.

    Returns empty list on error or when no identities are present.
    """
    sock = get_configured_socket(app_config)
    if not sock:
        return []
    env = env_with_socket(app_config)
    try:
        p = subprocess.run(["ssh-add", "-l"], env=env, capture_output=True, text=True, timeout=timeout)
        if p.returncode == 0:
            lines = [l.strip() for l in p.stdout.splitlines() if l.strip()]
            return lines
        # ssh-add returns 1 if no identities
        return []
    except Exception:
        return []


def add_key(key_path: str, app_config: Optional[any] = None, timeout: int = 10) -> subprocess.CompletedProcess:
    env = env_with_socket(app_config)
    return subprocess.run(["ssh-add", str(key_path)], env=env, capture_output=True, text=True, timeout=timeout)


def run_with_socket(args: List[str], app_config: Optional[any] = None, **subprocess_kwargs) -> subprocess.CompletedProcess:
    env = env_with_socket(app_config, subprocess_kwargs.pop("env", None))
    return subprocess.run(args, env=env, **subprocess_kwargs)
