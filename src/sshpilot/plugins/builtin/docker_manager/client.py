"""Data layer for the Docker Console plugin.

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
from typing import Any, Callable, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)

# run_command(nickname, command, *, timeout=...) -> object with
# .exit_code / .stdout / .stderr (the app's CommandResult).
RunCommand = Callable[..., Any]

# One published-port entry in a `docker ps` Ports string, e.g.
# "0.0.0.0:8080->80/tcp" / ":::8080->80/tcp" / "8080->80/tcp". The greedy
# optional prefix swallows any host address, IPv6 included.
_PUBLISHED_PORT_RE = re.compile(r"(?:.*:)?(\d+)->(\d+)/tcp\s*$")


def parse_published_ports(ports: str) -> List[Tuple[int, int, str]]:
    """``[(host_port, container_port, scheme)]`` from a ``docker ps`` Ports
    string such as ``0.0.0.0:8080->80/tcp, :::8080->80/tcp``. TCP only,
    published entries only, IPv4/IPv6 duplicates collapsed, sorted by host
    port; scheme is ``https`` for the conventional TLS ports (443/8443),
    else ``http``. Tolerant of empty/odd input — returns ``[]``."""
    found: dict = {}
    for entry in str(ports or "").split(","):
        m = _PUBLISHED_PORT_RE.match(entry.strip())
        if not m:
            continue
        host_port, container_port = int(m.group(1)), int(m.group(2))
        scheme = "https" if {host_port, container_port} & {443, 8443} else "http"
        found.setdefault((host_port, container_port), scheme)
    return sorted((hp, cp, s) for (hp, cp), s in found.items())


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

    # Substrings in sudo stderr when the user cannot use sudo at all (as opposed
    # to merely needing a password or entering a wrong one).
    _SUDO_NOT_ALLOWED_MARKERS = (
        "is not in the sudoers",
        "is not allowed to execute",
        "is not allowed to run sudo",
        "unknown user",
    )

    # Sentinel used as sudo's prompt (``sudo -p``) for interactive terminal
    # commands. Locale-independent, so the terminal can reliably detect it and
    # auto-type the password (see ``ctx.open_command_terminal`` pty auto-fill).
    SUDO_PROMPT = "[sshPilot] sudo password:"

    def __init__(self, run_command: RunCommand, nickname: str,
                 runtime: str = "docker", *, use_sudo: bool = False,
                 sudo_password: Optional[str] = None,
                 timeout: float = 30) -> None:
        self._run_command = run_command
        self.nickname = nickname
        self.runtime = runtime or "docker"
        self.use_sudo = use_sudo
        # When set (with ``use_sudo``) the host needs a password for sudo:
        # captured commands feed it to ``sudo -S`` over stdin, interactive
        # commands use ``sudo -p`` so the terminal can auto-type it.
        self.sudo_password = sudo_password
        self.timeout = timeout

    @property
    def _password_mode(self) -> bool:
        """sudo is enabled *and* a password must be supplied (not passwordless)."""
        return self.use_sudo and self.sudo_password is not None

    @classmethod
    def is_permission_error(cls, text: str) -> bool:
        low = (text or "").lower()
        return any(marker in low for marker in cls._PERMISSION_MARKERS)

    @classmethod
    def is_sudo_denied_error(cls, text: str) -> bool:
        low = (text or "").lower()
        return any(marker in low for marker in cls._SUDO_NOT_ALLOWED_MARKERS)

    # Captured commands: ``sudo -S`` (read the password from stdin) when a
    # password is required, else ``sudo -n`` — non-interactive so it fails fast
    # (rather than hanging on a password prompt) when passwordless sudo isn't
    # configured. Interactive terminal commands use ``sudo -p <sentinel>`` so the
    # terminal can detect the prompt and auto-type the password, else plain
    # ``sudo`` so the PTY can prompt.
    def _captured_runtime(self) -> str:
        if not self.use_sudo:
            return self.runtime
        if self._password_mode:
            return f"sudo -S -p '' {self.runtime}"
        return f"sudo -n {self.runtime}"

    def _interactive_runtime(self) -> str:
        if not self.use_sudo:
            return self.runtime
        if self._password_mode:
            return f"sudo -p {shlex.quote(self.SUDO_PROMPT)} {self.runtime}"
        return f"sudo {self.runtime}"

    def _run(self, command: str, *, timeout: Optional[float] = None) -> Any:
        """Invoke the injected ``run_command``, feeding the sudo password to
        ``sudo -S`` over stdin only in password mode (so plain ``run_command``
        implementations that take no ``input`` keep working)."""
        kwargs: dict = {"timeout": timeout if timeout is not None else self.timeout}
        if self._password_mode:
            kwargs["input"] = f"{self.sudo_password}\n"
        return self._invoke(command, **kwargs)

    def _invoke(self, command: str, **kwargs: Any) -> Any:
        """Run *command* via the injected callable and log at DEBUG (visible
        with ``--verbose``). Never logs stdin (may contain a sudo password)."""
        mode = self.runtime + ("+sudo" if self.use_sudo else "")
        logger.debug("docker[%s/%s] run: %s", self.nickname, mode, command)
        try:
            res = self._run_command(self.nickname, command, **kwargs)
        except Exception:
            logger.debug("docker[%s/%s] raised for: %s",
                         self.nickname, mode, command, exc_info=True)
            raise
        self._log_result(res)
        return res

    def _log_result(self, res: Any) -> None:
        exit_code = getattr(res, "exit_code", None)
        stdout = getattr(res, "stdout", "") or ""
        stderr = getattr(res, "stderr", "") or ""
        logger.debug(
            "docker[%s] exit=%s stdout=%dB stderr=%dB",
            self.nickname, exit_code, len(stdout), len(stderr),
        )
        if exit_code not in (0, None):
            detail = (stderr or stdout).strip()
            if detail:
                # Cap so a huge docker/SSH dump does not flood the log.
                logger.debug("docker[%s] failure output: %.800s",
                             self.nickname, detail)

    # -- low level ----------------------------------------------------
    def _exec(self, args: str, *, timeout: Optional[float] = None) -> Any:
        """Run ``<runtime> <args>`` on the host and return the CommandResult."""
        return self._run(f"{self._captured_runtime()} {args}", timeout=timeout)

    def _exec_json(self, args: str, *, timeout: Optional[float] = None) -> List[dict]:
        res = self._exec(args, timeout=timeout)
        if getattr(res, "exit_code", 1) != 0:
            raise DockerError((res.stderr or res.stdout or "command failed").strip())
        rows = self._parse_ndjson(res.stdout)
        logger.debug("docker[%s] parsed %d JSON row(s) from %s",
                     self.nickname, len(rows), args.split()[0] if args else "?")
        return rows

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
        # Call the injected runner directly (not ``_run``): detection must not
        # feed a sudo password on stdin, and it is not a ``<runtime> …`` command.
        # Use ``sh -c`` (not ``sh -lc``): a login shell sources profiles that
        # often hang on non-interactive SSH and burn the full command timeout.
        cmd = (
            "sh -c 'command -v docker >/dev/null 2>&1 && echo docker || "
            "(command -v podman >/dev/null 2>&1 && echo podman)'"
        )
        res = self._invoke(cmd, timeout=self.timeout)
        out = (getattr(res, "stdout", "") or "").strip().lower()
        if out.endswith("podman"):
            logger.debug("docker[%s] detected runtime: podman", self.nickname)
            return "podman"
        if out.endswith("docker"):
            logger.debug("docker[%s] detected runtime: docker", self.nickname)
            return "docker"
        logger.debug("docker[%s] detected runtime: none (stdout=%r)",
                     self.nickname, out)
        return None

    # -- queries ------------------------------------------------------
    def ps(self, all: bool = True) -> List[dict]:
        flag = "-a " if all else ""
        return self._exec_json(f"ps {flag}--format '{{{{json .}}}}'")

    def stats(self) -> List[dict]:
        return self._exec_json("stats --no-stream --format '{{json .}}'")

    def stats_one(self, container_id: str) -> dict:
        """One-shot stats for a single container (empty dict on failure/empty)."""
        rows = self._exec_json(
            f"stats --no-stream --format '{{{{json .}}}}' {shlex.quote(container_id)}")
        return rows[0] if rows else {}

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

    def logs_follow_stream_command(self, container_id: str, *, tail: int = 100,
                                   timestamps: bool = False) -> str:
        """Same as :meth:`logs_follow_command` but with the captured runtime
        (``sudo -S`` / ``sudo -n``) so a non-PTY stream can feed a password."""
        parts = [self._captured_runtime(), "logs", "-f"]
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

    def events_command(self) -> str:
        """Stream container lifecycle events as NDJSON (for in-page updates).

        Uses the captured runtime so a non-PTY stream can feed ``sudo -S``.
        """
        return (
            f"{self._captured_runtime()} events "
            "--filter type=container --format '{{json .}}'"
        )

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
        if self._password_mode:
            prefix = "sudo -S -p '' "
        elif self.use_sudo:
            prefix = "sudo -n "
        else:
            prefix = ""
        res = self._run(f"{prefix}cat {shlex.quote(path)}")
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
