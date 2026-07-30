"""Phase 13 temporary OpenSSH fixture (password, keys, SFTP, forwarding).

Isolated from the developer's ``~/.ssh``. Uses podman/docker Alpine sshd on a
localhost-only port. Host keys live inside the container; client material is
written only under ``tmp_path``.
"""
from __future__ import annotations

import os
import secrets
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from tests.daemon.password_sshd import container_runtime

AuthMode = Literal["password", "key", "key_plain"]


def _free_port() -> int:
    listener = socket.socket()
    try:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])
    finally:
        listener.close()


@dataclass
class TemporaryOpenSSH:
    """Controlled temporary OpenSSH for Phase 13 acceptance / GUI smoke."""

    runtime: str
    container_id: str
    port: int
    username: str
    password: str
    encrypted_key_path: Path
    encrypted_key_passphrase: str
    plain_key_path: Path
    known_hosts: Path
    ssh_config: Path
    client_home: Path
    host_alias_password: str = "phase13-password"
    host_alias_key: str = "phase13-key"
    host_alias_key_plain: str = "phase13-key-plain"
    remote_echo_port: int = 18080
    _destroyed: bool = field(default=False, repr=False)

    def destroy(self) -> None:
        if self._destroyed:
            return
        self._destroyed = True
        subprocess.run(
            (self.runtime, "rm", "-f", self.container_id),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

    def clear_known_hosts(self) -> None:
        """Empty known_hosts for first-use / host-key confirmation tests."""
        self.known_hosts.write_text("")
        self.known_hosts.chmod(0o600)

    def populate_known_hosts(self) -> None:
        tools = shutil.which("ssh-keyscan")
        if not tools:
            raise RuntimeError("ssh-keyscan unavailable")
        scan = subprocess.run(
            (tools, "-p", str(self.port), "-T", "5", "127.0.0.1"),
            check=False,
            capture_output=True,
            text=True,
            env={"PATH": os.environ.get("PATH", ""), "HOME": str(self.client_home)},
        )
        if scan.returncode != 0 or not scan.stdout.strip():
            raise RuntimeError(f"ssh-keyscan failed: {(scan.stderr or '').strip()}")
        self.known_hosts.write_text(scan.stdout)
        self.known_hosts.chmod(0o600)

    def write_client_config(self, auth: AuthMode = "password") -> Path:
        if auth == "password":
            body = "\n".join(
                (
                    f"Host {self.host_alias_password}",
                    "    HostName 127.0.0.1",
                    f"    Port {self.port}",
                    f"    User {self.username}",
                    "    PreferredAuthentications password",
                    "    PubkeyAuthentication no",
                    "    NumberOfPasswordPrompts 1",
                    f"    UserKnownHostsFile {self.known_hosts}",
                    "    GlobalKnownHostsFile /dev/null",
                    "    StrictHostKeyChecking yes",
                    "    IdentitiesOnly yes",
                    "    IdentityFile /dev/null",
                    "    LogLevel ERROR",
                )
            )
        elif auth == "key":
            body = "\n".join(
                (
                    f"Host {self.host_alias_key}",
                    "    HostName 127.0.0.1",
                    f"    Port {self.port}",
                    f"    User {self.username}",
                    "    PreferredAuthentications publickey",
                    "    PasswordAuthentication no",
                    "    PubkeyAuthentication yes",
                    "    IdentitiesOnly yes",
                    f"    IdentityFile {self.encrypted_key_path}",
                    f"    UserKnownHostsFile {self.known_hosts}",
                    "    GlobalKnownHostsFile /dev/null",
                    "    StrictHostKeyChecking yes",
                    "    LogLevel ERROR",
                )
            )
        else:
            body = "\n".join(
                (
                    f"Host {self.host_alias_key_plain}",
                    "    HostName 127.0.0.1",
                    f"    Port {self.port}",
                    f"    User {self.username}",
                    "    PreferredAuthentications publickey",
                    "    PasswordAuthentication no",
                    "    PubkeyAuthentication yes",
                    "    IdentitiesOnly yes",
                    f"    IdentityFile {self.plain_key_path}",
                    f"    UserKnownHostsFile {self.known_hosts}",
                    "    GlobalKnownHostsFile /dev/null",
                    "    StrictHostKeyChecking yes",
                    "    LogLevel ERROR",
                )
            )
        self.ssh_config.write_text(body + "\n")
        self.ssh_config.chmod(0o600)
        return self.ssh_config

    def write_all_host_blocks(self) -> Path:
        """Write password + encrypted-key + plain-key Host blocks together."""
        parts = []
        for auth in ("password", "key", "key_plain"):
            self.write_client_config(auth)  # type: ignore[arg-type]
            parts.append(self.ssh_config.read_text())
        self.ssh_config.write_text("\n".join(parts))
        self.ssh_config.chmod(0o600)
        return self.ssh_config

    def exec_in_container(self, *command: str, timeout: float = 15.0) -> subprocess.CompletedProcess:
        return subprocess.run(
            (self.runtime, "exec", self.container_id, *command),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def to_json(self) -> dict:
        return {
            "runtime": self.runtime,
            "container_id": self.container_id,
            "port": self.port,
            "username": self.username,
            "password": self.password,
            "encrypted_key_path": str(self.encrypted_key_path),
            "encrypted_key_passphrase": self.encrypted_key_passphrase,
            "plain_key_path": str(self.plain_key_path),
            "known_hosts": str(self.known_hosts),
            "ssh_config": str(self.ssh_config),
            "client_home": str(self.client_home),
            "host_alias_password": self.host_alias_password,
            "host_alias_key": self.host_alias_key,
            "host_alias_key_plain": self.host_alias_key_plain,
            "remote_echo_port": self.remote_echo_port,
        }


def start_temporary_openssh(tmp_path: Path) -> TemporaryOpenSSH:
    """Start disposable Alpine sshd for Phase 13 acceptance testing."""
    runtime = container_runtime()
    if runtime is None:
        raise RuntimeError("no container runtime (podman/docker)")

    tools = {name: shutil.which(name) for name in ("ssh", "ssh-keygen", "ssh-keyscan")}
    if not all(tools.values()):
        raise RuntimeError("OpenSSH client tools unavailable")

    port = _free_port()
    username = "phase13"
    password = f"phase13-{secrets.token_hex(8)}"
    key_passphrase = f"key-{secrets.token_hex(8)}"
    remote_echo_port = 18080

    client_home = tmp_path / "client-home"
    client_home.mkdir()
    ssh_dir = client_home / ".ssh"
    ssh_dir.mkdir(mode=0o700)
    known_hosts = client_home / "known_hosts"
    ssh_config = client_home / "config"
    encrypted_key = ssh_dir / "id_ed25519_enc"
    plain_key = ssh_dir / "id_ed25519_plain"
    container_name = f"sshpilot-p13-{secrets.token_hex(4)}"

    for path, passphrase in ((encrypted_key, key_passphrase), (plain_key, "")):
        keygen = subprocess.run(
            (
                tools["ssh-keygen"],
                "-t",
                "ed25519",
                "-N",
                passphrase,
                "-f",
                str(path),
                "-C",
                "phase13-fixture",
                "-q",
            ),
            check=False,
            capture_output=True,
            text=True,
        )
        if keygen.returncode != 0:
            raise RuntimeError(f"ssh-keygen failed: {(keygen.stderr or keygen.stdout).strip()}")
        path.chmod(0o600)

    enc_pub = (ssh_dir / "id_ed25519_enc.pub").read_text().strip()
    plain_pub = (ssh_dir / "id_ed25519_plain.pub").read_text().strip()

    boot = f"""
set -eu
apk add --no-cache openssh openssh-server busybox-extras >/dev/null
adduser -D -s /bin/sh {username}
echo '{username}:{password}' | chpasswd
mkdir -p /home/{username}/.ssh
chmod 700 /home/{username}/.ssh
printf '%s\\n%s\\n' '{enc_pub}' '{plain_pub}' > /home/{username}/.ssh/authorized_keys
chmod 600 /home/{username}/.ssh/authorized_keys
chown -R {username}:{username} /home/{username}/.ssh
ssh-keygen -A >/dev/null
cat > /etc/ssh/sshd_config <<'EOF'
Port 22
ListenAddress 0.0.0.0
PermitRootLogin no
PasswordAuthentication yes
KbdInteractiveAuthentication no
PubkeyAuthentication yes
AuthorizedKeysFile .ssh/authorized_keys
AllowUsers {username}
AllowTcpForwarding yes
GatewayPorts clientspecified
PermitListen any
PermitOpen any
PrintMotd no
Subsystem sftp /usr/lib/ssh/sftp-server
EOF
(
  while true; do
    printf 'HTTP/1.0 200 OK\\r\\nContent-Length: 2\\r\\n\\r\\nOK' | nc -l -p {remote_echo_port} -s 127.0.0.1 || true
  done
) >/dev/null 2>&1 &
/usr/sbin/sshd -t
exec /usr/sbin/sshd -D -e
"""
    create = subprocess.run(
        (
            runtime,
            "run",
            "--rm",
            "-d",
            "--name",
            container_name,
            "-p",
            f"127.0.0.1:{port}:22",
            "docker.io/library/alpine:3.20",
            "sh",
            "-c",
            boot,
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    if create.returncode != 0:
        detail = (create.stderr or create.stdout or "").strip()
        raise RuntimeError(f"container start failed: {detail or 'unknown'}")
    container_id = create.stdout.strip() or container_name
    env = TemporaryOpenSSH(
        runtime=runtime,
        container_id=container_id,
        port=port,
        username=username,
        password=password,
        encrypted_key_path=encrypted_key,
        encrypted_key_passphrase=key_passphrase,
        plain_key_path=plain_key,
        known_hosts=known_hosts,
        ssh_config=ssh_config,
        client_home=client_home,
        remote_echo_port=remote_echo_port,
    )
    try:
        deadline = time.monotonic() + 90.0
        while time.monotonic() < deadline:
            probe = socket.socket()
            try:
                probe.settimeout(0.25)
                probe.connect(("127.0.0.1", port))
            except OSError:
                time.sleep(0.2)
                continue
            finally:
                probe.close()
            try:
                env.populate_known_hosts()
            except RuntimeError:
                time.sleep(0.4)
                continue
            env.write_all_host_blocks()
            return env
        logs = subprocess.run(
            (runtime, "logs", container_id),
            check=False,
            capture_output=True,
            text=True,
        )
        detail = ((logs.stderr or "") + (logs.stdout or "")).strip()[-800:]
        raise RuntimeError(f"sshd never became ready: {detail or 'no logs'}")
    except Exception:
        env.destroy()
        raise


def require_temporary_openssh(tmp_path: Path) -> TemporaryOpenSSH:
    """pytest helper — skip when container runtime is unavailable."""
    import pytest

    if container_runtime() is None:
        pytest.skip("Phase 13 temporary OpenSSH requires podman or docker")
    try:
        return start_temporary_openssh(tmp_path)
    except RuntimeError as exc:
        pytest.skip(f"temporary OpenSSH unavailable: {exc}")
