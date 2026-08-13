"""Phase 13 temporary OpenSSH fixture (password, keys, SFTP, forwarding).

Isolated from the developer's ``~/.ssh``. Uses podman/docker Alpine sshd on a
localhost-only port. Host keys live inside the container; client material is
written only under ``tmp_path``.

Cleanup is fixture-owned: ``destroy()``, optional process-exit auto-cleanup,
and ``cleanup_orphaned_temporary_openssh()`` for pytest session / handoff
scripts. Production sshPilot daemon behavior is never involved.
"""
from __future__ import annotations

import atexit
import logging
import os
import pwd
import secrets
import shutil
import socket
import subprocess
import time
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal, Optional

from tests.daemon.password_sshd import container_runtime

AuthMode = Literal["password", "key", "key_plain"]

logger = logging.getLogger(__name__)

# Stable name prefix for orphan sweeps (must stay fixture-only).
CONTAINER_NAME_PREFIX = "sshpilot-p13-"

_LIVE: "weakref.WeakSet[TemporaryOpenSSH]" = weakref.WeakSet()
_ATEXIT_REGISTERED = False


def _login_home() -> Path:
    """Return the account home, ignoring an isolated test ``HOME`` override."""
    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except Exception:
        return Path.home()


def _container_env() -> dict[str, str]:
    """Env for podman/docker so storage/subuids stay on the login account.

    Phase 14 harnesses isolate ``HOME``/``XDG_*``. If podman inherits that, it
    creates a fresh storage root that often cannot unpack images (insufficient
    UIDs/GIDs). Keep container engine state on the real login home.
    """
    env = os.environ.copy()
    home = _login_home()
    env["HOME"] = str(home)
    env["XDG_DATA_HOME"] = str(home / ".local" / "share")
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["XDG_RUNTIME_DIR"] = str(
        Path(os.environ.get("XDG_RUNTIME_DIR_HOST")
             or f"/run/user/{os.getuid()}")
    )
    return env


def _free_port() -> int:
    listener = socket.socket()
    try:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])
    finally:
        listener.close()


def _run_container(
    runtime: str,
    args: Iterable[str],
    *,
    check: bool = False,
    timeout: Optional[float] = 60.0,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        (runtime, *args),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
        env=_container_env(),
    )


def _ensure_atexit() -> None:
    global _ATEXIT_REGISTERED
    if _ATEXIT_REGISTERED:
        return
    atexit.register(_atexit_destroy_live)
    _ATEXIT_REGISTERED = True


def _atexit_destroy_live() -> None:
    for env in list(_LIVE):
        try:
            env.destroy()
        except Exception:
            logger.debug("atexit TemporaryOpenSSH.destroy failed", exc_info=True)


def list_temporary_openssh_containers(runtime: Optional[str] = None) -> list[str]:
    """Return names of live fixture containers (``sshpilot-p13-*``)."""
    rt = runtime or container_runtime()
    if rt is None:
        return []
    listed = _run_container(
        rt,
        ("ps", "-a", "--filter", f"name={CONTAINER_NAME_PREFIX}", "--format", "{{.Names}}"),
        timeout=30.0,
    )
    if listed.returncode != 0:
        logger.warning(
            "failed to list temporary OpenSSH containers: %s",
            (listed.stderr or listed.stdout or "").strip(),
        )
        return []
    names: list[str] = []
    for line in (listed.stdout or "").splitlines():
        name = line.strip()
        if name.startswith(CONTAINER_NAME_PREFIX):
            names.append(name)
    return names


def cleanup_orphaned_temporary_openssh(
    *,
    runtime: Optional[str] = None,
    keep_names: Optional[Iterable[str]] = None,
) -> list[str]:
    """Force-remove leftover ``sshpilot-p13-*`` containers (and their conmon).

    Safe for pytest session start/finish and Flatpak handoff scripts. Never
    touches the production sshPilot daemon socket or processes.
    """
    rt = runtime or container_runtime()
    if rt is None:
        return []
    keep = {n for n in (keep_names or ()) if n}
    removed: list[str] = []
    for name in list_temporary_openssh_containers(rt):
        if name in keep:
            continue
        result = _run_container(rt, ("rm", "-f", name), timeout=60.0)
        if result.returncode == 0:
            removed.append(name)
            continue
        # Retry via stop then rm — rootless podman occasionally needs both.
        _run_container(rt, ("stop", "-t", "1", name), timeout=30.0)
        retry = _run_container(rt, ("rm", "-f", name), timeout=60.0)
        if retry.returncode == 0:
            removed.append(name)
        else:
            detail = (retry.stderr or retry.stdout or result.stderr or "").strip()
            logger.warning("failed to remove temporary OpenSSH %s: %s", name, detail)
    return removed


def destroy_temporary_openssh_meta(meta: dict) -> None:
    """Destroy a fixture described by ``to_json()`` / handoff metadata."""
    runtime = str(meta.get("runtime") or "") or container_runtime()
    if not runtime:
        return
    targets: list[str] = []
    for key in ("container_id", "container_name"):
        value = str(meta.get(key) or "").strip()
        if value and value not in targets:
            targets.append(value)
    for target in targets:
        result = _run_container(str(runtime), ("rm", "-f", target), timeout=60.0)
        if result.returncode == 0:
            continue
        _run_container(str(runtime), ("stop", "-t", "1", target), timeout=30.0)
        _run_container(str(runtime), ("rm", "-f", target), timeout=60.0)


@dataclass(eq=False)
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
    container_name: str = ""
    _destroyed: bool = field(default=False, repr=False)
    _auto_cleanup: bool = field(default=True, repr=False)
    _finalizer: Any = field(default=None, repr=False, compare=False)

    def _register_auto_cleanup(self) -> None:
        if not self._auto_cleanup:
            return
        _LIVE.add(self)
        _ensure_atexit()
        # GC backup when callers drop the object without destroy() (normal exit).
        self._finalizer = weakref.finalize(self, _finalize_destroy, self.runtime, self.container_id, self.container_name)

    def detach(self) -> "TemporaryOpenSSH":
        """Keep the container after this process exits.

        Use for handoff scripts that write metadata and exit while a later
        step (Flatpak E2E, manual ENTER trap) is responsible for destroy.
        """
        self._auto_cleanup = False
        _LIVE.discard(self)
        finalizer = self._finalizer
        self._finalizer = None
        if finalizer is not None:
            finalizer.detach()
        return self

    def destroy(self) -> None:
        if self._destroyed:
            return

        targets: list[str] = []
        if self.container_id:
            targets.append(self.container_id)
        if self.container_name and self.container_name not in targets:
            targets.append(self.container_name)

        last_error = ""
        for target in targets:
            for attempt in range(3):
                result = _run_container(self.runtime, ("rm", "-f", target), timeout=60.0)
                if result.returncode == 0 or not self._container_exists(target):
                    break
                last_error = (result.stderr or result.stdout or "").strip()
                if attempt < 2:
                    _run_container(self.runtime, ("stop", "-t", "1", target), timeout=30.0)
                    time.sleep(0.2)

        still = [target for target in targets if self._container_exists(target)]
        if still:
            logger.warning(
                "TemporaryOpenSSH.destroy left containers behind: %s (%s)",
                still,
                last_error or "no stderr",
            )
            # Keep auto-cleanup registration so atexit / session sweep can retry.
            if self._auto_cleanup:
                _LIVE.add(self)
            return

        self._destroyed = True
        _LIVE.discard(self)
        finalizer = self._finalizer
        self._finalizer = None
        if finalizer is not None:
            finalizer.detach()

    def _container_exists(self, target: str) -> bool:
        probe = _run_container(self.runtime, ("inspect", target), timeout=15.0)
        return probe.returncode == 0

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
        return _run_container(
            self.runtime,
            ("exec", self.container_id, *command),
            timeout=timeout,
        )

    def to_json(self) -> dict:
        return {
            "runtime": self.runtime,
            "container_id": self.container_id,
            "container_name": self.container_name,
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


def _finalize_destroy(runtime: str, container_id: str, container_name: str) -> None:
    """weakref backup — best-effort rm when the object is collected."""
    targets = [t for t in (container_id, container_name) if t]
    for target in targets:
        try:
            _run_container(runtime, ("rm", "-f", target), timeout=30.0)
        except Exception:
            pass


def start_temporary_openssh(
    tmp_path: Path,
    *,
    auto_cleanup: bool = True,
) -> TemporaryOpenSSH:
    """Start disposable Alpine sshd for Phase 13 acceptance testing.

    ``auto_cleanup=True`` (default) registers atexit + weakref destroy so
    pytest/harness leaks are cleaned when the process exits. Handoff scripts
    that write metadata and exit while keeping the container must pass
    ``auto_cleanup=False`` (or call :meth:`TemporaryOpenSSH.detach`) and destroy
    later via metadata / ``cleanup_orphaned_temporary_openssh``.
    """
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
    container_name = f"{CONTAINER_NAME_PREFIX}{secrets.token_hex(4)}"

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
    create = _run_container(
        runtime,
        (
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
        timeout=120.0,
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
        container_name=container_name,
        _auto_cleanup=auto_cleanup,
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
            env._register_auto_cleanup()
            return env
        logs = _run_container(runtime, ("logs", container_id), timeout=30.0)
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
