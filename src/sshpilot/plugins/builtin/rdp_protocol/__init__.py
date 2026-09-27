"""RDP protocol plugin: Windows (and xrdp) desktops through FreeRDP 3.

The remote desktop opens in FreeRDP's own window; the session tab runs the
client under the daemon's PTY like every other protocol, so it carries
FreeRDP's log. Where the login and certificate questions appear depends on
the client: ``xfreerdp3`` asks in the tab ("Password:", "Do you trust the
above certificate? (Y/T/N)"), ``sdl-freerdp3`` in dialogs of its own window.

``/from-stdin`` is deliberately never passed: with it, FreeRDP rejects an
unknown certificate outright instead of asking.

FreeRDP 3 only. Debian/Ubuntu/Arch install ``xfreerdp3``/``sdl-freerdp3``;
Fedora and Homebrew install the unsuffixed names, where ``xfreerdp`` may
still be a FreeRDP 2 build whose options differ, so it must prove its
version first. The SDL client is new in FreeRDP 3, so ``sdl-freerdp`` needs
no check.
"""

from __future__ import annotations

import os
import re
import sys
from gettext import gettext as _
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .._session_failure import BuiltinProtocolError
from .._shell import command_split_diagnostic, split_command
from ....api.models.sessions import PluginSessionFailureCode
from ...api import (
    FieldSpec,
    PluginContext,
    ProtocolBackend,
    SpawnSpec,
    SshPilotPlugin,
)

# (program, needs a version check) per client family, preferred name first.
_SDL_CLIENTS: Tuple[Tuple[str, bool], ...] = (
    ("sdl-freerdp3", False),
    ("sdl-freerdp", False),
)
_X11_CLIENTS: Tuple[Tuple[str, bool], ...] = (
    ("xfreerdp3", False),
    ("xfreerdp", True),
)

_SIZE_RE = re.compile(r"^[1-9][0-9]{2,4}x[1-9][0-9]{2,4}$")
# host[:port] — the value lands inside FreeRDP's comma-separated /gateway
# option, so it may not carry separators of its own.
_GATEWAY_RE = re.compile(r"^[^\s,:]+(:[0-9]{1,5})?$")

_SECURITY = ("auto", "nla", "tls", "rdp")
_CERT_POLICIES = ("prompt", "tofu", "ignore")

_version_cache: Dict[Tuple[str, ...], bool] = {}


def _is_freerdp3(argv: Sequence[str]) -> bool:
    """Whether ``argv --version`` reports FreeRDP 3 (cached per command).

    A local exec, not network I/O; it only runs for an unsuffixed
    ``xfreerdp`` and once per process for each resolved command.
    """
    from .._flatpak import host_binary_version  # noqa: PLC0415

    key = tuple(argv)
    if key not in _version_cache:
        output = host_binary_version(list(argv))
        _version_cache[key] = bool(re.search(r"FreeRDP version 3\.", output))
    return _version_cache[key]


def _prefer_sdl(environment: Dict[str, str]) -> bool:
    """SDL is native on Wayland and on macOS; xfreerdp needs X11/XQuartz."""
    return sys.platform == "darwin" or bool(environment.get("WAYLAND_DISPLAY"))


def _resolve_client(choice: str, environment: Dict[str, str]) -> Optional[List[str]]:
    from .._flatpak import resolve_host_binary  # noqa: PLC0415

    if choice == "sdl":
        families = (_SDL_CLIENTS,)
    elif choice == "x11":
        families = (_X11_CLIENTS,)
    elif _prefer_sdl(environment):
        families = (_SDL_CLIENTS, _X11_CLIENTS)
    else:
        families = (_X11_CLIENTS, _SDL_CLIENTS)
    for family in families:
        for program, check_version in family:
            argv = resolve_host_binary(program)
            if argv and (not check_version or _is_freerdp3(argv)):
                return list(argv)
    return None


def _server_address(host: str, port: int) -> str:
    # A bare IPv6 literal needs brackets before FreeRDP can split the port.
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{host}:{port}"


def _text(data: Dict[str, Any], key: str) -> str:
    return str(data.get(key) or "").strip()


class RdpProtocolBackend(ProtocolBackend):
    protocol_id = "rdp"
    display_name = "RDP"
    default_port = 3389

    def capabilities(self) -> frozenset:
        return frozenset()

    def connection_fields(self) -> List[FieldSpec]:
        return [
            FieldSpec(key="host", label=_("Host"), kind="text", required=True,
                      placeholder=_("hostname or IP address")),
            FieldSpec(key="port", label=_("Port"), kind="int",
                      default=self.default_port),
            FieldSpec(key="username", label=_("Username"), kind="text",
                      placeholder=_("(asked when connecting)")),
            FieldSpec(key="domain", label=_("Domain"), kind="text"),
            FieldSpec(key="fullscreen", label=_("Full screen"), kind="switch",
                      default=False, group="display"),
            FieldSpec(key="size", label=_("Window size"), kind="text",
                      placeholder="1600x900", group="display"),
            FieldSpec(key="dynamic_resolution",
                      label=_("Resize the remote desktop with the window"),
                      kind="switch", default=True, group="display"),
            FieldSpec(key="clipboard", label=_("Share clipboard"), kind="switch",
                      default=True, group="devices"),
            FieldSpec(key="sound", label=_("Play remote audio locally"),
                      kind="switch", default=True, group="devices"),
            FieldSpec(key="shared_folder", label=_("Shared folder"), kind="text",
                      placeholder="~/Public", group="devices"),
            FieldSpec(key="security", label=_("Security"), kind="choice",
                      default="auto", group="advanced",
                      choices=[("auto", _("Negotiate (default)")),
                               ("nla", _("NLA")),
                               ("tls", _("TLS")),
                               ("rdp", _("RDP (legacy)"))]),
            FieldSpec(key="cert_policy", label=_("Server certificate"),
                      kind="choice", default="prompt", group="advanced",
                      choices=[("prompt", _("Ask when unknown or changed")),
                               ("tofu", _("Trust on first use")),
                               ("ignore", _("Ignore (insecure)"))]),
            FieldSpec(key="gateway", label=_("RD Gateway"), kind="text",
                      placeholder="gateway.example.com:443", group="advanced"),
            FieldSpec(key="client", label=_("FreeRDP client"), kind="choice",
                      default="auto", group="advanced",
                      choices=[("auto", _("Automatic")),
                               ("sdl", _("SDL (Wayland, macOS)")),
                               ("x11", _("X11"))]),
            FieldSpec(key="extra_rdp_args", label=_("Extra FreeRDP arguments"),
                      kind="text", placeholder="/network:auto", group="advanced"),
        ]

    def validate(self, data: Dict[str, Any]) -> List[str]:
        errors: List[str] = []
        if not (_text(data, "host") or _text(data, "hostname")):
            errors.append(_("A host is required."))
        raw_port = data.get("port", self.default_port)
        if raw_port not in (None, ""):
            try:
                if not 0 < int(raw_port) < 65536:
                    errors.append(_("Port must be between 1 and 65535."))
            except (TypeError, ValueError):
                errors.append(_("Port must be a number."))
        size = _text(data, "size")
        if size and not _SIZE_RE.match(size):
            errors.append(_("Window size must look like 1600x900."))
        gateway = _text(data, "gateway")
        if gateway and not _GATEWAY_RE.match(gateway):
            errors.append(_("RD Gateway must be a host name, optionally with :port."))
        if "," in _text(data, "shared_folder"):
            errors.append(_("The shared folder path cannot contain a comma."))
        diagnostic = command_split_diagnostic(data.get("extra_rdp_args"))
        if diagnostic:
            errors.append(
                _("{field} could not be parsed: {diagnostic}.").format(
                    field=_("Extra FreeRDP arguments"), diagnostic=diagnostic
                )
            )
        return errors

    def build_spawn(self, connection: Any, ctx: PluginContext) -> SpawnSpec:
        data = getattr(connection, "data", None) or {}
        host = (_text(data, "host") or _text(data, "hostname")
                or str(getattr(connection, "hostname", "") or "").strip()
                or str(getattr(connection, "host", "") or "").strip())
        if not host:
            raise BuiltinProtocolError(
                PluginSessionFailureCode.HOST_REQUIRED,
                "No host configured for this connection.",
            )
        # Port comes from the data dict only: the Connection attribute
        # defaults to the SSH port (22), which is wrong here.
        try:
            port = int(data.get("port") or self.default_port)
        except (TypeError, ValueError):
            port = self.default_port

        # Parse user input before looking for binaries, so a typo is
        # reported as such even where FreeRDP is not installed.
        extra = split_command(data.get("extra_rdp_args"), "extra_rdp_args")

        env = dict(os.environ)
        choice = _text(data, "client") or "auto"
        client = _resolve_client(choice, env)
        if client is None:
            raise BuiltinProtocolError(
                PluginSessionFailureCode.RDP_CLIENT_UNAVAILABLE,
                "FreeRDP 3 is not installed. Install 'sdl-freerdp3' or "
                "'xfreerdp3' to use RDP connections.",
                parameters={
                    "preferred_program": "sdl-freerdp3",
                    "fallback_program": "xfreerdp3",
                },
            )

        argv = [*client, f"/v:{_server_address(host, port)}"]
        username = _text(data, "username")
        if username:
            argv.append(f"/u:{username}")
        domain = _text(data, "domain")
        if domain:
            argv.append(f"/d:{domain}")
        title = str(getattr(connection, "nickname", "") or "").strip()
        if title:
            argv.append(f"/t:{title}")

        if data.get("fullscreen"):
            argv.append("/f")
        size = _text(data, "size")
        if size:
            argv.append(f"/size:{size}")
        if data.get("dynamic_resolution", True):
            argv.append("/dynamic-resolution")

        if not data.get("clipboard", True):
            argv.append("-clipboard")
        if data.get("sound", True):
            argv.append("/sound")
        shared = _text(data, "shared_folder")
        if shared:
            argv.append(f"/drive:sshpilot,{os.path.expanduser(shared)}")

        security = _text(data, "security")
        if security in _SECURITY and security != "auto":
            argv.append(f"/sec:{security}")
        cert_policy = _text(data, "cert_policy")
        if cert_policy in _CERT_POLICIES and cert_policy != "prompt":
            argv.append(f"/cert:{cert_policy}")
        gateway = _text(data, "gateway")
        if gateway:
            argv.append(f"/gateway:g:{gateway}")

        argv += extra
        return SpawnSpec(argv=argv, env=env)


class Plugin(SshPilotPlugin):
    def activate(self, ctx: PluginContext) -> None:
        ctx.register_protocol(RdpProtocolBackend())
