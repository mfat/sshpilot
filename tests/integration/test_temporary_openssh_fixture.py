"""Prove the Phase 13 temporary OpenSSH fixture works end-to-end."""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time

import pytest

from tests.fixtures.temporary_openssh import require_temporary_openssh


pytestmark = pytest.mark.integration


@pytest.fixture
def openssh(tmp_path):
    env = require_temporary_openssh(tmp_path)
    yield env
    env.destroy()


def _ssh_env(home: str) -> dict:
    env = os.environ.copy()
    env["HOME"] = home
    env["SSH_AUTH_SOCK"] = ""
    return env


def test_password_login(openssh):
    openssh.write_client_config("password")
    proc = subprocess.run(
        (
            "ssh",
            "-F",
            str(openssh.ssh_config),
            "-o",
            "BatchMode=no",
            openssh.host_alias_password,
            "echo",
            "PW_OK",
        ),
        input=openssh.password + "\n",
        capture_output=True,
        text=True,
        timeout=30,
        env={
            **_ssh_env(str(openssh.client_home)),
            "SSH_ASKPASS_REQUIRE": "force",
            "SSH_ASKPASS": "sh",
            "DISPLAY": ":0",
        },
    )
    # Prefer sshpass when available for deterministic password feed.
    sshpass = shutil.which("sshpass")
    if sshpass:
        proc = subprocess.run(
            (
                sshpass,
                "-p",
                openssh.password,
                "ssh",
                "-F",
                str(openssh.ssh_config),
                openssh.host_alias_password,
                "echo",
                "PW_OK",
            ),
            capture_output=True,
            text=True,
            timeout=30,
            env=_ssh_env(str(openssh.client_home)),
        )
    assert proc.returncode == 0, proc.stderr
    assert "PW_OK" in proc.stdout


def test_encrypted_key_login(openssh):
    openssh.write_client_config("key")
    ask = openssh.client_home / "askpass.sh"
    ask.write_text(f"#!/bin/sh\nprintf '%s\\n' '{openssh.encrypted_key_passphrase}'\n")
    ask.chmod(0o700)
    agent = subprocess.run(
        ["ssh-agent", "-s"],
        capture_output=True,
        text=True,
        check=True,
    )
    agent_env = _ssh_env(str(openssh.client_home))
    for line in agent.stdout.splitlines():
        if line.startswith("SSH_AUTH_SOCK=") or line.startswith("SSH_AGENT_PID="):
            key, _, rest = line.partition("=")
            agent_env[key] = rest.split(";")[0]
    add = subprocess.run(
        ["ssh-add", str(openssh.encrypted_key_path)],
        capture_output=True,
        text=True,
        timeout=20,
        env={
            **agent_env,
            "SSH_ASKPASS": str(ask),
            "SSH_ASKPASS_REQUIRE": "force",
            "DISPLAY": ":0",
        },
    )
    assert add.returncode == 0, add.stderr or add.stdout
    try:
        proc = subprocess.run(
            (
                "ssh",
                "-F",
                str(openssh.ssh_config),
                openssh.host_alias_key,
                "echo",
                "KEY_OK",
            ),
            capture_output=True,
            text=True,
            timeout=30,
            env=agent_env,
        )
        assert proc.returncode == 0, proc.stderr
        assert "KEY_OK" in proc.stdout
    finally:
        pid = agent_env.get("SSH_AGENT_PID")
        if pid:
            subprocess.run(["kill", pid], check=False)


def test_plain_key_login(openssh):
    openssh.write_client_config("key_plain")
    proc = subprocess.run(
        (
            "ssh",
            "-F",
            str(openssh.ssh_config),
            openssh.host_alias_key_plain,
            "echo",
            "PLAIN_OK",
        ),
        capture_output=True,
        text=True,
        timeout=30,
        env=_ssh_env(str(openssh.client_home)),
    )
    assert proc.returncode == 0, proc.stderr
    assert "PLAIN_OK" in proc.stdout


def test_wrong_password_rejected(openssh):
    openssh.write_client_config("password")
    sshpass = shutil.which("sshpass")
    if not sshpass:
        pytest.skip("sshpass required for wrong-password probe")
    proc = subprocess.run(
        (
            sshpass,
            "-p",
            "definitely-wrong",
            "ssh",
            "-F",
            str(openssh.ssh_config),
            "-o",
            "NumberOfPasswordPrompts=1",
            openssh.host_alias_password,
            "true",
        ),
        capture_output=True,
        text=True,
        timeout=30,
        env=_ssh_env(str(openssh.client_home)),
    )
    assert proc.returncode != 0


def test_sftp_listing(openssh):
    openssh.write_client_config("key_plain")
    proc = subprocess.run(
        (
            "sftp",
            "-b",
            "-",
            "-F",
            str(openssh.ssh_config),
            openssh.host_alias_key_plain,
        ),
        input="ls\nbye\n",
        capture_output=True,
        text=True,
        timeout=30,
        env=_ssh_env(str(openssh.client_home)),
    )
    assert proc.returncode == 0, proc.stderr


def test_local_forward_to_echo(openssh):
    openssh.write_client_config("key_plain")
    local_port = 0
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    local_port = listener.getsockname()[1]
    listener.close()
    fwd = subprocess.Popen(
        (
            "ssh",
            "-F",
            str(openssh.ssh_config),
            "-N",
            "-L",
            f"{local_port}:127.0.0.1:{openssh.remote_echo_port}",
            openssh.host_alias_key_plain,
        ),
        env=_ssh_env(str(openssh.client_home)),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 15
        body = b""
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", local_port), timeout=0.5) as sock:
                    sock.sendall(b"GET / HTTP/1.0\r\n\r\n")
                    body = sock.recv(64)
                    if body:
                        break
            except OSError:
                time.sleep(0.2)
        assert b"OK" in body, (body, fwd.stderr.read() if fwd.poll() is not None else "")
    finally:
        fwd.terminate()
        try:
            fwd.wait(timeout=5)
        except subprocess.TimeoutExpired:
            fwd.kill()


def test_dynamic_socks_forward(openssh):
    openssh.write_client_config("key_plain")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    socks_port = listener.getsockname()[1]
    listener.close()
    fwd = subprocess.Popen(
        (
            "ssh",
            "-F",
            str(openssh.ssh_config),
            "-N",
            "-D",
            f"127.0.0.1:{socks_port}",
            openssh.host_alias_key_plain,
        ),
        env=_ssh_env(str(openssh.client_home)),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", socks_port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.2)
        else:
            pytest.fail(f"SOCKS port not listening: {fwd.stderr.read() if fwd.poll() else ''}")
        # Port accepting connections is enough for fixture proof.
        assert fwd.poll() is None
    finally:
        fwd.terminate()
        try:
            fwd.wait(timeout=5)
        except subprocess.TimeoutExpired:
            fwd.kill()


def test_remote_forward(openssh):
    openssh.write_client_config("key_plain")
    # Local tiny HTTP for the remote forward target.
    local = socket.socket()
    local.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    local.bind(("127.0.0.1", 0))
    local.listen(1)
    local_port = local.getsockname()[1]
    remote_listen = 19090

    def _serve_once():
        local.settimeout(20)
        try:
            conn, _ = local.accept()
            conn.recv(64)
            conn.sendall(b"HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\nOK")
            conn.close()
        except OSError:
            pass

    import threading

    threading.Thread(target=_serve_once, daemon=True).start()
    fwd = subprocess.Popen(
        (
            "ssh",
            "-F",
            str(openssh.ssh_config),
            "-N",
            "-R",
            f"127.0.0.1:{remote_listen}:127.0.0.1:{local_port}",
            openssh.host_alias_key_plain,
        ),
        env=_ssh_env(str(openssh.client_home)),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        time.sleep(1.5)
        probe = openssh.exec_in_container(
            "wget",
            "-qO-",
            f"http://127.0.0.1:{remote_listen}/",
        )
        # busybox wget may be absent naming — try nc fallback
        if probe.returncode != 0:
            probe = openssh.exec_in_container(
                "sh",
                "-c",
                f"printf 'GET / HTTP/1.0\\r\\n\\r\\n' | nc 127.0.0.1 {remote_listen}",
            )
        assert probe.returncode == 0 or "OK" in (probe.stdout or ""), probe.stderr
        assert "OK" in (probe.stdout or "")
    finally:
        fwd.terminate()
        try:
            fwd.wait(timeout=5)
        except subprocess.TimeoutExpired:
            fwd.kill()
        local.close()


def test_destroy_removes_container(openssh):
    from tests.fixtures.temporary_openssh import _container_env

    cid = openssh.container_id
    name = openssh.container_name
    runtime = openssh.runtime
    openssh.destroy()
    for target in (cid, name):
        probe = subprocess.run(
            (runtime, "inspect", target),
            capture_output=True,
            text=True,
            check=False,
            env=_container_env(),
        )
        assert probe.returncode != 0, target


def test_cleanup_orphaned_removes_detached_fixture(tmp_path):
    """Handoff-style start (auto_cleanup=False) must still be sweepable."""
    from tests.fixtures.temporary_openssh import (
        _container_env,
        cleanup_orphaned_temporary_openssh,
        start_temporary_openssh,
    )

    if not (shutil.which("podman") or shutil.which("docker")):
        pytest.skip("container runtime unavailable")
    env = start_temporary_openssh(tmp_path, auto_cleanup=False)
    name = env.container_name
    # Simulate starter process dropping the object without destroy.
    env.detach()
    removed = cleanup_orphaned_temporary_openssh()
    assert name in removed
    probe = subprocess.run(
        (env.runtime, "inspect", name),
        capture_output=True,
        text=True,
        check=False,
        env=_container_env(),
    )
    assert probe.returncode != 0


def test_destroy_from_meta_removes_container(tmp_path):
    from tests.fixtures.temporary_openssh import (
        _container_env,
        destroy_temporary_openssh_meta,
        start_temporary_openssh,
    )

    if not (shutil.which("podman") or shutil.which("docker")):
        pytest.skip("container runtime unavailable")
    env = start_temporary_openssh(tmp_path, auto_cleanup=False)
    meta = env.to_json()
    name = env.container_name
    env.detach()
    destroy_temporary_openssh_meta(meta)
    probe = subprocess.run(
        (meta["runtime"], "inspect", name),
        capture_output=True,
        text=True,
        check=False,
        env=_container_env(),
    )
    assert probe.returncode != 0
