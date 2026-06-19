"""Integration: run the REAL docker backend argv against a throwaway container.

Proves `docker exec -it <c> <cmd>` (with a real PTY) reaches the container and
runs the parsed command. Skips unless a docker daemon is reachable and pexpect
is present.
"""

import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

pytestmark = pytest.mark.integration

pexpect = pytest.importorskip("pexpect")

from sshpilot.connection_manager import Connection
from sshpilot.plugins import registry as registry_mod
from sshpilot.plugins.api import PluginContext
from sshpilot.plugins.builtin.docker_protocol import DockerProtocolBackend


def _docker_ready():
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=30).returncode == 0
    except Exception:
        return False


def _ctx():
    return PluginContext(plugin_id="docker", app_config=None, connection_manager=None,
                         protocol_registry=registry_mod.protocol_registry())


@pytest.mark.skipif(not _docker_ready(), reason="docker daemon not available")
def test_docker_exec_round_trip():
    name = f"sshpilot-itest-{os.getpid()}"
    subprocess.run(["docker", "rm", "-f", name],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    up = subprocess.run(["docker", "run", "-d", "--rm", "--name", name,
                         "alpine", "sleep", "120"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    assert up.returncode == 0, f"docker run failed: {up.stderr.decode(errors='replace')}"
    try:
        conn = Connection({"nickname": "d", "protocol": "docker", "container": name,
                           "command": "echo integration-ok", "runtime": "docker"})
        spec = DockerProtocolBackend().build_spawn(conn, _ctx())
        assert spec.argv[:4] == [spec.argv[0], "exec", "-it", name]

        child = pexpect.spawn(spec.argv[0], spec.argv[1:], timeout=30, encoding="utf-8")
        idx = child.expect(["integration-ok", pexpect.EOF, pexpect.TIMEOUT])
        assert idx == 0, f"exec output not seen: {child.before!r}"
        child.expect([pexpect.EOF, pexpect.TIMEOUT], timeout=10)
    finally:
        subprocess.run(["docker", "rm", "-f", name],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
