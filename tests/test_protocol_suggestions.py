"""What each built-in protocol says about itself, and offers while editing.

Covers the plugin API 1.16 additions -- ``ProtocolBackend.summary()`` (the
list's one-line target, carried to the frontend as
``ConnectionSummary.target_summary``) and ``FieldSpec.suggest`` -- plus the
fields added after comparing the editors with Tabby, XPipe, GNOME
Connections and Remmina.
"""

from __future__ import annotations

import os
import stat
from types import SimpleNamespace

import pytest

from sshpilot.api.models.common import ConnectionId
from sshpilot.api.models.connections import ConnectionSummary
from sshpilot.api.models.sessions import PluginSessionFailureCode
from sshpilot.api.transport.codec import (
    connection_summary_from_wire,
    connection_summary_to_wire,
)
from sshpilot.connection_display import format_connection_host_display
from sshpilot.core.connections import target_summary
from sshpilot.plugins.api import CommandResult
from sshpilot.plugins.builtin import _flatpak
from sshpilot.plugins.builtin._session_failure import BuiltinProtocolError
from sshpilot.plugins.builtin.docker_protocol import DockerProtocolBackend
from sshpilot.plugins.builtin.kubernetes_protocol import KubernetesProtocolBackend
from sshpilot.plugins.builtin.mosh_protocol import MoshProtocolBackend
from sshpilot.plugins.builtin.rdp_protocol import RdpProtocolBackend
from sshpilot.plugins.builtin.serial_protocol import SerialProtocolBackend
from sshpilot.plugins.builtin.telnet_protocol import TelnetProtocolBackend
import sshpilot.plugins.builtin.docker_protocol as docker_mod
import sshpilot.plugins.builtin.kubernetes_protocol as k8s_mod
import sshpilot.plugins.builtin.serial_protocol as serial_mod


class _Ctx:
    """Records local commands and answers them from a table."""

    def __init__(self, answers):
        self.answers = answers
        self.commands = []

    def run_local_command(self, command, timeout=30, input=None):
        self.commands.append(command)
        for needle, result in self.answers.items():
            if needle in command:
                return result
        return CommandResult(1, "", "unexpected command")


@pytest.fixture
def installed(monkeypatch):
    monkeypatch.setattr(_flatpak, "resolve_host_binary", lambda name: [f"/bin/{name}"])
    monkeypatch.setattr(serial_mod, "_access_denied_group", lambda _device: "")


def _spawn(backend, data):
    conn = SimpleNamespace(data=data, nickname="t", hostname=data.get("host", ""))
    return backend.build_spawn(conn, SimpleNamespace(connection_manager=None))


# --- summaries --------------------------------------------------------------

@pytest.mark.parametrize("backend,data,expected", [
    (TelnetProtocolBackend(), {"host": "router", "port": 23}, "router"),
    (TelnetProtocolBackend(), {"host": "router", "port": 2323}, "router:2323"),
    (TelnetProtocolBackend(), {"host": "fe80::1", "port": 2323}, "[fe80::1]:2323"),
    (MoshProtocolBackend(), {"host": "box", "username": "bob", "port": 22}, "bob@box"),
    (RdpProtocolBackend(), {"host": "win", "username": "al", "port": 3390}, "al@win:3390"),
    (RdpProtocolBackend(), {"hostname": "win", "port": 3389}, "win"),
    (DockerProtocolBackend(), {"container": "web", "runtime": "podman"}, "web · Podman"),
    (DockerProtocolBackend(), {"container": "web", "docker_context": "prod"},
     "web · Docker · prod"),
    (KubernetesProtocolBackend(), {"pod": "deploy/api", "namespace": "prod"}, "prod/deploy/api"),
    (KubernetesProtocolBackend(), {"pod": "api-0", "kube_context": "staging"},
     "api-0 · staging"),
    (SerialProtocolBackend(), {"device": "/dev/ttyUSB0", "baud": "9600"},
     "/dev/ttyUSB0 @ 9600"),
    (SerialProtocolBackend(),
     {"device": "/dev/serial/by-id/usb-FTDI_A50-if00-port0"},
     "usb-FTDI_A50-if00-port0 @ 115200"),
    (DockerProtocolBackend(), {}, ""),
])
def test_each_protocol_summarises_its_target(backend, data, expected):
    assert backend.summary(data) == expected


def test_summary_travels_only_when_there_is_one():
    base = dict(id=ConnectionId("c"), nickname="c", host="c", hostname="",
                username="", port=22, protocol="docker")
    plain = ConnectionSummary(**base)
    assert "target_summary" not in connection_summary_to_wire(plain)
    described = ConnectionSummary(**base, target_summary="web · Podman")
    wire = connection_summary_to_wire(described)
    assert connection_summary_from_wire(wire).target_summary == "web · Podman"


def test_core_describer_never_describes_ssh_and_survives_failures(monkeypatch):
    monkeypatch.setattr(target_summary, "_describer", None)
    assert target_summary.describe("rdp", {"host": "x"}) == ""
    target_summary.set_describer(lambda protocol, data: f"{protocol}:\x00{data['host']}\nx")
    try:
        assert target_summary.describe("ssh", {"host": "x"}) == ""
        assert target_summary.describe("rdp", {"host": "x"}) == "rdp:x x"

        def _boom(protocol, data):
            raise RuntimeError("bad plugin")

        target_summary.set_describer(_boom)
        assert target_summary.describe("rdp", {"host": "x"}) == ""
    finally:
        target_summary.set_describer(None)


def test_the_list_shows_the_summary_for_plugin_rows_only():
    row = SimpleNamespace(protocol="serial", target_summary="/dev/ttyUSB0 @ 9600",
                          nickname="ttyusb0", hostname="", username="", host="ttyusb0",
                          port=22)
    assert format_connection_host_display(row, include_port=True) == "/dev/ttyUSB0 @ 9600"
    ssh = SimpleNamespace(protocol="ssh", target_summary="ignored", nickname="web",
                          hostname="web.example", username="al", host="web", port=2222)
    assert format_connection_host_display(ssh, include_port=True) == "al@web.example:2222"


# --- serial -----------------------------------------------------------------

def test_serial_line_options_reach_picocom(installed):
    argv = _spawn(SerialProtocolBackend(), {
        "device": "/dev/ttyUSB0", "baud": "921600", "send_newline": "lf",
        "recv_add_cr": True, "local_echo": True, "logfile": "~/s.log",
    }).argv
    assert argv == ["/bin/picocom", "-b", "921600", "-f", "n", "--omap", "crlf",
                    "--imap", "lfcrlf", "--echo", "--logfile",
                    os.path.expanduser("~/s.log"), "/dev/ttyUSB0"]


def test_screen_refuses_the_options_it_cannot_honour(monkeypatch):
    monkeypatch.setattr(_flatpak, "resolve_host_binary",
                        lambda name: ["/bin/screen"] if name == "screen" else None)
    monkeypatch.setattr(serial_mod, "_access_denied_group", lambda _device: "")
    with pytest.raises(BuiltinProtocolError) as caught:
        _spawn(SerialProtocolBackend(), {"device": "/dev/ttyUSB0", "local_echo": True})
    assert caught.value.failure.code is (
        PluginSessionFailureCode.SERIAL_SCREEN_TERMINAL_OPTIONS_UNSUPPORTED
    )


def test_an_unopenable_device_names_the_group_to_join(monkeypatch):
    monkeypatch.setattr(_flatpak, "resolve_host_binary", lambda name: [f"/bin/{name}"])
    monkeypatch.setattr(serial_mod, "_access_denied_group", lambda _device: "dialout")
    with pytest.raises(BuiltinProtocolError) as caught:
        _spawn(SerialProtocolBackend(), {"device": "/dev/ttyUSB0"})
    assert caught.value.failure.code is PluginSessionFailureCode.SERIAL_DEVICE_ACCESS_DENIED
    assert dict(caught.value.failure.parameters) == {
        "device": "/dev/ttyUSB0", "group": "dialout"}


@pytest.mark.skipif(os.geteuid() == 0, reason="root can open anything")
def test_access_check_reads_the_devices_group(tmp_path, monkeypatch):
    monkeypatch.setattr(_flatpak, "is_flatpak", lambda: False)
    device = tmp_path / "ttyFAKE"
    device.write_text("")
    assert serial_mod._access_denied_group(str(device)) == ""
    device.chmod(0)
    try:
        import grp
        expected = grp.getgrgid(os.stat(device).st_gid).gr_name
        assert serial_mod._access_denied_group(str(device)) == expected
        assert serial_mod._access_denied_group(str(tmp_path / "absent")) == ""
    finally:
        device.chmod(stat.S_IRUSR | stat.S_IWUSR)


def test_serial_ports_prefer_stable_names(monkeypatch):
    by_id = "/dev/serial/by-id/usb-FTDI_A50-if00-port0"
    listing = {
        "/dev/serial/by-id/*": [by_id],
        "/dev/ttyUSB*": ["/dev/ttyUSB0", "/dev/ttyUSB1"],
    }
    monkeypatch.setattr(serial_mod.glob, "glob", lambda pattern: listing.get(pattern, []))
    monkeypatch.setattr(serial_mod.os.path, "realpath",
                        lambda path: "/dev/ttyUSB0" if path == by_id else path)
    assert serial_mod._serial_ports({}, None) == [
        (by_id, "usb-FTDI_A50-if00-port0 (ttyUSB0)"),
        ("/dev/ttyUSB1", "/dev/ttyUSB1"),
    ]


# --- docker -----------------------------------------------------------------

@pytest.mark.parametrize("runtime,flag", [("docker", "--context"), ("podman", "--connection")])
def test_a_context_selects_the_daemon(installed, runtime, flag):
    argv = _spawn(DockerProtocolBackend(), {
        "container": "web", "runtime": runtime, "docker_context": "prod"}).argv
    assert argv[:3] == [f"/bin/{runtime}", flag, "prod"]


def test_context_and_daemon_host_are_alternatives():
    errors = DockerProtocolBackend().validate(
        {"container": "web", "docker_context": "prod", "docker_host": "tcp://h:2375"})
    assert any("either" in e for e in errors)


def test_container_suggestions_put_running_ones_first():
    ctx = _Ctx({"ps -a": CommandResult(
        0, "old\tExited (0) 2 days ago\nweb\tUp 3 hours\n", "")})
    assert docker_mod._containers({"runtime": "podman", "docker_host": "tcp://h"}, ctx) == [
        ("web", "web — Up 3 hours"),
        ("old", "old — Exited (0) 2 days ago"),
    ]
    assert ctx.commands[0].startswith("podman -H tcp://h ps -a")


def test_a_failing_tool_reports_its_own_message():
    ctx = _Ctx({"context ls": CommandResult(1, "", "Cannot connect to the Docker daemon\nmore")})
    with pytest.raises(RuntimeError, match="Cannot connect to the Docker daemon"):
        docker_mod._contexts({"runtime": "docker"}, ctx)


# --- kubernetes -------------------------------------------------------------

def test_workloads_are_offered_in_the_form_exec_takes():
    ctx = _Ctx({"get deployments": CommandResult(0, "\n".join([
        "deployment.apps/web", "statefulset.apps/db", "service/api",
        "pod/web-7d9-x2", "replicaset.apps/ignored",
    ]), "")})
    assert [v for v, _l in k8s_mod._targets({"namespace": "prod"}, ctx)] == [
        "deploy/web", "sts/db", "svc/api", "web-7d9-x2"]
    assert " -n prod " in ctx.commands[0]


def test_containers_come_from_the_pod_or_its_template():
    ctx = _Ctx({"jsonpath": CommandResult(0, "app sidecar", "")})
    assert k8s_mod._containers({"pod": "deploy/web"}, ctx) == [
        ("app", "app"), ("sidecar", "sidecar")]
    assert "deploy/web" in ctx.commands[-1] and "template" in ctx.commands[-1]
    k8s_mod._containers({"pod": "web-0"}, ctx)
    assert "pod/web-0" in ctx.commands[-1] and "template" not in ctx.commands[-1]
    assert k8s_mod._containers({"pod": "svc/api"}, ctx) == []


def test_contexts_are_read_without_the_selected_one():
    ctx = _Ctx({"get-contexts": CommandResult(0, "prod\nstaging\n", "")})
    assert k8s_mod._contexts({"kube_context": "prod", "kubeconfig": "/k"}, ctx) == [
        ("prod", "prod"), ("staging", "staging")]
    assert "--context" not in ctx.commands[0] and "--kubeconfig /k" in ctx.commands[0]


# --- rdp --------------------------------------------------------------------

@pytest.fixture
def rdp_client(monkeypatch):
    monkeypatch.setattr(_flatpak, "resolve_host_binary",
                        lambda name: ["/bin/xfreerdp3"] if name == "xfreerdp3" else None)


@pytest.mark.parametrize("data,flag,absent", [
    ({"display_mode": "fit"}, "/smart-sizing", "/dynamic-resolution"),
    ({"display_mode": "original"}, None, "/dynamic-resolution"),
    ({"display_mode": "fullscreen"}, "/f", "/dynamic-resolution"),
    ({}, "/dynamic-resolution", "/f"),
    # Saved before the display choice existed.
    ({"fullscreen": True}, "/f", "/dynamic-resolution"),
    ({"dynamic_resolution": False}, None, "/dynamic-resolution"),
])
def test_one_display_choice_maps_to_one_freerdp_mode(rdp_client, data, flag, absent):
    argv = _spawn(RdpProtocolBackend(), {"host": "win", "client": "x11", **data}).argv
    if flag:
        assert flag in argv
    assert absent not in argv


def test_gateway_login_is_separate_only_when_given(rdp_client):
    shared = _spawn(RdpProtocolBackend(), {"host": "win", "client": "x11",
                                           "gateway": "gw:443"}).argv
    assert "/gateway:g:gw:443" in shared
    own = _spawn(RdpProtocolBackend(), {
        "host": "win", "client": "x11", "gateway": "gw:443",
        "gateway_username": "gwuser", "gateway_domain": "CORP"}).argv
    assert "/gateway:g:gw:443,u:gwuser,d:CORP" in own
    errors = RdpProtocolBackend().validate(
        {"host": "win", "gateway": "gw", "gateway_username": "a,b"})
    assert any("comma" in e for e in errors)

