# ssh_password_exec.py
import os, tempfile, threading, subprocess, shutil

from .scp_utils import assemble_scp_transfer_args

def _write_once_fifo(path: str, secret: str):
    # Writer runs in a thread: blocks until sshpass opens FIFO for read
    with open(path, "w", encoding="utf-8") as w:
        w.write(secret)
        w.flush()

def _mk_priv_dir(prefix="sshpilot-pass-") -> str:
    d = tempfile.mkdtemp(prefix=prefix)
    os.chmod(d, 0o700)
    return d

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
        ssh_opts += ["-o", "PreferredAuthentications=password"]
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

    # Resolve sshpass and ssh binaries like in window.py/terminal.py
    sshpass = ("/app/bin/sshpass" if os.path.exists("/app/bin/sshpass") and os.access("/app/bin/sshpass", os.X_OK) else None) or shutil.which("sshpass")
    sshbin = shutil.which("ssh") or "/usr/bin/ssh"
    
    # Debug logging
    import logging
    logger = logging.getLogger(__name__)
    logger.debug(f"sshpass resolved to: {sshpass}")
    logger.debug(f"sshbin resolved to: {sshbin}")
    logger.debug(f"/app/bin/sshpass exists: {os.path.exists('/app/bin/sshpass')}")
    logger.debug(f"/app/bin/sshpass executable: {os.access('/app/bin/sshpass', os.X_OK) if os.path.exists('/app/bin/sshpass') else False}")
    
    if sshpass:
        cmd = [sshpass, "-f", fifo, sshbin, "-p", str(port), *ssh_opts, f"{user}@{host}"]
    else:
        # sshpass not available – allow interactive password prompt
        cmd = [sshbin, "-p", str(port), *ssh_opts, f"{user}@{host}"]
    if argv_tail:
        cmd += argv_tail  # e.g. ["uptime"] or ["-tt"] etc.

    # Always strip askpass vars so OpenSSH can prompt interactively if needed
    env = (inherit_env or os.environ).copy()
    env.pop("SSH_ASKPASS", None)
    env.pop("SSH_ASKPASS_REQUIRE", None)

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

def run_scp_with_password(host: str, user: str, password: str,
                          sources: list[str], destination: str, *,
                          direction: str = 'upload',
                          port: int = 22,
                          known_hosts_path: str | None = None,
                          extra_ssh_opts: list[str] | None = None,
                          inherit_env: dict | None = None,
                          use_publickey: bool = False) -> subprocess.CompletedProcess:
    tmpdir = _mk_priv_dir()
    fifo = os.path.join(tmpdir, "pw.fifo")
    os.mkfifo(fifo, 0o600)
    t = threading.Thread(target=_write_once_fifo, args=(fifo, password), daemon=True)
    t.start()

    ssh_opts = []
    if use_publickey:
        ssh_opts += [
            "-o",
            "PreferredAuthentications=gssapi-with-mic,hostbased,publickey,keyboard-interactive,password",
        ]
    else:
        ssh_opts += ["-o", "PreferredAuthentications=password"]
    ssh_opts += ["-o", "NumberOfPasswordPrompts=1"]
    if known_hosts_path:
        ssh_opts += ["-o", f"UserKnownHostsFile={known_hosts_path}",
                     "-o", "StrictHostKeyChecking=accept-new"]
    else:
        ssh_opts += ["-o", "StrictHostKeyChecking=accept-new"]
    if extra_ssh_opts:
        ssh_opts += list(extra_ssh_opts)

    # Resolve sshpass and scp binaries
    sshpass = ("/app/bin/sshpass" if os.path.exists("/app/bin/sshpass") and os.access("/app/bin/sshpass", os.X_OK) else None) or shutil.which("sshpass")
    scpbin = shutil.which("scp") or "/usr/bin/scp"

    target = f"{user}@{host}" if user else host
    transfer_sources, transfer_destination = assemble_scp_transfer_args(
        target,
        sources,
        destination,
        direction,
    )

    base_cmd = [scpbin, "-v", "-P", str(port), *ssh_opts, *transfer_sources, transfer_destination]

    if sshpass:
        cmd = [sshpass, "-f", fifo, *base_cmd]
    else:
        # sshpass not available – allow interactive password prompt
        cmd = base_cmd

    # Always strip askpass vars so OpenSSH can prompt interactively if needed
    env = (inherit_env or os.environ).copy()
    env.pop("SSH_ASKPASS", None)
    env.pop("SSH_ASKPASS_REQUIRE", None)

    # Ensure /app/bin is first in PATH for Flatpak compatibility
    if os.path.exists('/app/bin'):
        current_path = env.get('PATH', '')
        if '/app/bin' not in current_path:
            env['PATH'] = f"/app/bin:{current_path}"
    
    try:
        return subprocess.run(cmd, env=env, text=True, capture_output=True, check=False)
    finally:
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass
