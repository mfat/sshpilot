"""Serial console protocol plugin.

A local serial/USB console (routers, switches, embedded boards) opened in the
VTE via ``picocom`` (preferred) or ``screen``. No network, no auth — just a
device path and a baud rate. Like telnet, it needs nothing beyond the system
tool and stays entirely within the terminal seam.
"""

from __future__ import annotations

import os
import shutil  # noqa: F401  # kept: tests patch this module's `shutil.which`
from gettext import gettext as _
from typing import Any, Dict, List

from .._session_failure import BuiltinProtocolError
from ....api.models.sessions import PluginSessionFailureCode
from ...api import (
    FieldSpec,
    PluginContext,
    ProtocolBackend,
    SpawnSpec,
    SshPilotPlugin,
)

_BAUDS = ("9600", "19200", "38400", "57600", "115200")
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


class SerialProtocolBackend(ProtocolBackend):
    protocol_id = "serial"
    display_name = "Serial"
    default_port = None

    def capabilities(self) -> frozenset:
        return frozenset()

    def connection_fields(self) -> List[FieldSpec]:
        return [
            FieldSpec(key="device", label=_("Device"), kind="text", required=True,
                      placeholder="/dev/ttyUSB0"),
            FieldSpec(key="baud", label=_("Baud rate"), kind="choice", default="115200",
                      choices=[(b, b) for b in _BAUDS]),
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
        ]

    def validate(self, data: Dict[str, Any]) -> List[str]:
        errors: List[str] = []
        if not (data.get("device") or "").strip():
            errors.append(_("A serial device is required."))
        baud = data.get("baud") or "115200"
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
        baud = str(data.get("baud") or "115200")
        flow = str(data.get("flow") or "none")

        from .._flatpak import resolve_host_binary  # noqa: PLC0415
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
            argv.append(device)
            return SpawnSpec(argv=argv, env=dict(os.environ))

        screen = resolve_host_binary("screen")
        if screen:
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
