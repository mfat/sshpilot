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
import logging
import re
import shlex
from typing import Any, Callable, List, Optional, Union

logger = logging.getLogger(__name__)

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
                logger.debug("docker output looked like a JSON array but didn't "
                             "parse; falling back to line-by-line")
        out: List[dict] = []
        skipped = 0
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                logger.debug("skipping unparseable docker output line: %.200r", line)
                continue
            if isinstance(obj, dict):
                out.append(obj)
        if skipped:
            logger.debug("%d docker output line(s) failed to parse", skipped)
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

    def volume_inspect(self, name: str) -> dict:
        rows = self._exec_json(f"volume inspect {shlex.quote(name)} --format '{{{{json .}}}}'")
        return rows[0] if rows else {}

    def remove_volume(self, name: str, *, force: bool = False) -> Any:
        flag = " -f" if force else ""
        return self._exec(f"volume rm{flag} {shlex.quote(name)}")

    def networks(self) -> List[dict]:
        """Available networks: ``[{Name, Driver, ...}, ...]``."""
        return self._exec_json("network ls --format '{{json .}}'")

    def network_inspect(self, name: str) -> dict:
        rows = self._exec_json(f"network inspect {shlex.quote(name)} --format '{{{{json .}}}}'")
        return rows[0] if rows else {}

    def remove_network(self, name: str) -> Any:
        return self._exec(f"network rm {shlex.quote(name)}")

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
        """`exec -it` into the container, preferring bash with an sh fallback.

        The shell is resolved via the container's PATH (not hard-coded
        ``/bin/bash``/``/bin/sh``), so images whose shell lives elsewhere still
        work. A single ``exec`` is used so exiting bash with a non-zero status
        does not spuriously re-open sh."""
        c = shlex.quote(container_id)
        opts = ""
        if user:
            opts += f"-u {shlex.quote(user)} "
        if workdir:
            opts += f"-w {shlex.quote(workdir)} "
        # Resolved inside the container by its own PATH; prefer bash, else sh.
        picker = shlex.quote("command -v bash >/dev/null 2>&1 && exec bash || exec sh")
        return f"{self._interactive_runtime()} exec -it {opts}{c} sh -c {picker}"

    def stats_stream_command(self) -> str:
        return f"{self._interactive_runtime()} stats"

    # -- details / inspect -------------------------------------------
    def inspect(self, container_id: str) -> dict:
        """Full ``inspect`` of a container as a dict (empty on failure)."""
        rows = self._exec_json(
            f"inspect {shlex.quote(container_id)} --format '{{{{json .}}}}'")
        return rows[0] if rows else {}

    # -- images ------------------------------------------------------
    def image_history(self, image: str) -> List[dict]:
        return self._exec_json(
            f"history --no-trunc --format '{{{{json .}}}}' {shlex.quote(image)}")

    def image_prune(self) -> Any:
        return self._exec("image prune -f")

    def pull_command(self, ref: str) -> str:
        """Streamed `pull` (progress shown in a terminal tab)."""
        return f"{self._interactive_runtime()} pull {shlex.quote(ref)}"

    # -- compose -----------------------------------------------------
    _COMPOSE_ACTIONS = {"stop", "start", "restart"}

    def compose_ls(self) -> List[dict]:
        """Compose projects: ``[{Name, Status, ConfigFiles}, ...]`` (Compose v2).

        Older Compose builds support neither ``--all`` (include stopped projects)
        nor ``--format json``, so we degrade through the variants on an
        "unknown flag" error and parse the plain table as the last resort."""
        # (args, returns-json) in order of richest → most compatible.
        variants = (
            ("compose ls --all --format json", True),
            ("compose ls --format json", True),
            ("compose ls --all", False),
            ("compose ls", False),
        )
        res = None
        for args, is_json in variants:
            res = self._exec(args)
            if getattr(res, "exit_code", 1) == 0:
                return (self._parse_ndjson(res.stdout) if is_json
                        else self._parse_compose_table(res.stdout))
            text = ((res.stderr or "") + (res.stdout or "")).lower()
            if not ("unknown flag" in text or "unknown shorthand" in text
                    or "--all" in text or "--format" in text):
                break  # a real error (daemon down, no compose) — stop degrading
        raise DockerError(
            ((getattr(res, "stderr", "") or getattr(res, "stdout", "") or "command failed")).strip())

    @staticmethod
    def _parse_compose_table(text: str) -> List[dict]:
        """Parse the columnar ``docker compose ls`` output (no ``--format``).

        Columns are separated by runs of 2+ spaces:
        ``NAME    STATUS    CONFIG FILES``."""
        lines = [ln for ln in (text or "").splitlines() if ln.strip()]
        if lines and lines[0].strip().upper().startswith("NAME"):
            lines = lines[1:]
        out: List[dict] = []
        for line in lines:
            cols = re.split(r"\s{2,}", line.strip())
            row = {"Name": cols[0]}
            if len(cols) > 1:
                row["Status"] = cols[1]
            if len(cols) > 2:
                row["ConfigFiles"] = cols[2]
            out.append(row)
        return out

    def compose(self, project: str, action: str) -> Any:
        """Quick captured compose action on an existing project's containers."""
        if action not in self._COMPOSE_ACTIONS:
            raise ValueError(f"unsupported compose action: {action!r}")
        return self._exec(f"compose -p {shlex.quote(project)} {action}")

    def compose_ps(self, project: str) -> List[dict]:
        """Per-service breakdown of a compose project: ``[{Name, Service, State,
        Status, Ports}, ...]``. Degrades like :meth:`compose_ls` when ``--format``
        isn't supported (older Compose) — parsing the plain table instead."""
        p = shlex.quote(project)
        for args, is_json in ((f"compose -p {p} ps --format json", True),
                              (f"compose -p {p} ps", False)):
            res = self._exec(args)
            if getattr(res, "exit_code", 1) == 0:
                return (self._parse_ndjson(res.stdout) if is_json
                        else self._parse_compose_table(res.stdout))
            text = ((res.stderr or "") + (res.stdout or "")).lower()
            if not ("unknown flag" in text or "unknown shorthand" in text
                    or "--format" in text):
                raise DockerError(
                    ((res.stderr or res.stdout or "command failed")).strip())
        return self._parse_compose_table(res.stdout)

    def compose_up_command(self, config_file: str) -> str:
        """Streamed `compose up -d` (deploy/redeploy) from the project's file."""
        return (f"{self._interactive_runtime()} compose "
                f"-f {shlex.quote(config_file)} up -d")

    def compose_down_command(self, project: str) -> str:
        """Streamed `compose down` (tear the stack down) by project name."""
        return (f"{self._interactive_runtime()} compose "
                f"-p {shlex.quote(project)} down")

    # -- misc --------------------------------------------------------
    def read_file(self, path: str) -> str:
        """Read a host file (e.g. a compose file) via ``cat`` — NOT a docker
        command, so it is not runtime-prefixed (sudo still honoured)."""
        prefix = "sudo -n " if self.use_sudo else ""
        res = self._run_command(self.nickname, f"{prefix}cat {shlex.quote(path)}",
                                timeout=self.timeout)
        if getattr(res, "exit_code", 1) != 0:
            raise DockerError((res.stderr or res.stdout or "read failed").strip())
        return res.stdout or ""

    # -- container creation ------------------------------------------
    def create_run_args(self, image: str, *, name: Optional[str] = None,
                        ports=None, volumes=None, envs=None,
                        restart: Optional[str] = None,
                        command: Union[str, List[str], None] = None,
                        network: Optional[str] = None,
                        interactive: bool = False, tty: bool = False,
                        user: Optional[str] = None,
                        memory: Optional[str] = None,
                        cpus: Optional[str] = None) -> str:
        """Build the ``run -d …`` argument string. ``ports``/``volumes`` are
        ``[(a, b), …]`` → ``a:b``; ``envs`` is ``[(k, v), …]`` → ``k=v``; all
        values are shell-quoted.

        ``command`` is an argv: a list is used as-is, a string is ``shlex.split``;
        either way every token is ``shlex.quote``d, so a value can never break out
        of its argument into the remote shell. (A malformed string raises
        ``ValueError`` from ``shlex.split`` — callers validate before calling.)

        Advanced flags (all optional): ``interactive``/``tty`` add ``-i``/``-t``;
        ``network`` adds ``--network`` (skipped for the default ``bridge``);
        ``user``/``memory``/``cpus`` add ``--user``/``--memory``/``--cpus``."""
        parts: List[str] = ["run", "-d"]
        if interactive:
            parts.append("-i")
        if tty:
            parts.append("-t")
        if name:
            parts += ["--name", shlex.quote(name)]
        if network and network != "bridge":
            parts += ["--network", shlex.quote(network)]
        if user:
            parts += ["--user", shlex.quote(user)]
        if memory:
            parts += ["--memory", shlex.quote(memory)]
        if cpus:
            parts += ["--cpus", shlex.quote(cpus)]
        for host, cont in (ports or []):
            parts += ["-p", shlex.quote(f"{host}:{cont}")]
        for src, dst in (volumes or []):
            parts += ["-v", shlex.quote(f"{src}:{dst}")]
        for key, value in (envs or []):
            parts += ["-e", shlex.quote(f"{key}={value}")]
        if restart and restart != "no":
            parts += ["--restart", shlex.quote(restart)]
        parts.append(shlex.quote(image))
        argv = shlex.split(command) if isinstance(command, str) else list(command or [])
        parts += [shlex.quote(tok) for tok in argv]
        return " ".join(parts)

    def create_container(self, image: str, **kwargs: Any) -> Any:
        """Create + start a detached container; returns the CommandResult."""
        return self._exec(self.create_run_args(image, **kwargs), timeout=120)
