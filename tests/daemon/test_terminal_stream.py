import os
import shutil
import socket
import subprocess
import sys
import threading
import time

import pytest

from sshpilot.api import DaemonClient, ErrorCode, SshPilotError
from sshpilot.api.models.sessions import (
    AttachSessionRequest,
    CloseSessionRequest,
    OpenSessionRequest,
    SessionState,
)
from sshpilot.api.models.terminal import (
    ReplayRequest,
    ResizeTerminalRequest,
    TerminalDimensions,
    TerminalInput,
)
from sshpilot.daemon.pty_runner import (
    DEFAULT_TERMINAL_INPUT_BYTES,
    PtySessionProcessRunner,
)
from sshpilot.daemon.terminal_stream import TerminalReplayBuffer


def _wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_replay_buffer_tracks_absolute_offsets_and_eviction():
    replay = TerminalReplayBuffer(8)
    assert replay.append(b"abc") == (0, 3)
    assert replay.append(b"defghij") == (3, 10)

    snapshot = replay.replay(0, 8)

    assert snapshot.available_start == 2
    assert snapshot.returned_start == 2
    assert snapshot.returned_end == 10
    assert b"".join(data for _sequence, data in snapshot.chunks) == b"cdefghij"
    assert snapshot.truncated is True
    assert replay.mark_eof() == 10
    assert replay.replay(10, 8).eof is True


def test_replay_buffer_trim_to_frees_oldest_bytes():
    replay = TerminalReplayBuffer(64)
    replay.append(b"abcdefghij")
    freed = replay.trim_to(4)
    assert freed == 6
    assert replay.retained_bytes == 4
    snapshot = replay.replay(0, 64)
    assert snapshot.truncated is True
    assert b"".join(data for _sequence, data in snapshot.chunks) == b"ghij"


def test_owned_pty_reports_tty_output_input_resize_and_exit():
    output = bytearray()
    eof = threading.Event()
    exited = threading.Event()
    exit_info = []
    script = (
        "import fcntl,os,struct,sys,termios,tty;tty.setraw(0);"
        "sys.stdout.write(str(os.isatty(0))+' '+"
        "str(os.tcgetpgrp(0)==os.getpgrp())+'\\n');sys.stdout.flush();"
        "data=os.read(0,4);"
        "size=struct.unpack('HHHH',fcntl.ioctl(0,termios.TIOCGWINSZ,b'\\0'*8));"
        "os.write(1,b'GOT:'+data+b':SIZE:'+str(size[:2]).encode());"
    )
    runner = PtySessionProcessRunner(
        lambda _spec: (
            (sys.executable, "-u", "-c", script),
            {"PATH": os.environ.get("PATH", "")},
        )
    )
    from sshpilot.api.models.common import ConnectionId, SessionId
    from sshpilot.daemon.session_runtime import SessionLaunchSpec

    spec = SessionLaunchSpec(
        session_id=SessionId(
            "session-1"
        ),
        connection_id=ConnectionId(
            "conn-2"
        ),
        protocol="ssh",
        hostname="example.test",
        username="alice",
        port=22,
        dimensions=TerminalDimensions(rows=24, columns=80),
    )
    handle = runner.start(
        spec,
        lambda info: (exit_info.append(info), exited.set()),
        output.extend,
        eof.set,
    )
    try:
        assert _wait_until(lambda: b"True True" in output)
        handle.resize(TerminalDimensions(rows=40, columns=100))
        assert not handle.write(b"x" * (DEFAULT_TERMINAL_INPUT_BYTES + 1))
        assert handle.write(b"\0abc")
        assert _wait_until(lambda: b"GOT:\0abc" in output)
        assert b"SIZE:(40, 100)" in output
        assert exited.wait(2)
        assert eof.wait(2)
        assert exit_info[0].exit_code == 0
    finally:
        runner.close()


def test_pty_launch_failure_releases_owned_resources():
    runner = PtySessionProcessRunner(
        lambda _spec: (
            ("/definitely/not/an/sshpilot/executable",),
            {"PATH": os.environ.get("PATH", "")},
        )
    )
    from sshpilot.api.models.common import ConnectionId, SessionId
    from sshpilot.daemon.session_runtime import SessionLaunchSpec

    spec = SessionLaunchSpec(
        session_id=SessionId(
            "session-1"
        ),
        connection_id=ConnectionId(
            "conn-2"
        ),
        protocol="ssh",
        hostname="example.test",
        username="alice",
        port=22,
    )
    try:
        with pytest.raises(SshPilotError) as raised:
            runner.start(spec, lambda _info: None, lambda _data: None, lambda: None)
        assert raised.value.code is ErrorCode.PTY_ALLOCATION_FAILED
        assert runner._handles == set()
        assert runner._io._states == {}
    finally:
        runner.close()


def test_owned_pty_streams_from_isolated_local_openssh(tmp_path):
    ssh = shutil.which("ssh")
    sshd = shutil.which("sshd")
    ssh_keygen = shutil.which("ssh-keygen")
    if not ssh or not sshd or not ssh_keygen:
        pytest.skip("OpenSSH integration tools are unavailable")

    listener = socket.socket()
    try:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    finally:
        listener.close()

    host_key = tmp_path / "host_key"
    client_key = tmp_path / "client_key"
    for key_path in (host_key, client_key):
        subprocess.run(
            (
                ssh_keygen,
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-f",
                str(key_path),
            ),
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    authorized_keys = tmp_path / "authorized_keys"
    authorized_keys.write_bytes(client_key.with_suffix(".pub").read_bytes())
    authorized_keys.chmod(0o600)
    username = os.environ.get("USER") or os.environ.get("LOGNAME")
    if not username:
        pytest.skip("Current user identity is unavailable")
    daemon_config = tmp_path / "sshd_config"
    daemon_config.write_text(
        "\n".join(
            (
                f"Port {port}",
                "ListenAddress 127.0.0.1",
                f"HostKey {host_key}",
                f"PidFile {tmp_path / 'sshd.pid'}",
                f"AuthorizedKeysFile {authorized_keys}",
                f"AllowUsers {username}",
                "PubkeyAuthentication yes",
                "PasswordAuthentication no",
                "KbdInteractiveAuthentication no",
                "UsePAM no",
                "StrictModes no",
                "LogLevel ERROR",
            )
        )
        + "\n"
    )
    client_config = tmp_path / "ssh_config"
    client_config.write_text(
        "\n".join(
            (
                "Host phase7-test",
                "    HostName 127.0.0.1",
                f"    Port {port}",
                f"    User {username}",
                f"    IdentityFile {client_key}",
                "    IdentitiesOnly yes",
                "    BatchMode yes",
                "    StrictHostKeyChecking no",
                "    UserKnownHostsFile /dev/null",
                "    LogLevel ERROR",
            )
        )
        + "\n"
    )
    daemon = subprocess.Popen(
        (sshd, "-D", "-f", str(daemon_config)),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    runner = None
    try:
        ready = False
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and daemon.poll() is None:
            probe = socket.socket()
            probe.settimeout(0.1)
            try:
                probe.connect(("127.0.0.1", port))
                ready = True
                break
            except OSError:
                time.sleep(0.02)
            finally:
                probe.close()
        if not ready:
            pytest.skip("Temporary sshd cannot run in this environment")

        output = bytearray()
        eof = threading.Event()
        exited = threading.Event()
        exit_info = []
        runner = PtySessionProcessRunner(
            lambda _spec: (
                (
                    ssh,
                    "-F",
                    str(client_config),
                    "phase7-test",
                    "printf 'SSHPILOT_OPENSSH_PTY_OK\\n'",
                ),
                {
                    "HOME": str(tmp_path),
                    "LANG": "C.UTF-8",
                    "LOGNAME": username,
                    "PATH": os.environ.get("PATH", ""),
                    "TERM": "xterm-256color",
                    "USER": username,
                },
            )
        )
        from sshpilot.api.models.common import ConnectionId, SessionId
        from sshpilot.daemon.session_runtime import SessionLaunchSpec

        runner.start(
            SessionLaunchSpec(
                session_id=SessionId(
                    "session-10"
                ),
                connection_id=ConnectionId(
                    "conn-11"
                ),
                protocol="ssh",
                hostname="127.0.0.1",
                username=username,
                port=port,
            ),
            lambda info: (exit_info.append(info), exited.set()),
            output.extend,
            eof.set,
        )
        assert exited.wait(5)
        assert eof.wait(5)
        assert b"SSHPILOT_OPENSSH_PTY_OK" in output
        assert exit_info[0].exit_code == 0
    finally:
        if runner is not None:
            runner.close()
        daemon.terminate()
        try:
            daemon.wait(timeout=3)
        except subprocess.TimeoutExpired:
            daemon.kill()
            daemon.wait(timeout=1)


def test_daemon_client_receives_replay_live_input_and_eof(daemon_factory):
    script = (
        "import os,time,tty;tty.setraw(0);"
        "os.write(1,b'BEFORE\\n');"
        "data=os.read(0,4);os.write(1,b'AFTER:'+data+b'\\n');"
    )
    runner = PtySessionProcessRunner(
        lambda _spec: (
            (sys.executable, "-u", "-c", script),
            {"PATH": os.environ.get("PATH", "")},
        )
    )
    server, _manager = daemon_factory(session_runner=runner)
    client = DaemonClient(socket_path=server.socket_path)
    viewer = DaemonClient(socket_path=server.socket_path)
    outputs = []
    viewer_outputs = []
    eof = threading.Event()
    input_errors = []
    try:
        opened = client.open_session(
            OpenSessionRequest(
                connection_id=client.list_connections()[0].id,
                dimensions=TerminalDimensions(rows=24, columns=80),
            )
        )
        assert opened.state is SessionState.STARTING
        subscription = client.subscribe_terminal(
            opened.id,
            outputs.append,
            on_eof=lambda _session_id, _sequence: eof.set(),
        )
        attached = client.attach_session(
            AttachSessionRequest(
                session_id=opened.id,
                request_input=True,
                want_terminal_output=True,
                from_sequence=0,
            )
        )
        assert attached.attachment.input_owner is True
        viewer_subscription = viewer.subscribe_terminal(
            opened.id,
            viewer_outputs.append,
            on_error=input_errors.append,
        )
        viewer_attachment = viewer.attach_session(
            AttachSessionRequest(
                session_id=opened.id,
                request_input=True,
                want_terminal_output=True,
                from_sequence=0,
            )
        )
        assert viewer_attachment.attachment.input_owner is False
        viewer.send_terminal_input(
            TerminalInput(
                session_id=opened.id,
                attachment_id=viewer_attachment.attachment.id,
                data=b"nope",
            )
        )
        assert _wait_until(lambda: bool(input_errors))
        assert input_errors[0].code is ErrorCode.TERMINAL_INPUT_OWNER_REQUIRED
        assert _wait_until(
            lambda: b"BEFORE" in b"".join(item.data for item in outputs)
        )
        client.resize_terminal(
            ResizeTerminalRequest(
                session_id=opened.id,
                attachment_id=attached.attachment.id,
                dimensions=TerminalDimensions(rows=40, columns=100),
            )
        )
        client.send_terminal_input(
            TerminalInput(
                session_id=opened.id,
                attachment_id=attached.attachment.id,
                data=b"ping",
            )
        )
        assert _wait_until(
            lambda: b"AFTER:ping" in b"".join(item.data for item in outputs)
        )
        assert _wait_until(
            lambda: b"AFTER:ping"
            in b"".join(item.data for item in viewer_outputs)
        )
        viewer_data = b"".join(item.data for item in viewer_outputs)
        assert b"BEFORE\n" in viewer_data
        assert b"AFTER:ping\n" in viewer_data
        viewer_chunks = [item for item in viewer_outputs if item.data]
        assert all(
            current.next_sequence == following.sequence
            for current, following in zip(
                viewer_chunks,
                viewer_chunks[1:],
            )
        )
        assert eof.wait(3)
        replay = client.replay_terminal(
            ReplayRequest(
                session_id=opened.id,
                attachment_id=attached.attachment.id,
                after_sequence=0,
            )
        )
        assert replay.next_sequence >= len(b"BEFORE\nAFTER:ping\n")
        assert _wait_until(
            lambda: client.get_session(opened.id).state is SessionState.CLOSED
        )
        subscription.close()
        viewer_subscription.close()
    finally:
        client.close()
        viewer.close()
        runner.close()


def test_close_terminates_exact_pty_child(daemon_factory):
    runner = PtySessionProcessRunner(
        lambda _spec: (
            (sys.executable, "-u", "-c", "import time;time.sleep(30)"),
            {"PATH": os.environ.get("PATH", "")},
        )
    )
    server, _manager = daemon_factory(session_runner=runner)
    client = DaemonClient(socket_path=server.socket_path)
    try:
        opened = client.open_session(
            OpenSessionRequest(connection_id=client.list_connections()[0].id)
        )
        assert _wait_until(
            lambda: client.get_session(opened.id).state is SessionState.RUNNING
        )
        client.close_session(CloseSessionRequest(session_id=opened.id))
        assert _wait_until(
            lambda: client.get_session(opened.id).state is SessionState.CLOSED
        )
    finally:
        client.close()
        runner.close()


def test_slow_terminal_subscriber_does_not_block_control_responses(
    daemon_factory,
):
    script = (
        "import os,time;"
        "os.write(1,b'BLOCK\\n');"
        "time.sleep(30)"
    )
    runner = PtySessionProcessRunner(
        lambda _spec: (
            (sys.executable, "-u", "-c", script),
            {"PATH": os.environ.get("PATH", "")},
        )
    )
    server, _manager = daemon_factory(session_runner=runner)
    client = DaemonClient(socket_path=server.socket_path)
    callback_entered = threading.Event()
    callback_release = threading.Event()

    def _blocked_output(_output):
        callback_entered.set()
        assert callback_release.wait(2)

    try:
        opened = client.open_session(
            OpenSessionRequest(connection_id=client.list_connections()[0].id)
        )
        subscription = client.subscribe_terminal(opened.id, _blocked_output)
        client.attach_session(
            AttachSessionRequest(
                session_id=opened.id,
                want_terminal_output=True,
                from_sequence=0,
            )
        )
        assert callback_entered.wait(2)

        started = time.monotonic()
        listed = client.list_connections()
        elapsed = time.monotonic() - started

        assert listed
        assert elapsed < 1
        callback_release.set()
        subscription.close()
        client.close_session(CloseSessionRequest(session_id=opened.id))
    finally:
        callback_release.set()
        client.close()
        runner.close()


def test_daemon_shutdown_kills_active_pty_and_stops_io_threads(
    daemon_factory,
):
    script = (
        "import os,signal,time;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        "os.write(1,b'READY');"
        "time.sleep(30)"
    )
    runner = PtySessionProcessRunner(
        lambda _spec: (
            (sys.executable, "-u", "-c", script),
            {"PATH": os.environ.get("PATH", "")},
        )
    )
    server, _manager = daemon_factory(
        session_runner=runner,
        session_shutdown_timeout=2.0,
    )
    client = DaemonClient(socket_path=server.socket_path)
    opened = client.open_session(
        OpenSessionRequest(connection_id=client.list_connections()[0].id)
    )
    output = []
    subscription = client.subscribe_terminal(opened.id, output.append)
    client.attach_session(
        AttachSessionRequest(
            session_id=opened.id,
            want_terminal_output=True,
            from_sequence=0,
        )
    )
    assert _wait_until(
        lambda: b"READY" in b"".join(item.data for item in output)
    )

    server.shutdown()

    assert server.wait_stopped(timeout=3)
    assert not runner._io._thread.is_alive()
    assert not runner._reaper.is_alive()
    assert runner._handles == set()
    subscription.close()
    client.close()


def test_terminal_subscriber_can_close_client_without_deadlock(
    daemon_factory,
):
    runner = PtySessionProcessRunner(
        lambda _spec: (
            (
                sys.executable,
                "-u",
                "-c",
                "import os,time;os.write(1,b'CLOSE');time.sleep(30)",
            ),
            {"PATH": os.environ.get("PATH", "")},
        )
    )
    server, _manager = daemon_factory(session_runner=runner)
    client = DaemonClient(socket_path=server.socket_path)
    closed = threading.Event()
    opened = client.open_session(
        OpenSessionRequest(connection_id=client.list_connections()[0].id)
    )

    def _close_from_output(_output):
        client.close()
        closed.set()

    client.subscribe_terminal(opened.id, _close_from_output)
    client.attach_session(
        AttachSessionRequest(
            session_id=opened.id,
            want_terminal_output=True,
            from_sequence=0,
        )
    )
    try:
        assert closed.wait(2)
    finally:
        client.close()
        server.shutdown()
        assert server.wait_stopped()
        runner.close()
