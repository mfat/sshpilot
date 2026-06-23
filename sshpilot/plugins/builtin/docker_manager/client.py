"""Data layer for the Docker Manager plugin.

A thin wrapper that runs Docker/Podman **CLI** commands on a host over the app's
single native SSH path (``ctx.run_command``) and parses the JSON the CLI emits
with ``--format '{{json .}}'``. No docker SDK and no extra dependencies — the CLI
gives structured data via stdlib :mod:`json`, works for both ``docker`` and
``podman``, and reuses the host's real ``~/.ssh/config`` / auth.

This module is deliberately free of GTK and of any direct sshpilot imports: it
takes an injected ``run_command`` callable, so it is unit-testable offline with a
fake (no Docker, no SSH needed).
"""

from __future__ import annotations

import json
import shlex
from typing import Any, Callable, List, Optional

# run_command(nickname, command, *, timeout=...) -> object with
# .exit_code / .stdout / .stderr (the app's CommandResult).
RunCommand = Callable[..., Any]


class DockerError(Exception):
    """Raised when a Docker/Podman command exits non-zero."""


class DockerClient:
    # Substrings that mark a Docker-socket permission failure (non-root user not
    # in the ``docker`` group). Used to decide whether to retry with sudo.
    _PERMISSION_MARKERS = (
        "permission denied",
        "dial unix",
        "connect: permission denied",
        "got permission denied",
    )

    def __init__(self, run_command: RunCommand, nickname: str,
                 runtime: str = "docker", *, use_sudo: bool = False,
                 timeout: float = 30) -> None:
        self._run_command = run_command
        self.nickname = nickname
        self.runtime = runtime or "docker"
        self.use_sudo = use_sudo
        self.timeout = timeout

    @classmethod
    def is_permission_error(cls, text: str) -> bool:
        low = (text or "").lower()
        return any(marker in low for marker in cls._PERMISSION_MARKERS)

    # ``sudo -n`` for captured commands: non-interactive, so it fails fast
    # (rather than hanging on a password prompt) when passwordless sudo isn't
    # configured. Interactive terminal commands use plain ``sudo`` so the PTY
    # can prompt for a password.
    def _captured_runtime(self) -> str:
        return f"sudo -n {self.runtime}" if self.use_sudo else self.runtime

    def _interactive_runtime(self) -> str:
        return f"sudo {self.runtime}" if self.use_sudo else self.runtime

    # -- low level ----------------------------------------------------
    def _exec(self, args: str, *, timeout: Optional[float] = None) -> Any:
        """Run ``<runtime> <args>`` on the host and return the CommandResult."""
        return self._run_command(self.nickname, f"{self._captured_runtime()} {args}",
                                 timeout=timeout if timeout is not None else self.timeout)

    def _exec_json(self, args: str, *, timeout: Optional[float] = None) -> List[dict]:
        res = self._exec(args, timeout=timeout)
        if getattr(res, "exit_code", 1) != 0:
            raise DockerError((res.stderr or res.stdout or "command failed").strip())
        return self._parse_ndjson(res.stdout)

    @staticmethod
    def _parse_ndjson(text: str) -> List[dict]:
        """Parse newline-delimited JSON objects (Docker/Podman ``{{json .}}``).

        Also tolerates a single JSON array (some Podman versions), so callers get
        a consistent ``list[dict]`` either way."""
        text = (text or "").strip()
        if not text:
            return []
        # Whole-payload array first (Podman ``--format json``).
        if text[0] == "[":
            try:
                data = json.loads(text)
                return [d for d in data if isinstance(d, dict)]
            except json.JSONDecodeError:
                pass
        out: List[dict] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
        return out

    # -- runtime detection -------------------------------------------
    def detect_runtime(self) -> Optional[str]:
        """Return ``'docker'`` or ``'podman'`` (whichever the host has), else None."""
        res = self._run_command(
            self.nickname,
            "sh -lc 'command -v docker >/dev/null 2>&1 && echo docker || "
            "(command -v podman >/dev/null 2>&1 && echo podman)'",
            timeout=self.timeout,
        )
        out = (getattr(res, "stdout", "") or "").strip().lower()
        if out.endswith("podman"):
            return "podman"
        if out.endswith("docker"):
            return "docker"
        return None

    # -- queries ------------------------------------------------------
    def ps(self, all: bool = True) -> List[dict]:
        flag = "-a " if all else ""
        return self._exec_json(f"ps {flag}--format '{{{{json .}}}}'")

    def stats(self) -> List[dict]:
        return self._exec_json("stats --no-stream --format '{{json .}}'")

    def images(self) -> List[dict]:
        return self._exec_json("images --format '{{json .}}'")

    def volumes(self) -> List[dict]:
        return self._exec_json("volume ls --format '{{json .}}'")

    def ping(self) -> Any:
        """Cheap access probe (``<runtime> ps -q``); returns the CommandResult so
        callers can inspect exit_code/stderr (e.g. to detect a permission error
        and decide whether to retry with sudo)."""
        return self._exec("ps -q")

    def logs_snapshot(self, container_id: str, *, tail: int = 100,
                      timestamps: bool = False) -> str:
        """Return the last ``tail`` log lines (non-following) as text."""
        ts = "-t " if timestamps else ""
        res = self._exec(f"logs {ts}--tail {int(tail)} {shlex.quote(container_id)}")
        # docker writes container logs to both stdout and stderr; show both.
        return ((res.stdout or "") + (res.stderr or "")).rstrip("\n")

    # -- actions ------------------------------------------------------
    _LIFECYCLE = {"start", "stop", "restart", "kill", "pause", "unpause", "rm"}

    def lifecycle(self, action: str, container_id: str, *, force: bool = False) -> Any:
        if action not in self._LIFECYCLE:
            raise ValueError(f"unsupported action: {action!r}")
        flag = " -f" if (force and action == "rm") else ""
        return self._exec(f"{action}{flag} {shlex.quote(container_id)}")

    def remove_image(self, image_id: str, *, force: bool = False) -> Any:
        flag = " -f" if force else ""
        return self._exec(f"rmi{flag} {shlex.quote(image_id)}")

    def system_prune(self) -> Any:
        return self._exec("system prune -f")

    def volume_prune(self) -> Any:
        return self._exec("volume prune -f")

    # -- command strings for ctx.open_command_terminal ----------------
    # These return shell command strings (run on the host) for streamed /
    # interactive output that a captured run_command cannot show.
    def logs_follow_command(self, container_id: str, *, tail: int = 100,
                            timestamps: bool = False) -> str:
        parts = [self._interactive_runtime(), "logs", "-f"]
        if timestamps:
            parts.append("-t")
        if tail:
            parts += ["--tail", str(int(tail))]
        parts.append(shlex.quote(container_id))
        return " ".join(parts)

    def exec_shell_command(self, container_id: str, *, user: Optional[str] = None,
                           workdir: Optional[str] = None) -> str:
        """`exec -it` into the container, preferring bash with an sh fallback."""
        c = shlex.quote(container_id)
        opts = ""
        if user:
            opts += f"-u {shlex.quote(user)} "
        if workdir:
            opts += f"-w {shlex.quote(workdir)} "
        rt = self._interactive_runtime()
        return (f"{rt} exec -it {opts}{c} /bin/bash || "
                f"{rt} exec -it {opts}{c} /bin/sh")

    def stats_stream_command(self) -> str:
        return f"{self._interactive_runtime()} stats"
