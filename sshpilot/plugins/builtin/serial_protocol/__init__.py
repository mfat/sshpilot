"""Serial console protocol plugin.

A local serial/USB console (routers, switches, embedded boards) opened in the
VTE via ``picocom`` (preferred) or ``screen``. No network, no auth — just a
device path and a baud rate. Like telnet, it needs nothing beyond the system
tool and stays entirely within the terminal seam.
"""

from __future__ import annotations

import os
import shutil
from typing import Any, Dict, List

from ...api import (
    FieldSpec,
    PluginContext,
    ProtocolBackend,
    ProtocolError,
    SpawnSpec,
    SshPilotPlugin,
)

_BAUDS = ("9600", "19200", "38400", "57600", "115200")
_FLOW = (("none", "None"), ("hard", "Hardware (RTS/CTS)"), ("soft", "Software (XON/XOFF)"))
# picocom -f flag values keyed by our choice value
_PICOCOM_FLOW = {"none": "n", "hard": "h", "soft": "x"}
_PARITY = (("none", "None"), ("even", "Even"), ("odd", "Odd"))
_PICOCOM_PARITY = {"none": "n", "even": "e", "odd": "o"}


class SerialProtocolBackend(ProtocolBackend):
    protocol_id = "serial"
    display_name = "Serial"
    default_port = None

    def capabilities(self) -> frozenset:
        return frozenset()

    def connection_fields(self) -> List[FieldSpec]:
        return [
            FieldSpec(key="device", label="Device", kind="text", required=True,
                      placeholder="/dev/ttyUSB0"),
            FieldSpec(key="baud", label="Baud rate", kind="choice", default="115200",
                      choices=[(b, b) for b in _BAUDS]),
            FieldSpec(key="flow", label="Flow control", kind="choice", default="none",
                      choices=list(_FLOW)),
            FieldSpec(key="databits", label="Data bits", kind="choice", default="8",
                      choices=[("8", "8"), ("7", "7"), ("6", "6"), ("5", "5")],
                      group="advanced"),
            FieldSpec(key="parity", label="Parity", kind="choice", default="none",
                      choices=list(_PARITY), group="advanced"),
            FieldSpec(key="stopbits", label="Stop bits", kind="choice", default="1",
                      choices=[("1", "1"), ("2", "2")], group="advanced"),
        ]

    def validate(self, data: Dict[str, Any]) -> List[str]:
        errors: List[str] = []
        if not (data.get("device") or "").strip():
            errors.append("A serial device is required.")
        baud = data.get("baud") or "115200"
        try:
            if int(baud) <= 0:
                errors.append("Baud rate must be a positive number.")
        except (TypeError, ValueError):
            errors.append("Baud rate must be a number.")
        return errors

    def build_spawn(self, connection: Any, ctx: PluginContext) -> SpawnSpec:
        data = getattr(connection, "data", None) or {}
        device = (data.get("device") or "").strip()
        if not device:
            raise ProtocolError("No serial device configured for this connection.")
        baud = str(data.get("baud") or "115200")
        flow = str(data.get("flow") or "none")

        picocom = shutil.which("picocom")
        if picocom:
            argv = [picocom, "-b", baud]
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

        screen = shutil.which("screen")
        if screen:
            # screen handles only the basic device+baud form.
            return SpawnSpec(argv=[screen, device, baud], env=dict(os.environ))

        raise ProtocolError(
            "Neither 'picocom' nor 'screen' is installed. Install one to use "
            "serial connections.")


class Plugin(SshPilotPlugin):
    def activate(self, ctx: PluginContext) -> None:
        ctx.register_protocol(SerialProtocolBackend())
