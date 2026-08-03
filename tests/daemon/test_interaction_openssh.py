import os
import shutil
import socket
import subprocess
import threading
import time

import pytest

from sshpilot.api.events import EventType
from sshpilot.api.models import (
    ClientId,
    ConnectionId,
    HostKeyDecision,
    InteractionDecisionRequest,
    InteractionType,
    RememberPolicy,
    SecretDecision,
    SessionId,
)
from sshpilot.api.transport.secret_frames import SecretFrame, SecretFrameKind
from sshpilot.daemon.interaction_broker import InteractionBroker
from sshpilot.daemon.pty_runner import PtySessionProcessRunner
from sshpilot.daemon.session_runtime import SessionLaunchSpec

pytestmark = pytest.mark.integration


def _free_port() -> int:
    listener = socket.socket()
    try:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]
    finally:
        listener.close()


def test_encrypted_key_and_unknown_host_use_typed_interactions(tmp_path) -> None:
    tools = {
        name: shutil.which(name)
        for name in ("ssh", "sshd", "ssh-keygen", "ssh-keyscan")
    }
    if not all(tools.values()):
        pytest.skip("OpenSSH integration tools are unavailable")
    username = os.environ.get("USER") or os.environ.get("LOGNAME")
    if not username:
        pytest.skip("Current user identity is unavailable")
    port = _free_port()
    host_key = tmp_path / "host_key"
    client_key = tmp_path / "client_key"
    subprocess.run(
        (
            tools["ssh-keygen"],
            "-q",
            "-t",
            "ed25519",
            "-N",
            "",
            "-f",
            str(host_key),
        ),
        check=True,
    )
    passphrase = "phase8-isolated-passphrase"
    subprocess.run(
        (
            tools["ssh-keygen"],
            "-q",
            "-t",
            "ed25519",
            "-N",
            passphrase,
            "-f",
            str(client_key),
        ),
        check=True,
    )
    authorized = tmp_path / "authorized_keys"
    authorized.write_bytes(client_key.with_suffix(".pub").read_bytes())
    authorized.chmod(0o600)
    sshd_config = tmp_path / "sshd_config"
    sshd_config.write_text(
        "\n".join(
            (
                f"Port {port}",
                "ListenAddress 127.0.0.1",
                f"HostKey {host_key}",
                f"PidFile {tmp_path / 'sshd.pid'}",
                f"AuthorizedKeysFile {authorized}",
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
    known_hosts = tmp_path / "known_hosts"
    ssh_config = tmp_path / "ssh_config"
    ssh_config.write_text(
        "\n".join(
            (
                "Host phase8-test",
                "    HostName 127.0.0.1",
                f"    Port {port}",
                f"    User {username}",
                f"    IdentityFile {client_key}",
                "    IdentitiesOnly yes",
                f"    UserKnownHostsFile {known_hosts}",
                "    StrictHostKeyChecking ask",
                "    RequestTTY force",
                "    RemoteCommand printf 'PHASE8_TYPED_AUTH_OK\\n'; sleep 1",
                "    LogLevel ERROR",
            )
        )
        + "\n"
    )
    daemon = subprocess.Popen(
        (tools["sshd"], "-D", "-f", str(sshd_config)),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    stored_passphrases = []
    broker = InteractionBroker(
        passphrase_store=lambda key, value: (
            stored_passphrases.append((key, value)) or True
        )
    )
    runner = None
    session_id = SessionId("session-20")
    connection_id = ConnectionId(
        "conn-21"
    )
    client_id = ClientId("client:phase8")
    seen = []

    def answer(event) -> None:
        if event.type is not EventType.INTERACTION_CREATED:
            return
        summary = event.payload
        seen.append(summary.type)
        claim = broker.claim(summary.id, client_id)
        if summary.type is InteractionType.HOST_KEY_CONFIRMATION:
            broker.respond(
                InteractionDecisionRequest(
                    interaction_id=summary.id,
                    host_key_decision=HostKeyDecision.ACCEPT,
                ),
                client_id,
            )
        else:
            broker.respond(
                InteractionDecisionRequest(
                    interaction_id=summary.id,
                    secret_decision=SecretDecision.SUBMIT,
                    remember_policy=RememberPolicy.STORE_AFTER_SUCCESS,
                ),
                client_id,
            )
            broker.submit_secret(
                SecretFrame(
                    kind=SecretFrameKind.RESPONSE,
                    interaction_id=summary.id,
                    nonce=bytes.fromhex(claim.nonce),
                    secret=bytearray(passphrase.encode()),
                ),
                client_id,
            )

    broker.subscribe_events(answer)
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and daemon.poll() is None:
            probe = socket.socket()
            probe.settimeout(0.1)
            try:
                probe.connect(("127.0.0.1", port))
                break
            except OSError:
                time.sleep(0.02)
            finally:
                probe.close()
        else:
            pytest.skip("Temporary sshd cannot run in this environment")

        output = bytearray()
        exited = threading.Event()

        def collect_output(data):
            output.extend(data)
            if b"PHASE8_TYPED_AUTH_OK" in output:
                broker.mark_authenticated(session_id)

        runner = PtySessionProcessRunner(
            lambda spec: broker.prepare_launch(
                spec,
                lambda _connection_id, **_kwargs: (
                    (tools["ssh"], "-F", str(ssh_config), "phase8-test"),
                    {
                        "HOME": str(tmp_path),
                        "LANG": "C.UTF-8",
                        "LOGNAME": username,
                        "PATH": os.environ.get("PATH", ""),
                        "TERM": "xterm-256color",
                        "USER": username,
                    },
                ),
            )
        )
        runner.start(
            SessionLaunchSpec(
                session_id=session_id,
                connection_id=connection_id,
                protocol="ssh",
                hostname="127.0.0.1",
                username=username,
                port=port,
            ),
            lambda _info: exited.set(),
            collect_output,
            lambda: None,
        )
        assert exited.wait(8)
        assert b"PHASE8_TYPED_AUTH_OK" in output
        assert passphrase.encode() not in output
        assert seen == [
            InteractionType.HOST_KEY_CONFIRMATION,
            InteractionType.PRIVATE_KEY_PASSPHRASE,
        ]
        assert stored_passphrases == [(str(client_key), passphrase)]
        assert known_hosts.is_file()
    finally:
        if runner is not None:
            runner.close()
        broker.close()
        daemon.terminate()
        try:
            daemon.wait(timeout=3)
        except subprocess.TimeoutExpired:
            daemon.kill()
            daemon.wait(timeout=1)


def _wait_for_port(port: int, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        probe = socket.socket()
        probe.settimeout(0.1)
        try:
            probe.connect(("127.0.0.1", port))
            return True
        except OSError:
            time.sleep(0.02)
        finally:
            probe.close()
    return False


def test_stored_passphrase_autofills_without_pythonpath(tmp_path) -> None:
    """Stored passphrase must autofill even when askpass has no PYTHONPATH.

    Reproduces the daemon failure mode where OpenSSH's askpass child runs with a
    reduced environment: the old ``from sshpilot.daemon.askpass_helper import main``
    wrapper exits 1, askpass never reaches the broker, and the user is prompted
    (or auth fails). The inlined helper must not import sshpilot.
    """
    tools = {
        name: shutil.which(name)
        for name in ("ssh", "sshd", "ssh-keygen", "ssh-keyscan")
    }
    if not all(tools.values()):
        pytest.skip("OpenSSH integration tools are unavailable")
    username = os.environ.get("USER") or os.environ.get("LOGNAME")
    if not username:
        pytest.skip("Current user identity is unavailable")
    port = _free_port()
    host_key = tmp_path / "host_key"
    client_key = tmp_path / "client_key"
    subprocess.run(
        (tools["ssh-keygen"], "-q", "-t", "ed25519", "-N", "", "-f", str(host_key)),
        check=True,
    )
    passphrase = "stored-autofill-passphrase"
    subprocess.run(
        (
            tools["ssh-keygen"],
            "-q",
            "-t",
            "ed25519",
            "-N",
            passphrase,
            "-f",
            str(client_key),
        ),
        check=True,
    )
    authorized = tmp_path / "authorized_keys"
    authorized.write_bytes(client_key.with_suffix(".pub").read_bytes())
    authorized.chmod(0o600)
    sshd_config = tmp_path / "sshd_config"
    sshd_config.write_text(
        "\n".join(
            (
                f"Port {port}",
                "ListenAddress 127.0.0.1",
                f"HostKey {host_key}",
                f"PidFile {tmp_path / 'sshd.pid'}",
                f"AuthorizedKeysFile {authorized}",
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
    known_hosts = tmp_path / "known_hosts"
    ssh_config = tmp_path / "ssh_config"
    ssh_config.write_text(
        "\n".join(
            (
                "Host autofill-test",
                "    HostName 127.0.0.1",
                f"    Port {port}",
                f"    User {username}",
                f"    IdentityFile {client_key}",
                "    IdentitiesOnly yes",
                f"    UserKnownHostsFile {known_hosts}",
                "    StrictHostKeyChecking ask",
                "    RequestTTY force",
                "    RemoteCommand printf 'AUTOFILL_OK\\n'; sleep 1",
                "    LogLevel ERROR",
            )
        )
        + "\n"
    )
    daemon = subprocess.Popen(
        (tools["sshd"], "-D", "-f", str(sshd_config)),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    lookups = []
    interactions = []
    broker = InteractionBroker(
        passphrase_lookup=lambda key_path: (
            lookups.append(key_path) or passphrase
        ),
    )
    helper_source = broker._askpass_helper_path.read_text()
    assert "from sshpilot" not in helper_source
    assert "def main(" in helper_source
    runner = None
    session_id = SessionId("session-50")
    connection_id = ConnectionId(
        "conn-51"
    )
    client_id = ClientId("client:autofill")

    def answer(event) -> None:
        if event.type is not EventType.INTERACTION_CREATED:
            return
        summary = event.payload
        interactions.append(summary.type)
        broker.claim(summary.id, client_id)
        if summary.type is InteractionType.HOST_KEY_CONFIRMATION:
            broker.respond(
                InteractionDecisionRequest(
                    interaction_id=summary.id,
                    host_key_decision=HostKeyDecision.ACCEPT,
                ),
                client_id,
            )
        else:
            raise AssertionError(
                f"stored passphrase must not prompt for {summary.type}"
            )

    broker.subscribe_events(answer)
    try:
        if not _wait_for_port(port):
            pytest.skip("Temporary sshd cannot run in this environment")
        output = bytearray()
        exited = threading.Event()

        def launch(spec):
            argv, environment = broker.prepare_launch(
                spec,
                lambda _connection_id, **_kwargs: (
                    (tools["ssh"], "-F", str(ssh_config), "autofill-test"),
                    {
                        "HOME": str(tmp_path),
                        "LANG": "C.UTF-8",
                        "LOGNAME": username,
                        "PATH": os.environ.get("PATH", ""),
                        "TERM": "xterm-256color",
                        "USER": username,
                        # No SSH_AUTH_SOCK — force askpass passphrase path.
                    },
                ),
            )
            # Hostile askpass env: OpenSSH may not forward PYTHONPATH.
            environment.pop("PYTHONPATH", None)
            return argv, environment

        runner = PtySessionProcessRunner(launch)
        runner.start(
            SessionLaunchSpec(
                session_id=session_id,
                connection_id=connection_id,
                protocol="ssh",
                hostname="127.0.0.1",
                username=username,
                port=port,
            ),
            lambda _info: exited.set(),
            output.extend,
            lambda: None,
        )
        assert exited.wait(12)
        assert b"AUTOFILL_OK" in output
        assert passphrase.encode() not in output
        assert lookups == [str(client_key)]
        assert interactions == [InteractionType.HOST_KEY_CONFIRMATION]
        assert InteractionType.PRIVATE_KEY_PASSPHRASE not in interactions
    finally:
        if runner is not None:
            runner.close()
        broker.close()
        daemon.terminate()
        try:
            daemon.wait(timeout=3)
        except subprocess.TimeoutExpired:
            daemon.kill()
            daemon.wait(timeout=1)


def test_accept_persists_known_hosts_according_to_openssh(tmp_path) -> None:
    tools = {
        name: shutil.which(name)
        for name in ("ssh", "sshd", "ssh-keygen", "ssh-keyscan")
    }
    if not all(tools.values()):
        pytest.skip("OpenSSH integration tools are unavailable")
    username = os.environ.get("USER") or os.environ.get("LOGNAME")
    if not username:
        pytest.skip("Current user identity is unavailable")
    port = _free_port()
    host_key = tmp_path / "host_key"
    client_key = tmp_path / "client_key"
    subprocess.run(
        (tools["ssh-keygen"], "-q", "-t", "ed25519", "-N", "", "-f", str(host_key)),
        check=True,
    )
    subprocess.run(
        (tools["ssh-keygen"], "-q", "-t", "ed25519", "-N", "", "-f", str(client_key)),
        check=True,
    )
    authorized = tmp_path / "authorized_keys"
    authorized.write_bytes(client_key.with_suffix(".pub").read_bytes())
    authorized.chmod(0o600)
    sshd_config = tmp_path / "sshd_config"
    sshd_config.write_text(
        "\n".join(
            (
                f"Port {port}",
                "ListenAddress 127.0.0.1",
                f"HostKey {host_key}",
                f"PidFile {tmp_path / 'sshd.pid'}",
                f"AuthorizedKeysFile {authorized}",
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
    known_hosts = tmp_path / "known_hosts"
    ssh_config = tmp_path / "ssh_config"
    ssh_config.write_text(
        "\n".join(
            (
                "Host phase8-store",
                "    HostName 127.0.0.1",
                f"    Port {port}",
                f"    User {username}",
                f"    IdentityFile {client_key}",
                "    IdentitiesOnly yes",
                f"    UserKnownHostsFile {known_hosts}",
                "    StrictHostKeyChecking ask",
                "    RequestTTY force",
                "    RemoteCommand printf 'PHASE8_STORE_OK\\n'; sleep 1",
                "    LogLevel ERROR",
            )
        )
        + "\n"
    )
    daemon = subprocess.Popen(
        (tools["sshd"], "-D", "-f", str(sshd_config)),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    broker = InteractionBroker()
    runner = None
    session_id = SessionId("session-30")
    connection_id = ConnectionId(
        "conn-31"
    )
    client_id = ClientId("client:phase8-store")

    def answer(event) -> None:
        if event.type is not EventType.INTERACTION_CREATED:
            return
        summary = event.payload
        broker.claim(summary.id, client_id)
        if summary.type is InteractionType.HOST_KEY_CONFIRMATION:
            broker.respond(
                InteractionDecisionRequest(
                    interaction_id=summary.id,
                    host_key_decision=HostKeyDecision.ACCEPT,
                ),
                client_id,
            )

    broker.subscribe_events(answer)
    try:
        if not _wait_for_port(port):
            pytest.skip("Temporary sshd cannot run in this environment")
        output = bytearray()
        exited = threading.Event()

        def collect_output(data):
            output.extend(data)
            if b"PHASE8_STORE_OK" in output:
                broker.mark_authenticated(session_id)

        runner = PtySessionProcessRunner(
            lambda spec: broker.prepare_launch(
                spec,
                lambda _connection_id, **_kwargs: (
                    (tools["ssh"], "-F", str(ssh_config), "phase8-store"),
                    {
                        "HOME": str(tmp_path),
                        "LANG": "C.UTF-8",
                        "LOGNAME": username,
                        "PATH": os.environ.get("PATH", ""),
                        "TERM": "xterm-256color",
                        "USER": username,
                    },
                ),
            )
        )
        runner.start(
            SessionLaunchSpec(
                session_id=session_id,
                connection_id=connection_id,
                protocol="ssh",
                hostname="127.0.0.1",
                username=username,
                port=port,
            ),
            lambda _info: exited.set(),
            collect_output,
            lambda: None,
        )
        assert exited.wait(8)
        assert b"PHASE8_STORE_OK" in output
        assert known_hosts.is_file()
        assert known_hosts.stat().st_mode & 0o777 == 0o600
        content = known_hosts.read_text()
        assert "ssh-ed25519" in content
        assert "127.0.0.1" in content or content.startswith("|1|")
    finally:
        if runner is not None:
            runner.close()
        broker.close()
        daemon.terminate()
        try:
            daemon.wait(timeout=3)
        except subprocess.TimeoutExpired:
            daemon.kill()
            daemon.wait(timeout=1)


def test_accept_allows_openssh_to_persist_user_known_hosts(tmp_path) -> None:
    """Accept returns yes and leaves persistence to OpenSSH configuration."""
    tools = {
        name: shutil.which(name)
        for name in ("ssh", "sshd", "ssh-keygen")
    }
    if not all(tools.values()):
        pytest.skip("OpenSSH integration tools are unavailable")
    username = os.environ.get("USER") or os.environ.get("LOGNAME")
    if not username:
        pytest.skip("Current user identity is unavailable")
    port = _free_port()
    host_key = tmp_path / "host_key"
    client_key = tmp_path / "client_key"
    subprocess.run(
        (tools["ssh-keygen"], "-q", "-t", "ed25519", "-N", "", "-f", str(host_key)),
        check=True,
    )
    subprocess.run(
        (tools["ssh-keygen"], "-q", "-t", "ed25519", "-N", "", "-f", str(client_key)),
        check=True,
    )
    authorized = tmp_path / "authorized_keys"
    authorized.write_bytes(client_key.with_suffix(".pub").read_bytes())
    authorized.chmod(0o600)
    sshd_config = tmp_path / "sshd_config"
    sshd_config.write_text(
        "\n".join(
            (
                f"Port {port}",
                "ListenAddress 127.0.0.1",
                f"HostKey {host_key}",
                f"PidFile {tmp_path / 'sshd.pid'}",
                f"AuthorizedKeysFile {authorized}",
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
    known_hosts = tmp_path / "known_hosts"
    ssh_config = tmp_path / "ssh_config"
    ssh_config.write_text(
        "\n".join(
            (
                "Host phase8-once",
                "    HostName 127.0.0.1",
                f"    Port {port}",
                f"    User {username}",
                f"    IdentityFile {client_key}",
                "    IdentitiesOnly yes",
                f"    UserKnownHostsFile {known_hosts}",
                "    StrictHostKeyChecking ask",
                "    RequestTTY force",
                "    RemoteCommand printf 'PHASE8_ONCE_OK\\n'; sleep 1",
                "    LogLevel ERROR",
            )
        )
        + "\n"
    )
    daemon = subprocess.Popen(
        (tools["sshd"], "-D", "-f", str(sshd_config)),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    broker = InteractionBroker()
    runner = None
    session_id = SessionId("session-40")
    connection_id = ConnectionId(
        "conn-41"
    )
    client_id = ClientId("client:phase8-once")

    def answer(event) -> None:
        if event.type is not EventType.INTERACTION_CREATED:
            return
        summary = event.payload
        if summary.type is not InteractionType.HOST_KEY_CONFIRMATION:
            return
        broker.claim(summary.id, client_id)
        broker.respond(
            InteractionDecisionRequest(
                interaction_id=summary.id,
                host_key_decision=HostKeyDecision.ACCEPT,
            ),
            client_id,
        )

    broker.subscribe_events(answer)
    try:
        if not _wait_for_port(port):
            pytest.skip("Temporary sshd cannot run in this environment")
        output = bytearray()
        exited = threading.Event()
        runner = PtySessionProcessRunner(
            lambda spec: broker.prepare_launch(
                spec,
                lambda _connection_id, **_kwargs: (
                    (tools["ssh"], "-F", str(ssh_config), "phase8-once"),
                    {
                        "HOME": str(tmp_path),
                        "LANG": "C.UTF-8",
                        "LOGNAME": username,
                        "PATH": os.environ.get("PATH", ""),
                        "TERM": "xterm-256color",
                        "USER": username,
                    },
                ),
            )
        )
        runner.start(
            SessionLaunchSpec(
                session_id=session_id,
                connection_id=connection_id,
                protocol="ssh",
                hostname="127.0.0.1",
                username=username,
                port=port,
            ),
            lambda _info: exited.set(),
            output.extend,
            lambda: None,
        )
        assert exited.wait(8)
        assert b"PHASE8_ONCE_OK" in output
        assert known_hosts.is_file()
    finally:
        if runner is not None:
            runner.close()
        broker.close()
        daemon.terminate()
        try:
            daemon.wait(timeout=3)
        except subprocess.TimeoutExpired:
            daemon.kill()
            daemon.wait(timeout=1)
