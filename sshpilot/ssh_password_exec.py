# ssh_password_exec.py
import os, tempfile, threading, subprocess, shutil
import re
import logging
from typing import Callable, Iterable, List, Optional, Tuple

from .platform_utils import get_sshpass_path

# Moved from scp_utils.py to avoid circular import
_REMOTE_SPEC_RE = re.compile(r"^[^@]+@(?:\[[^\]]+\]|[^:]+):.+$")


def _strip_brackets(value: str) -> str:
    if value.startswith('[') and value.endswith(']'):
        return value[1:-1]
    return value


def _extract_host(target: str) -> str:
    if '@' in target:
        host = target.split('@', 1)[1]
    else:
        host = target
    return _strip_brackets(host)


def _normalize_remote_sources(target: str, sources: Iterable[str]) -> List[str]:
    host = _extract_host(target)
    host_variants = [host] if host else []
    if host and ':' in host:
        bracketed = f"[{host}]"
        if bracketed not in host_variants:
            host_variants.append(bracketed)
    normalized: List[str] = []
    for item in sources:
        path = (item or '').strip()
        if not path:
            continue
        if path.startswith(f"{target}:"):
            normalized.append(path)
            continue
        if host_variants:
            matched_host_variant = False
            for host_variant in host_variants:
                if path.startswith(f"{host_variant}:"):
                    normalized.append(path)
                    matched_host_variant = True
                    break
            if matched_host_variant:
                continue
        if _REMOTE_SPEC_RE.match(path):
            normalized.append(path)
            continue
        normalized.append(f"{target}:{path}")
    return normalized


def assemble_scp_transfer_args(
    target: str,
    sources: Iterable[str],
    destination: str,
    direction: str,
) -> Tuple[List[str], str]:
    """Return normalized scp sources and destination arguments for a transfer.

    Parameters
    ----------
    target:
        The ``user@host`` string for the connection (user may be omitted).
    sources:
        Iterable of source paths supplied by the caller.
    destination:
        Destination path (remote directory for uploads or local path for downloads).
    direction:
        Either ``"upload"`` or ``"download"``.
    """
    direction_value = (direction or '').lower()
    if direction_value not in {'upload', 'download'}:
        raise ValueError(f"Unsupported scp direction: {direction}")

    if direction_value == 'upload':
        cleaned_sources = [s for s in sources if s]
        return cleaned_sources, f"{target}:{destination}"

    remote_sources = _normalize_remote_sources(target, sources)
    return remote_sources, destination

def _write_once_fifo(path: str, secret: str):
    # Writer runs in a thread: blocks until sshpass opens FIFO for read
    with open(path, "w", encoding="utf-8") as w:
        w.write(secret)
        w.flush()

def _mk_priv_dir(prefix="sshpilot-pass-") -> str:
    d = tempfile.mkdtemp(prefix=prefix)
    os.chmod(d, 0o700)
    return d


def wrap_argv_with_sshpass(
    argv: List[str],
    password: str,
    *,
    env: Optional[dict] = None,
) -> Tuple[List[str], Callable[[], None]]:
    """Wrap ``argv`` so a stored password is fed to ssh/scp/ssh-copy-id via a
    write-once FIFO (``sshpass -f <fifo>``), keeping the secret off the command
    line and out of the environment.

    Shared by the terminal, SCP, and ssh-copy-id spawners so the FIFO dance lives
    in one place. Behaviour:

    * Creates a private 0700 temp dir + FIFO and starts a daemon writer that
      writes the password exactly once when sshpass opens the FIFO for reading.
    * Returns ``([sshpass, '-f', fifo, *argv], cleanup)``. ``cleanup()`` removes
      the temp dir; the caller decides when to call it (e.g. ``atexit.register``
      or on widget teardown).
    * If ``env`` is given it is mutated in place: ``SSH_ASKPASS`` is dropped and
      ``SSH_ASKPASS_REQUIRE=never`` is set so OpenSSH never diverts the password
      prompt to askpass.
    * If sshpass is unavailable, returns ``(argv, no-op)`` unchanged but still
      mutates ``env`` (REQUIRE=never) so the caller falls back to an interactive
      prompt rather than a stuck askpass.
    """
    if env is not None:
        env.pop("SSH_ASKPASS", None)
        env["SSH_ASKPASS_REQUIRE"] = "never"

    sshpass = get_sshpass_path()
    if not sshpass:
        logging.getLogger(__name__).warning(
            "sshpass unavailable; falling back to interactive password prompt"
        )
        return list(argv), (lambda: None)

    tmpdir = _mk_priv_dir()
    fifo = os.path.join(tmpdir, "pw.fifo")
    os.mkfifo(fifo, 0o600)
    threading.Thread(target=_write_once_fifo, args=(fifo, password), daemon=True).start()

    def _cleanup() -> None:
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass

    return [sshpass, "-f", fifo, *argv], _cleanup

def run_ssh_with_password(host: str, user: str, password: str, *,
                          port: int = 22,
                          argv_tail: list[str] | None = None,
                          known_hosts_path: str | None = None,
                          extra_ssh_opts: list[str] | None = None,
                          inherit_env: dict | None = None,
                          use_publickey: bool = False) -> subprocess.CompletedProcess:
    """Launch `ssh` using sshpass -f <FIFO> safely.
    - No password in argv/env.
    - FIFO lives in private temp dir, removed afterwards.
    - When ``use_publickey`` is True, allow publickey+password authentication.
    """
    tmpdir = _mk_priv_dir()
    fifo = os.path.join(tmpdir, "pw.fifo")
    os.mkfifo(fifo, 0o600)

    # Start writer thread that writes the password exactly once
    t = threading.Thread(target=_write_once_fifo, args=(fifo, password), daemon=True)
    t.start()

    ssh_opts = []
    if use_publickey:
        ssh_opts += [
            "-o",
            "PreferredAuthentications=gssapi-with-mic,hostbased,publickey,keyboard-interactive,password",
        ]
    else:
        ssh_opts += ["-o", "PreferredAuthentications=keyboard-interactive,password"]
    ssh_opts += [
        "-o", "NumberOfPasswordPrompts=1",
    ]
    if known_hosts_path:
        ssh_opts += ["-o", f"UserKnownHostsFile={known_hosts_path}",
                     "-o", "StrictHostKeyChecking=accept-new"]
    else:
        # Avoid interactive host key prompts inside GUI/sandbox. Adjust if you want stronger checks.
        ssh_opts += ["-o", "StrictHostKeyChecking=accept-new"]

    if extra_ssh_opts:
        ssh_opts += extra_ssh_opts

    logger = logging.getLogger(__name__)
    sshpass = get_sshpass_path()
    sshbin = shutil.which("ssh") or "/usr/bin/ssh"
    logger.debug(f"sshpass resolved to: {sshpass}")
    logger.debug(f"sshbin resolved to: {sshbin}")
    
    if sshpass:
        cmd = [sshpass, "-f", fifo, sshbin, "-p", str(port), *ssh_opts, f"{user}@{host}"]
    else:
        # sshpass not available – allow interactive password prompt
        cmd = [sshbin, "-p", str(port), *ssh_opts, f"{user}@{host}"]
    if argv_tail:
        cmd += argv_tail  # e.g. ["uptime"] or ["-tt"] etc.

    # Remove SSH_ASKPASS and force never so sshpass can deliver the password via PTY
    # without OpenSSH bypassing it via askpass (including compiled-in defaults).
    env = (inherit_env or os.environ).copy()
    env.pop("SSH_ASKPASS", None)
    env["SSH_ASKPASS_REQUIRE"] = "never"

    # Ensure /app/bin is first in PATH for Flatpak compatibility
    if os.path.exists('/app/bin'):
        current_path = env.get('PATH', '')
        if '/app/bin' not in current_path:
            env['PATH'] = f"/app/bin:{current_path}"

    try:
        # Capture output or stream as you prefer
        result = subprocess.run(cmd, env=env, text=True, capture_output=True, check=False)
        return result
    finally:
        # Best-effort cleanup; FIFO isn't reusable anyway
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass


