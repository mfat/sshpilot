"""Disposable OpenSSH fixture for Phase 10.1 real-daemon integration tests.

Extends the Phase 9 password-only container with:

* password **and** encrypted-key authentication
* SFTP subsystem
* TCP forwarding (local / remote / dynamic)
* ``GatewayPorts clientspecified`` so remote forwards may name a bind address
* ``PermitListen any`` (Alpine OpenSSH rejects ``host:low-high`` ranges)
* isolated client ``known_hosts`` + ``ssh_config`` **without** ``RequestTTY``
  (``RequestTTY force`` breaks ``ssh -s sftp`` and ``ssh -N``)

Host keys are generated inside the container; the client never mounts developer
SSH material. Cleanup is deterministic via ``destroy()``.
"""

from __future__ import annotations

import os
import secrets
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from tests.daemon.password_sshd import container_runtime

AuthMode = Literal["password", "key"]


def _free_port() -> int:
    listener = socket.socket()
    try:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]
    finally:
        listener.close()


@dataclass
class Phase10SshdEnvironment:
    """Isolated OpenSSH reached only on localhost for Phase 10.1."""

    runtime: str
    container_id: str
    port: int
    username: str
    password: str
    key_path: Path
    key_passphrase: str
    known_hosts: Path
    ssh_config: Path
    host_alias: str
    client_home: Path
    # TCP echo/HTTP probe inside the container (for local/dynamic forwards).
    remote_echo_port: int = 18080

    def destroy(self) -> None:
        subprocess.run(
            (self.runtime, "rm", "-f", self.container_id),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

    def password_host_lines(self) -> str:
        """Host block for password auth — no RequestTTY."""

        return "\n".join(
            (
                f"Host {self.host_alias}",
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

    def key_host_lines(self) -> str:
        """Host block for encrypted-key auth — no RequestTTY."""

        return "\n".join(
            (
                f"Host {self.host_alias}",
                "    HostName 127.0.0.1",
                f"    Port {self.port}",
                f"    User {self.username}",
                "    PreferredAuthentications publickey",
                "    PasswordAuthentication no",
                "    PubkeyAuthentication yes",
                "    IdentitiesOnly yes",
                f"    IdentityFile {self.key_path}",
                f"    UserKnownHostsFile {self.known_hosts}",
                "    GlobalKnownHostsFile /dev/null",
                "    StrictHostKeyChecking yes",
                "    LogLevel ERROR",
            )
        )

    def write_client_config(self, auth: AuthMode = "password") -> Path:
        """Rewrite ``ssh_config`` for password or key auth (never RequestTTY)."""

        body = self.password_host_lines() if auth == "password" else self.key_host_lines()
        self.ssh_config.write_text(body + "\n")
        self.ssh_config.chmod(0o600)
        return self.ssh_config

    def exec_in_container(self, *command: str, timeout: float = 10.0) -> subprocess.CompletedProcess:
        """Run a command inside the fixture container (remote-forward probes)."""

        return subprocess.run(
            (self.runtime, "exec", self.container_id, *command),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )


def start_phase10_sshd(tmp_path: Path) -> Phase10SshdEnvironment:
    """Start a disposable Alpine sshd with password + encrypted-key auth."""

    runtime = container_runtime()
    if runtime is None:
        raise RuntimeError("no container runtime")

    tools = {
        name: shutil.which(name)
        for name in ("ssh", "ssh-keygen", "ssh-keyscan")
    }
    if not all(tools.values()):
        raise RuntimeError("OpenSSH client tools unavailable")

    port = _free_port()
    username = "phase10"
    password = f"phase10-{secrets.token_hex(8)}"
    key_passphrase = f"key-{secrets.token_hex(8)}"
    remote_echo_port = 18080

    client_home = tmp_path / "client-home"
    client_home.mkdir()
    ssh_dir = client_home / ".ssh"
    ssh_dir.mkdir(mode=0o700)
    known_hosts = client_home / "known_hosts"
    ssh_config = client_home / "config"
    key_path = ssh_dir / "id_ed25519"
    host_alias = "phase10-sshd"
    container_name = f"sshpilot-p10-{secrets.token_hex(4)}"

    keygen = subprocess.run(
        (
            tools["ssh-keygen"],
            "-t",
            "ed25519",
            "-N",
            key_passphrase,
            "-f",
            str(key_path),
            "-C",
            "phase10-integration",
            "-q",
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    if keygen.returncode != 0:
        raise RuntimeError(f"ssh-keygen failed: {(keygen.stderr or keygen.stdout).strip()}")
    key_path.chmod(0o600)
    pubkey = (ssh_dir / "id_ed25519.pub").read_text().strip()

    # GatewayPorts clientspecified: remote forwards may request a bind address
    # (default is loopback-only). PermitListen any keeps remote-forward tests
    # working on Alpine OpenSSH, which rejects host:port-range syntax.
    boot = f"""
set -eu
apk add --no-cache openssh openssh-server busybox-extras >/dev/null
adduser -D -s /bin/sh {username}
echo '{username}:{password}' | chpasswd
mkdir -p /home/{username}/.ssh
chmod 700 /home/{username}/.ssh
printf '%s\\n' '{pubkey}' > /home/{username}/.ssh/authorized_keys
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
# Isolated container: allow any listen port; tests only request high ports.
PermitListen any
PermitOpen any
PrintMotd no
Subsystem sftp /usr/lib/ssh/sftp-server
EOF
# Tiny TCP echo for local/dynamic forward smoke tests (high port, non-root).
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
    env = Phase10SshdEnvironment(
        runtime=runtime,
        container_id=container_id,
        port=port,
        username=username,
        password=password,
        key_path=key_path,
        key_passphrase=key_passphrase,
        known_hosts=known_hosts,
        ssh_config=ssh_config,
        host_alias=host_alias,
        client_home=client_home,
        remote_echo_port=remote_echo_port,
    )
    try:
        deadline = time.monotonic() + 90.0
        scan_output = ""
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
            scan = subprocess.run(
                (
                    tools["ssh-keyscan"],
                    "-p",
                    str(port),
                    "-T",
                    "3",
                    "127.0.0.1",
                ),
                check=False,
                capture_output=True,
                text=True,
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "HOME": str(client_home),
                },
            )
            if scan.returncode == 0 and scan.stdout.strip():
                scan_output = scan.stdout
                break
            time.sleep(0.4)
        else:
            logs = subprocess.run(
                (runtime, "logs", container_id),
                check=False,
                capture_output=True,
                text=True,
            )
            detail = ((logs.stderr or "") + (logs.stdout or "")).strip()[-800:]
            raise RuntimeError(
                f"sshd never became ready for key exchange: {detail or 'no logs'}"
            )

        known_hosts.write_text(scan_output)
        known_hosts.chmod(0o600)
        env.write_client_config("password")
        return env
    except Exception:
        env.destroy()
        raise
