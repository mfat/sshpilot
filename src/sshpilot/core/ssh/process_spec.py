"""Structured process launch specification (GTK-free)."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional, Tuple


@dataclass(frozen=True)
class ProcessSpec:
    """Ready-to-spawn process description. Core never owns the child process."""

    argv: Tuple[str, ...]
    env: Mapping[str, str] = field(default_factory=dict)
    cwd: Optional[Path] = None

    @classmethod
    def from_ssh_connection_command(cls, cmd: object) -> "ProcessSpec":
        """Adapt a legacy ``SSHConnectionCommand`` (or duck-typed equivalent)."""
        argv = tuple(getattr(cmd, "command", ()) or ())
        env = dict(getattr(cmd, "env", {}) or {})
        return cls(argv=argv, env=env, cwd=None)
