"""Disposable containerized OpenSSH password-auth fixture for Phase 9."""

from __future__ import annotations

import os
import secrets
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


def _free_port() -> int:
    listener = socket.socket()
    try:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]
    finally:
        listener.close()


def container_runtime() -> Optional[str]:
    """Return an available local container runtime, or None."""

    for name in ("podman", "docker"):
        path = shutil.which(name)
        if path is None:
            continue
        try:
            probe = subprocess.run(
                (path, "info"),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
            )
        except subprocess.TimeoutExpired:
            continue
        if probe.returncode == 0:
            return path
    return None


@dataclass
class PasswordSshdEnvironment:
    """Isolated password-enabled sshd reached only on localhost."""

    runtime: str
    container_id: str
    port: int
    username: str
    password: str
    host_key_dir: Path
    client_home: Path
    known_hosts: Path
    ssh_config: Path
    host_alias: str = "phase9-password"

    def destroy(self) -> None:
        subprocess.run(
            (self.runtime, "rm", "-f", self.container_id),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


def start_password_sshd(tmp_path: Path) -> PasswordSshdEnvironment:
    """Start a disposable Alpine sshd with password authentication only."""

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
    username = "pwuser"
    password = f"phase9-{secrets.token_hex(8)}"
    host_key_dir = tmp_path / "host-keys"
    host_key_dir.mkdir()
    client_home = tmp_path / "client-home"
    client_home.mkdir()
    known_hosts = client_home / "known_hosts"
    ssh_config = client_home / "config"
    container_name = f"sshpilot-pw-{secrets.token_hex(4)}"

    # Write a minimal sshd config into the image command so the host never
    # mounts developer SSH material into the container.
    boot = f"""
set -eu
apk add --no-cache openssh openssh-server >/dev/null
adduser -D -s /bin/sh {username}
echo '{username}:{password}' | chpasswd
ssh-keygen -A >/dev/null
cat > /etc/ssh/sshd_config <<'EOF'
Port 22
ListenAddress 0.0.0.0
PermitRootLogin no
PasswordAuthentication yes
KbdInteractiveAuthentication no
PubkeyAuthentication no
AllowUsers {username}
PrintMotd no
Subsystem sftp /usr/lib/ssh/sftp-server
EOF
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
    env = PasswordSshdEnvironment(
        runtime=runtime,
        container_id=container_id,
        port=port,
        username=username,
        password=password,
        host_key_dir=host_key_dir,
        client_home=client_home,
        known_hosts=known_hosts,
        ssh_config=ssh_config,
    )
    try:
        deadline = time.monotonic() + 45.0
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
            # Wait until sshd speaks the SSH protocol, not merely until the
            # published port is open (container boot can still be installing).
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
            raise RuntimeError("sshd never became ready for key exchange")

        known_hosts.write_text(scan_output)
        known_hosts.chmod(0o600)
        ssh_config.write_text(
            "\n".join(
                (
                    f"Host {env.host_alias}",
                    "    HostName 127.0.0.1",
                    f"    Port {port}",
                    f"    User {username}",
                    "    PreferredAuthentications password",
                    "    PubkeyAuthentication no",
                    "    NumberOfPasswordPrompts 3",
                    f"    UserKnownHostsFile {known_hosts}",
                    "    GlobalKnownHostsFile /dev/null",
                    "    StrictHostKeyChecking yes",
                    "    IdentitiesOnly yes",
                    "    IdentityFile /dev/null",
                    "    RequestTTY force",
                    "    LogLevel ERROR",
                )
            )
            + "\n"
        )
        ssh_config.chmod(0o600)
        return env
    except Exception:
        env.destroy()
        raise
