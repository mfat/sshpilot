"""SSH launch request models and ProcessSpec construction (GTK-free)."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import List, Mapping, Optional, Tuple

from ..errors import CoreError, ErrorCode
from .process_spec import ProcessSpec


class AuthMethod(str, Enum):
    PUBLIC_KEY = "public_key"
    PASSWORD = "password"
    AGENT = "agent"


class HostKeyMode(str, Enum):
    ACCEPT_NEW = "accept-new"
    YES = "yes"
    NO = "no"
    ASK = "ask"


class LaunchMode(str, Enum):
    INTERACTIVE = "interactive"
    BATCH = "batch"
    SCP = "scp"
    SFTP = "sftp"
    COPY_ID = "ssh-copy-id"


@dataclass(frozen=True)
class ForwardSpec:
    kind: str  # local | remote | dynamic
    listen: str
    destination: str = ""

    def as_argv(self) -> Tuple[str, ...]:
        kind = self.kind.lower()
        if kind == "local":
            return ("-L", f"{self.listen}:{self.destination}")
        if kind == "remote":
            return ("-R", f"{self.listen}:{self.destination}")
        if kind == "dynamic":
            return ("-D", self.listen)
        raise CoreError(ErrorCode.VALIDATION_ERROR, f"Unknown forward kind {self.kind!r}")


@dataclass
class SSHLaunchRequest:
    """Normalized description of an SSH process to construct.

    Does not execute anything. Call :func:`build_ssh_process_spec`.
    """

    destination: str
    executable: str = "ssh"
    config_file: Optional[str] = None
    username: Optional[str] = None
    port: Optional[int] = None
    identity_files: List[str] = field(default_factory=list)
    certificate_file: Optional[str] = None
    proxy_jump: List[str] = field(default_factory=list)
    proxy_command: Optional[str] = None
    forwards: List[ForwardSpec] = field(default_factory=list)
    agent_forwarding: bool = False
    auth_method: AuthMethod = AuthMethod.PUBLIC_KEY
    host_key_mode: Optional[HostKeyMode] = None
    batch_mode: bool = False
    compression: bool = False
    keepalive_interval: Optional[int] = None
    keepalive_count_max: Optional[int] = None
    connect_timeout: Optional[int] = None
    remote_command: Optional[str] = None
    local_command: Optional[str] = None
    askpass_required: bool = False
    askpass_require_mode: str = "prefer"  # prefer | force | never
    env: Mapping[str, str] = field(default_factory=dict)
    extra_options: List[str] = field(default_factory=list)
    ssh_overrides: List[str] = field(default_factory=list)
    launch_mode: LaunchMode = LaunchMode.INTERACTIVE
    force_tty: bool = False
    cwd: Optional[Path] = None


def _validate_request(req: SSHLaunchRequest) -> None:
    if not (req.destination or "").strip():
        raise CoreError(ErrorCode.VALIDATION_ERROR, "SSH destination is required")
    if req.port is not None and not (1 <= int(req.port) <= 65535):
        raise CoreError(ErrorCode.VALIDATION_ERROR, f"Invalid port {req.port}")
    if req.proxy_jump and req.proxy_command:
        raise CoreError(
            ErrorCode.VALIDATION_ERROR,
            "ProxyJump and ProxyCommand are mutually exclusive",
        )
    if req.batch_mode and req.askpass_required:
        raise CoreError(
            ErrorCode.VALIDATION_ERROR,
            "BatchMode cannot be combined with askpass interaction",
        )
    if req.local_command and req.launch_mode in {LaunchMode.SCP, LaunchMode.SFTP}:
        raise CoreError(
            ErrorCode.VALIDATION_ERROR,
            "LocalCommand is not valid for SCP/SFTP launch modes",
        )
    if req.askpass_require_mode not in {"prefer", "force", "never"}:
        raise CoreError(
            ErrorCode.VALIDATION_ERROR,
            f"Invalid askpass require mode {req.askpass_require_mode!r}",
        )
    kinds = {f.kind.lower() for f in req.forwards}
    if "dynamic" in kinds and ("local" in kinds or "remote" in kinds):
        # Not strictly forbidden by OpenSSH, but we treat mixed dynamic+L/R as a policy error
        # when both target the same listen semantics in one request — allow for now.
        pass


def build_ssh_process_spec(req: SSHLaunchRequest) -> ProcessSpec:
    """Compose a deterministic ``ProcessSpec`` from *req*.

    Argument order (stable, tested):
    executable, ``-F`` config, explicit ``-o``/flags, identity/certificate/
    proxy/forward options, ``extra_options``, ``ssh_overrides``, destination,
    remote command.

    ``ssh_overrides`` carries the app-wide Preferences settings and comes
    *last*, because OpenSSH takes the first value it obtains for an option.
    They are defaults: anything a connection sets for itself is emitted earlier
    and therefore wins. Placing them first — as this once did — made a global
    Preferences value silently override the per-connection setting the editor
    displayed.
    """
    _validate_request(req)
    argv: List[str] = [req.executable or "ssh"]

    if req.config_file:
        argv.extend(["-F", str(req.config_file)])

    if req.batch_mode or req.launch_mode == LaunchMode.BATCH:
        if "BatchMode=yes" not in argv:
            argv.extend(["-o", "BatchMode=yes"])

    if req.host_key_mode is not None:
        argv.extend(["-o", f"StrictHostKeyChecking={req.host_key_mode.value}"])

    if req.connect_timeout is not None and int(req.connect_timeout) > 0:
        argv.extend(["-o", f"ConnectTimeout={int(req.connect_timeout)}"])

    if req.keepalive_interval is not None and int(req.keepalive_interval) > 0:
        argv.extend(["-o", f"ServerAliveInterval={int(req.keepalive_interval)}"])

    if req.keepalive_count_max is not None and int(req.keepalive_count_max) > 0:
        argv.extend(["-o", f"ServerAliveCountMax={int(req.keepalive_count_max)}"])

    if req.compression:
        argv.append("-C")

    if req.port is not None:
        argv.extend(["-p", str(int(req.port))])

    if req.username:
        argv.extend(["-l", str(req.username)])

    for identity in req.identity_files or []:
        if identity:
            argv.extend(["-i", str(identity)])

    if req.certificate_file:
        argv.extend(["-o", f"CertificateFile={req.certificate_file}"])

    if req.proxy_jump:
        argv.extend(["-J", ",".join(str(j) for j in req.proxy_jump if j)])

    if req.proxy_command:
        argv.extend(["-o", f"ProxyCommand={req.proxy_command}"])

    if req.agent_forwarding:
        argv.append("-A")

    for fwd in req.forwards or []:
        argv.extend(fwd.as_argv())

    if req.local_command:
        argv.extend(["-o", "PermitLocalCommand=yes"])
        argv.extend(["-o", f"LocalCommand={req.local_command}"])

    for opt in req.extra_options or []:
        if opt:
            argv.append(str(opt))

    # App-wide Preferences last: they lose to everything above (see docstring).
    for entry in req.ssh_overrides or []:
        if entry:
            argv.append(str(entry))

    if req.force_tty:
        argv.append("-t")

    dest = req.destination.strip()
    argv.append(dest)

    if req.remote_command:
        argv.append(str(req.remote_command))

    env = dict(req.env or {})
    if req.askpass_required:
        env.setdefault("SSH_ASKPASS_REQUIRE", req.askpass_require_mode)

    return ProcessSpec(argv=tuple(argv), env=env, cwd=req.cwd)
