"""Integration: drive the REAL serial backend argv against a socat-created PTY.

Proves the parsed arguments actually launch picocom and open the device — the
"args reach the PTY" check. Skips unless socat + picocom + pexpect are present
(so it no-ops in the unit job and locally; runs in the test-environment image).
"""

import os
import shutil
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

pytestmark = pytest.mark.integration

pexpect = pytest.importorskip("pexpect")

from sshpilot.connection_manager import Connection
from sshpilot.plugins import registry as registry_mod
from sshpilot.plugins.api import PluginContext
from sshpilot.plugins.builtin.serial_protocol import SerialProtocolBackend


def _ctx():
    return PluginContext(plugin_id="serial", app_config=None, connection_manager=None,
                         protocol_registry=registry_mod.protocol_registry())


@pytest.mark.skipif(not shutil.which("socat"), reason="socat not installed")
@pytest.mark.skipif(not shutil.which("picocom"), reason="picocom not installed")
def test_serial_picocom_opens_socat_pty(tmp_path):
    link_a = str(tmp_path / "ttyA")
    link_b = str(tmp_path / "ttyB")
    socat = subprocess.Popen(
        ["socat", "-d", "-d",
         f"pty,raw,echo=0,link={link_a}", f"pty,raw,echo=0,link={link_b}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        # Wait for socat to create the linked PTY nodes.
        deadline = time.time() + 10
        while time.time() < deadline and not (os.path.exists(link_a) and os.path.exists(link_b)):
            time.sleep(0.1)
        assert os.path.exists(link_a), "socat did not create the PTY link"

        conn = Connection({"nickname": "ser", "protocol": "serial",
                           "device": link_a, "baud": "115200", "flow": "none"})
        spec = SerialProtocolBackend().build_spawn(conn, _ctx())
        assert os.path.basename(spec.argv[0]) == "picocom"

        child = pexpect.spawn(spec.argv[0], spec.argv[1:], timeout=15, encoding="utf-8")
        try:
            idx = child.expect(["Terminal ready", "FATAL", pexpect.EOF, pexpect.TIMEOUT])
            assert idx == 0, f"picocom did not open {link_a}: {child.before!r}"
            # picocom quit sequence: Ctrl-A Ctrl-X
            child.send("\x01\x18")
            child.expect([pexpect.EOF, pexpect.TIMEOUT], timeout=10)
        finally:
            if child.isalive():
                child.terminate(force=True)
    finally:
        socat.terminate()
        try:
            socat.wait(timeout=5)
        except Exception:
            socat.kill()
