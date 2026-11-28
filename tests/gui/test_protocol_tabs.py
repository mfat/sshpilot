"""Non-SSH protocol tabs, driven through the real main window.

Each test saves a connection the way the app does (through the daemon), opens
it from the window, and proves two things a user sees:

* the far side is a real shell: the marker only appears once that shell has
  evaluated ``$((6*7))``, so typed text echoed back by a dead target cannot
  pass;
* the "Connecting" veil is gone once the session is up. #1432: every plugin
  tab connected but kept the veil, and nothing below the window could see it.

The daemon spawns plugin clients with its own environment, so every test starts
with a ``TERM`` no terminfo database knows. Mosh refuses to start with one
(#1432), and the remote shell must report the terminal sshPilot actually
provides.

Each protocol skips when its client program or target cannot run here: CI's
"Protocol tabs in a real window" job installs all of them.

Opt-in: SSHPILOT_GUI_TESTS=1 xvfb-run -a pytest -m gui tests/gui/test_protocol_tabs.py
"""

from __future__ import annotations

import os
import pwd
import secrets
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

from tests._gui_harness import requires_gui

Gtk, Adw, Gio, GLib = requires_gui()

try:
    import gi

    gi.require_version("Vte", "3.91")
    from gi.repository import Vte  # noqa: F401
except Exception as error:  # pragma: no cover
    pytest.skip(f"VTE unavailable: {error}", allow_module_level=True)

pytestmark = pytest.mark.gui

COMMAND = "printf 'SHELL_%s_OK\\n' $((6*7))\n"
MARKER = "SHELL_42_OK"
TERM_COMMAND = "printf 'TERM_%s_OK\\n' \"$TERM\"\n"
TERM_MARKER = "TERM_xterm-256color_OK"
UNKNOWN_TERM = "sshpilot-unknown-term"
ALPINE = "docker.io/library/alpine:3.20"

# Read before the harness points HOME at a scratch directory.
_LOGIN_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
_KUBECONFIG = os.environ.get("KUBECONFIG") or str(_LOGIN_HOME / ".kube" / "config")


# -- helpers -----------------------------------------------------------------


def _free_tcp_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_tcp(port: int, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.2)
    raise AssertionError(f"nothing listens on 127.0.0.1:{port}")


def _require(*programs: str) -> None:
    missing = [name for name in programs if shutil.which(name) is None]
    if missing:
        pytest.skip(f"not installed: {', '.join(missing)}")


def _runtime_works(runtime: str) -> bool:
    from tests.fixtures.temporary_openssh import _container_env

    if shutil.which(runtime) is None:
        return False
    try:
        return subprocess.run(
            [runtime, "info"],
            env=_container_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _container(runtime: str, *args: str, timeout: float = 180.0):
    from tests.fixtures.temporary_openssh import _container_env

    return subprocess.run(
        [runtime, *args],
        env=_container_env(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _start_alpine(runtime: str, name: str, *args: str) -> None:
    result = _container(runtime, "run", "-d", "--rm", "--name", name, *args)
    assert result.returncode == 0, f"{runtime} run failed: {result.stderr.strip()}"


def _remove_container(runtime: str, name: str) -> None:
    try:
        _container(runtime, "rm", "-f", name, timeout=60.0)
    except Exception:
        pass


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _add_plugin_connection(h, nickname: str, protocol: str, fields: dict):
    """Save a plugin connection through the daemon, like the editor does."""
    win = h.gui.window
    data = {"nickname": nickname, "protocol": protocol, **fields}
    created = win.plugin_connection_services.add_connection_from_data(data)
    store = win.connection_manager
    h.pump_until(
        lambda: store.get_connection_by_id(str(created.id)) is not None,
        timeout=10.0,
        label=f"connection {nickname!r} in the sidebar store",
    )
    h.pump(200)
    connection = store.get_connection_by_id(str(created.id))
    assert connection.protocol == protocol
    return connection


def _terminal_text(term) -> str:
    vte = getattr(term, "vte", None) or getattr(getattr(term, "backend", None), "vte", None)
    if vte is None:
        return ""
    content = vte.get_text_format(Vte.Format.TEXT)
    return (content[0] if isinstance(content, tuple) else content) or ""


def _child_processes() -> str:
    """This test's descendant processes, to show where a launch hangs."""
    parents: dict[int, int] = {}
    commands: dict[int, str] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
            cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue
        parents[int(entry.name)] = int(stat.rsplit(")", 1)[1].split()[1])
        commands[int(entry.name)] = cmdline.strip()
    found, todo = [], [os.getpid()]
    while todo:
        pid = todo.pop()
        children = [child for child, parent in parents.items() if parent == pid]
        found += children
        todo += children
    return "; ".join(f"{pid}: {commands[pid][:200]}" for pid in sorted(found)) or "none"


def _evidence(term) -> str:
    return f"terminal shows {_terminal_text(term)[-400:]!r}; processes: {_child_processes()}"


def _wait_for(h, term, marker: str, *, timeout: float = 30.0) -> str:
    from tests.gui._phase14_harness import Phase14EvidenceError

    try:
        return h.wait_for_marker(marker, terminal=term, timeout=timeout)
    except Phase14EvidenceError as error:
        raise AssertionError(
            f"{error}; {_evidence(term)}"
        ) from error


def _wait_for_prompt(h, term, *, timeout: float) -> None:
    """Type only once the shell shows a prompt, as a person would.

    Clients such as ``docker exec -it`` switch their terminal to raw mode
    with a flush once attached, which discards anything typed before.
    """
    def _prompt():
        lines = [line.rstrip() for line in _terminal_text(term).splitlines() if line.strip()]
        return bool(lines) and lines[-1][-1:] in ("$", "#", ">", "%")

    try:
        try:
            h.pump_until(_prompt, timeout=10.0, label="shell prompt")
        except AssertionError:
            # A serial console prints nothing until it is poked: its first
            # prompt went out before the client opened the line.
            h.emit_terminal_input("\n", term)
            h.pump_until(_prompt, timeout=timeout, label="shell prompt after Enter")
    except AssertionError as error:
        raise AssertionError(f"{error}; {_evidence(term)}") from error


def _open_and_prove_shell(h, connection, *, monkeypatch, timeout: float = 60.0):
    """Open the tab from the window and prove a live shell sits behind it."""
    monkeypatch.setenv("TERM", UNKNOWN_TERM)
    win = h.gui.window
    win.terminal_manager.connect_to_host(connection, force_new=True)
    h.pump_until(
        lambda: h.find_terminal_widget(connection) is not None,
        timeout=20.0,
        label="terminal widget created",
    )
    term = h.find_terminal_widget(connection)
    h.pump_until(
        lambda: bool(getattr(term, "is_connected", False)),
        timeout=timeout,
        label=f"{connection.protocol} tab connected",
    )
    try:
        h.ensure_input_ready(term, timeout=20.0)
    except AssertionError as error:
        raise AssertionError(f"{error}; {_evidence(term)}") from error
    _wait_for_prompt(h, term, timeout=timeout)
    h.emit_terminal_input(COMMAND, term)
    assert MARKER in _wait_for(h, term, MARKER, timeout=timeout)

    # The session is up and answering, so nothing may still cover the
    # terminal (#1432).
    h.pump_until(
        lambda: getattr(term, "_overlay_mode", None) == "none",
        timeout=10.0,
        label=f"Connecting veil lifted (mode={getattr(term, '_overlay_mode', None)!r})",
    )
    assert not term.connecting_bg.get_visible()
    assert not term.connecting_box.get_visible()
    return term


# -- protocols ---------------------------------------------------------------


def test_telnet_tab_runs_a_remote_command(phase14_harness, monkeypatch):
    _require("telnet", "socat")
    h = phase14_harness
    port = _free_tcp_port()
    # A shell per TCP connection; "fork" keeps the readiness probe harmless.
    server = subprocess.Popen(
        [
            "socat",
            f"TCP-LISTEN:{port},bind=127.0.0.1,reuseaddr,fork",
            "EXEC:sh -i,pty,stderr,setsid,ctty,sane",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_tcp(port)
        conn = _add_plugin_connection(
            h, "GuiTelnet", "telnet", {"host": "127.0.0.1", "port": port}
        )
        _open_and_prove_shell(h, conn, monkeypatch=monkeypatch)
    finally:
        _stop(server)


def test_serial_tab_runs_a_command_on_the_device(phase14_harness, monkeypatch, tmp_path):
    _require("picocom", "socat")
    h = phase14_harness
    device = tmp_path / "ttyGUI"
    # The far end of the pty pair is a shell, standing in for a device console.
    line = subprocess.Popen(
        [
            "socat",
            f"PTY,raw,echo=0,link={device}",
            "EXEC:sh -i,pty,stderr,setsid,ctty,sane",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not device.exists():
            time.sleep(0.1)
        assert device.exists(), "socat did not create the serial device"
        conn = _add_plugin_connection(
            h,
            "GuiSerial",
            "serial",
            {"device": str(device), "baud": "115200", "flow": "none"},
        )
        _open_and_prove_shell(h, conn, monkeypatch=monkeypatch)
    finally:
        _stop(line)


@pytest.mark.parametrize("runtime", ["docker", "podman"])
def test_container_tab_runs_a_command_in_the_container(
    phase14_harness, monkeypatch, tmp_path, runtime
):
    if not _runtime_works(runtime):
        pytest.skip(f"{runtime} cannot run containers here")
    h = phase14_harness
    name = f"sshpilot-gui-{runtime}-{secrets.token_hex(3)}"
    fields = {"container": name, "runtime": runtime, "command": "sh"}
    service = None
    if runtime == "podman":
        # The test isolates XDG_RUNTIME_DIR, where rootless podman keeps its
        # container state, so reach the user's containers through an API
        # socket: the editor's "Daemon host" field.
        from tests.fixtures.temporary_openssh import _container_env

        sock = Path(f"/tmp/sshpilot-gui-podman-{secrets.token_hex(4)}.sock")
        service = subprocess.Popen(
            ["podman", "system", "service", "--time=0", f"unix://{sock}"],
            env=_container_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not sock.exists():
            time.sleep(0.1)
        assert sock.exists(), "podman API socket never appeared"
        fields["docker_host"] = f"unix://{sock}"
    try:
        _start_alpine(runtime, name, ALPINE, "sleep", "600")
        conn = _add_plugin_connection(h, f"Gui{runtime.title()}", "docker", fields)
        _open_and_prove_shell(h, conn, monkeypatch=monkeypatch)
    finally:
        _remove_container(runtime, name)
        if service is not None:
            _stop(service)
            sock.unlink(missing_ok=True)


def _kubectl(*args: str, timeout: float = 60.0):
    return subprocess.run(
        ["kubectl", "--kubeconfig", _KUBECONFIG, *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _cluster_problem() -> str:
    """Why no cluster can be used here, or "" when one answers."""
    if shutil.which("kubectl") is None:
        return "kubectl not installed"
    if not Path(_KUBECONFIG).is_file():
        return f"no kubeconfig at {_KUBECONFIG}"
    problem = ""
    # A busy single-node cluster can miss one short probe.
    for _attempt in range(3):
        try:
            probe = _kubectl("--request-timeout=10s", "get", "--raw=/readyz", timeout=20)
        except (OSError, subprocess.TimeoutExpired) as error:
            problem = str(error)
        else:
            if probe.returncode == 0:
                return ""
            problem = (probe.stderr or probe.stdout).strip()
        time.sleep(3)
    return f"no reachable Kubernetes cluster in {_KUBECONFIG}: {problem}"


def test_kubernetes_tab_runs_a_command_in_the_pod(phase14_harness, monkeypatch):
    problem = _cluster_problem()
    if problem:
        pytest.skip(problem)
    h = phase14_harness
    pod = f"sshpilot-gui-{secrets.token_hex(3)}"
    started = _kubectl(
        "run", pod, f"--image={ALPINE}", "--restart=Never", "--", "sleep", "600"
    )
    assert started.returncode == 0, f"kubectl run failed: {started.stderr.strip()}"
    try:
        ready = _kubectl(
            "wait", "--for=condition=Ready", f"pod/{pod}", "--timeout=180s", timeout=200
        )
        assert ready.returncode == 0, f"pod never got ready: {ready.stderr.strip()}"
        conn = _add_plugin_connection(
            h,
            "GuiKubernetes",
            "k8s",
            {"pod": pod, "namespace": "default", "kubeconfig": _KUBECONFIG, "command": "sh"},
        )
        _open_and_prove_shell(h, conn, monkeypatch=monkeypatch)
    finally:
        _kubectl("delete", "pod", pod, "--now", "--wait=false")


def test_mosh_tab_runs_a_remote_command(phase14_harness, monkeypatch):
    _require("mosh")
    h = phase14_harness
    runtime = h.openssh.runtime
    sshd_port = _free_tcp_port()
    mosh_port = _free_udp_port()
    user = h.openssh.username
    pub = Path(f"{h.openssh.plain_key_path}.pub").read_text().strip()
    name = f"sshpilot-gui-mosh-{secrets.token_hex(3)}"
    # Host networking: a published UDP port collides with rootless podman's
    # pasta, and mosh-server must answer on the address ssh reached.
    boot = f"""
set -eu
apk add --no-cache openssh-server mosh >/dev/null
adduser -D -s /bin/sh {user}
# sshd refuses even key logins to a locked (passwordless) account.
echo '{user}:{secrets.token_hex(12)}' | chpasswd
mkdir -p /home/{user}/.ssh
echo '{pub}' > /home/{user}/.ssh/authorized_keys
chmod 700 /home/{user}/.ssh; chmod 600 /home/{user}/.ssh/authorized_keys
chown -R {user}:{user} /home/{user}/.ssh
ssh-keygen -A >/dev/null
printf 'Port {sshd_port}\\nListenAddress 127.0.0.1\\nPasswordAuthentication no\\nPubkeyAuthentication yes\\nAllowUsers {user}\\nPrintMotd no\\n' > /etc/ssh/sshd_config
exec /usr/sbin/sshd -D -e
"""
    _start_alpine(runtime, name, "--network", "host", ALPINE, "sh", "-c", boot)
    try:
        _wait_for_tcp(sshd_port, timeout=120.0)
        conn = _add_plugin_connection(
            h,
            "GuiMosh",
            "mosh",
            {
                "host": "127.0.0.1",
                "username": user,
                "port": sshd_port,
                "keyfile": str(h.openssh.plain_key_path),
                "mosh_port": str(mosh_port),
                "extra_ssh_opts": (
                    "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
                    "-o PreferredAuthentications=publickey -o IdentitiesOnly=yes"
                ),
            },
        )
        term = _open_and_prove_shell(h, conn, monkeypatch=monkeypatch, timeout=90.0)
        h.emit_terminal_input(TERM_COMMAND, term)
        assert TERM_MARKER in _wait_for(h, term, TERM_MARKER)
    finally:
        _remove_container(runtime, name)
