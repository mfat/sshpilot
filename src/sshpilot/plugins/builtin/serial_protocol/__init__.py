"""Serial console protocol plugin.

A local serial/USB console (routers, switches, embedded boards) opened in the
VTE via ``picocom`` (preferred) or ``screen``. No network, no auth — just a
device path and a baud rate. Like telnet, it needs nothing beyond the system
tool and stays entirely within the terminal seam.
"""

from __future__ import annotations

import glob
import grp
import os
import shutil  # noqa: F401  # kept: tests patch this module's `shutil.which`
from gettext import gettext as _
from typing import Any, Dict, List, Tuple

from .._session_failure import BuiltinProtocolError
from ....api.models.sessions import PluginSessionFailureCode
from ...api import (
    FieldSpec,
    PluginContext,
    ProtocolBackend,
    SpawnSpec,
    SshPilotPlugin,
)

# Offered as suggestions; any positive rate may be typed. From legacy
# equipment (1200) to board consoles (921600 for ESP flashing, 1500000 on
# Raspberry Pi 5 and Rockchip boards).
_BAUDS = ("1200", "2400", "4800", "9600", "19200", "38400", "57600", "115200",
          "230400", "460800", "921600", "1500000")
_BY_ID = "/dev/serial/by-id"
# picocom --omap: what the Enter key's CR becomes on the wire.
_PICOCOM_SEND = {"lf": "crlf", "crlf": "crcrlf"}
# picocom -f flag values keyed by our choice value
_PICOCOM_FLOW = {"none": "n", "hard": "h", "soft": "x"}
_PICOCOM_PARITY = {"none": "n", "even": "e", "odd": "o"}

# ``screen`` takes the line parameters as a comma-separated stty-style list
# appended to the baud argument (``screen /dev/ttyUSB0 9600,cs7,parenb``), so
# the fallback is not limited to device+baud.  Per ``man screen``, an
# unspecified parameter is left to "the terminal driver […] defaults or values
# saved from a previous connection" — nondeterministic, and invisible to the
# user — so every parameter is emitted explicitly, including the defaults.
_SCREEN_DATABITS = {"8": "cs8", "7": "cs7"}
_SCREEN_PARITY = {
    "none": ("-parenb",),
    "even": ("parenb", "-parodd"),
    "odd": ("parenb", "parodd"),
}
_SCREEN_STOPBITS = {"1": ("-cstopb",), "2": ("cstopb",)}
# Only software flow control has an stty flag here; ``crtscts`` appears in
# screen's status display, not among the settable options.
_SCREEN_FLOW = {
    "none": ("-ixon", "-ixoff"),
    "soft": ("ixon", "ixoff"),
}


def _serial_ports(_values: Dict[str, Any], _ctx: Any) -> List[Tuple[str, str]]:
    """Connected serial ports, stable names first.

    ``/dev/serial/by-id`` names survive replugging and reboots where
    ``ttyUSB0`` can become ``ttyUSB1``, so they are offered as the value, with
    the tty they point at in the label.
    """
    ports: List[Tuple[str, str]] = []
    covered = set()
    for path in sorted(glob.glob(os.path.join(_BY_ID, "*"))):
        target = os.path.realpath(path)
        covered.add(target)
        ports.append((path, f"{os.path.basename(path)} ({os.path.basename(target)})"))
    for pattern in ("/dev/ttyUSB*", "/dev/ttyACM*", "/dev/ttyAMA*",
                    "/dev/rfcomm*", "/dev/cu.*"):
        for path in sorted(glob.glob(pattern)):
            if os.path.realpath(path) not in covered:
                ports.append((path, path))
    return ports


def _baud_rates(_values: Dict[str, Any], _ctx: Any) -> List[Tuple[str, str]]:
    return [(rate, rate) for rate in _BAUDS]


def _access_denied_group(device: str) -> str:
    """The group that may open *device*, when this user may not; else ``""``.

    The usual first-day failure is a device only its group may open
    (dialout on Debian/Ubuntu, uucp on Arch), which the tools report as a
    bare "Permission denied". A Flatpak sees the sandbox's view of /dev,
    not the host's permissions, so it is left to the tool there.
    """
    from .._flatpak import is_flatpak  # noqa: PLC0415

    if is_flatpak() or not os.path.exists(device):
        return ""
    if os.access(device, os.R_OK | os.W_OK):
        return ""
    try:
        return grp.getgrgid(os.stat(device).st_gid).gr_name
    except (OSError, KeyError):
        return ""


class SerialProtocolBackend(ProtocolBackend):
    protocol_id = "serial"
    display_name = "Serial"
    default_port = None
    # A local device: there is no network path for a pre-connection step to open.
    pre_connect = False

    def capabilities(self) -> frozenset:
        return frozenset()

    def connection_fields(self) -> List[FieldSpec]:
        return [
            FieldSpec(key="device", label=_("Device"), kind="text", required=True,
                      placeholder="/dev/ttyUSB0", suggest=_serial_ports),
            FieldSpec(key="baud", label=_("Baud rate"), kind="text", default="115200",
                      placeholder="115200", suggest=_baud_rates),
            FieldSpec(key="flow", label=_("Flow control"), kind="choice", default="none",
                      choices=[("none", _("None")),
                               ("hard", _("Hardware (RTS/CTS)")),
                               ("soft", _("Software (XON/XOFF)"))]),
            FieldSpec(key="databits", label=_("Data bits"), kind="choice", default="8",
                      choices=[("8", "8"), ("7", "7"), ("6", "6"), ("5", "5")],
                      group="advanced"),
            FieldSpec(key="parity", label=_("Parity"), kind="choice", default="none",
                      choices=[("none", _("None")), ("even", _("Even")),
                               ("odd", _("Odd"))], group="advanced"),
            FieldSpec(key="stopbits", label=_("Stop bits"), kind="choice", default="1",
                      choices=[("1", "1"), ("2", "2")], group="advanced"),
            FieldSpec(key="send_newline", label=_("Enter key sends"), kind="choice",
                      default="cr", group="terminal",
                      choices=[("cr", _("CR (default)")), ("lf", _("LF")),
                               ("crlf", _("CR LF"))]),
            FieldSpec(key="recv_add_cr", label=_("Start received lines at the left edge"),
                      kind="switch", default=False, group="terminal"),
            FieldSpec(key="local_echo", label=_("Local echo"), kind="switch",
                      default=False, group="terminal"),
            FieldSpec(key="logfile", label=_("Log to file"), kind="text",
                      placeholder="~/serial.log", group="terminal"),
        ]

    def summary(self, data: Dict[str, Any]) -> str:
        device = str(data.get("device") or "").strip()
        if not device:
            return ""
        # A by-id name is long; the name itself is the informative part.
        if device.startswith(_BY_ID + "/"):
            device = os.path.basename(device)
        return f"{device} @ {str(data.get('baud') or '115200').strip()}"

    def validate(self, data: Dict[str, Any]) -> List[str]:
        errors: List[str] = []
        if not (data.get("device") or "").strip():
            errors.append(_("A serial device is required."))
        baud = str(data.get("baud") or "115200").strip()
        try:
            if int(baud) <= 0:
                errors.append(_("Baud rate must be a positive number."))
        except (TypeError, ValueError):
            errors.append(_("Baud rate must be a number."))
        return errors

    def build_spawn(self, connection: Any, ctx: PluginContext) -> SpawnSpec:
        data = getattr(connection, "data", None) or {}
        device = (data.get("device") or "").strip()
        if not device:
            raise BuiltinProtocolError(
                PluginSessionFailureCode.SERIAL_DEVICE_REQUIRED,
                "No serial device configured for this connection.",
            )
        baud = str(data.get("baud") or "115200").strip()
        flow = str(data.get("flow") or "none")
        send_newline = str(data.get("send_newline") or "cr")
        logfile = str(data.get("logfile") or "").strip()
        terminal_options = (send_newline != "cr" or bool(data.get("recv_add_cr"))
                            or bool(data.get("local_echo")) or bool(logfile))

        from .._flatpak import resolve_host_binary  # noqa: PLC0415
        group = _access_denied_group(device)
        if group:
            raise BuiltinProtocolError(
                PluginSessionFailureCode.SERIAL_DEVICE_ACCESS_DENIED,
                f"Permission denied opening {device}; join the '{group}' group.",
                parameters={"device": device, "group": group},
            )

        picocom = resolve_host_binary("picocom")
        if picocom:
            argv = [*picocom, "-b", baud]
            argv += ["-f", _PICOCOM_FLOW.get(flow, "n")]
            # Line params: only emit when they differ from picocom's 8N1 default,
            # so the common case stays a short command.
            databits = str(data.get("databits") or "8")
            if databits != "8":
                argv += ["--databits", databits]
            parity = str(data.get("parity") or "none")
            if parity != "none":
                argv += ["--parity", _PICOCOM_PARITY.get(parity, "n")]
            stopbits = str(data.get("stopbits") or "1")
            if stopbits != "1":
                argv += ["--stopbits", stopbits]
            if send_newline in _PICOCOM_SEND:
                argv += ["--omap", _PICOCOM_SEND[send_newline]]
            if data.get("recv_add_cr"):
                argv += ["--imap", "lfcrlf"]
            if data.get("local_echo"):
                argv.append("--echo")
            if logfile:
                argv += ["--logfile", os.path.expanduser(logfile)]
            argv.append(device)
            return SpawnSpec(argv=argv, env=dict(os.environ))

        screen = resolve_host_binary("screen")
        if screen:
            # screen has no line-ending maps or local echo; refused with the
            # line settings below rather than quietly behaving differently.
            if terminal_options:
                raise BuiltinProtocolError(
                    PluginSessionFailureCode.SERIAL_SCREEN_TERMINAL_OPTIONS_UNSUPPORTED,
                    "Only 'screen' is available, which cannot change line endings, "
                    "echo locally or log. Install 'picocom' to use this connection.",
                    parameters={
                        "fallback_program": "screen",
                        "preferred_program": "picocom",
                    },
                )
            databits = str(data.get("databits") or "8")
            parity = str(data.get("parity") or "none")
            stopbits = str(data.get("stopbits") or "1")
            # Refuse rather than drop: a serial line is not negotiated, so a
            # parameter that silently fails to apply misframes every byte with
            # nothing in the UI to explain it.
            unsupported = []
            if flow == "hard":
                unsupported.append("hardware (RTS/CTS) flow control")
            if databits not in _SCREEN_DATABITS:
                unsupported.append(f"{databits} data bits")
            if unsupported:
                parameters = {
                    "fallback_program": "screen",
                    "preferred_program": "picocom",
                }
                if flow == "hard" and databits not in _SCREEN_DATABITS:
                    code = (
                        PluginSessionFailureCode.SERIAL_SCREEN_HARDWARE_FLOW_AND_DATABITS_UNSUPPORTED
                    )
                    parameters.update({"flow": "RTS/CTS", "databits": databits})
                elif flow == "hard":
                    code = (
                        PluginSessionFailureCode.SERIAL_SCREEN_HARDWARE_FLOW_UNSUPPORTED
                    )
                    parameters["flow"] = "RTS/CTS"
                else:
                    code = (
                        PluginSessionFailureCode.SERIAL_SCREEN_DATABITS_UNSUPPORTED
                    )
                    parameters["databits"] = databits
                raise BuiltinProtocolError(
                    code,
                    "Only 'screen' is available, which cannot set "
                    + " or ".join(unsupported)
                    + ". Install 'picocom' to use this connection.",
                    parameters=parameters,
                )
            settings = [
                baud,
                _SCREEN_DATABITS[databits],
                *_SCREEN_PARITY.get(parity, _SCREEN_PARITY["none"]),
                *_SCREEN_STOPBITS.get(stopbits, _SCREEN_STOPBITS["1"]),
                *_SCREEN_FLOW.get(flow, _SCREEN_FLOW["none"]),
            ]
            return SpawnSpec(
                argv=[*screen, device, ",".join(settings)],
                env=dict(os.environ),
            )

        raise BuiltinProtocolError(
            PluginSessionFailureCode.SERIAL_PROGRAMS_UNAVAILABLE,
            "Neither 'picocom' nor 'screen' is installed. Install one to use "
            "serial connections.",
            parameters={
                "preferred_program": "picocom",
                "fallback_program": "screen",
            },
        )


class Plugin(SshPilotPlugin):
    def activate(self, ctx: PluginContext) -> None:
        ctx.register_protocol(SerialProtocolBackend())
