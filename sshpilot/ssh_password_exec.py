# ssh_password_exec.py
import os, tempfile, threading, subprocess, shutil

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
                          inherit_env: dict | None = None) -> subprocess.CompletedProcess:
    """Launch `ssh` using sshpass -f <FIFO> safely.
    - No password in argv/env.
    - FIFO lives in private temp dir, removed afterwards.
    """
    tmpdir = _mk_priv_dir()
    fifo = os.path.join(tmpdir, "pw.fifo")
    os.mkfifo(fifo, 0o600)

    # Start writer thread that writes the password exactly once
    t = threading.Thread(target=_write_once_fifo, args=(fifo, password), daemon=True)
    t.start()

    ssh_opts = [
        "-o", "PreferredAuthentications=keyboard-interactive,password",
        "-o", "PubkeyAuthentication=no",
        "-o", "NumberOfPasswordPrompts=1",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
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
    sshpass = shutil.which("sshpass") or ("/app/bin/sshpass" if os.path.exists("/app/bin/sshpass") and os.access("/app/bin/sshpass", os.X_OK) else None)
    sshbin = shutil.which("ssh") or "/usr/bin/ssh"
    
    # Debug logging
    import logging
    logger = logging.getLogger(__name__)
    logger.debug(f"sshpass resolved to: {sshpass}")
    logger.debug(f"sshbin resolved to: {sshbin}")
    logger.debug(f"/app/bin/sshpass exists: {os.path.exists('/app/bin/sshpass')}")
    logger.debug(f"/app/bin/sshpass executable: {os.access('/app/bin/sshpass', os.X_OK) if os.path.exists('/app/bin/sshpass') else False}")
    
    if not sshpass:
        # sshpass not available → use askpass fallback
        from .askpass_utils import get_ssh_env_with_askpass
        askpass_env = get_ssh_env_with_askpass("force")
        env = (inherit_env or os.environ).copy()
        env.update(askpass_env)
        # Build cmd WITHOUT sshpass, just ssh + opts
        cmd = [sshbin, "-p", str(port), *ssh_opts, f"{user}@{host}"]
    else:
        cmd = [sshpass, "-f", fifo, sshbin, "-p", str(port), *ssh_opts, f"{user}@{host}"]
    if argv_tail:
        cmd += argv_tail  # e.g. ["uptime"] or ["-tt"] etc.

    # Important: strip askpass vars so OpenSSH won't try your passphrase helper for passwords
    # Only do this if we're using sshpass (not askpass fallback)
    if sshpass:
        env = (inherit_env or os.environ).copy()
        env.pop("SSH_ASKPASS", None)
        env.pop("SSH_ASKPASS_REQUIRE", None)
    # If using askpass fallback, env is already set up above

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
                          local_paths: list[str], remote_dir: str, *,
                          port: int = 22,
                          known_hosts_path: str | None = None,
                          extra_ssh_opts: list[str] | None = None,
                          inherit_env: dict | None = None) -> subprocess.CompletedProcess:
    tmpdir = _mk_priv_dir()
    fifo = os.path.join(tmpdir, "pw.fifo")
    os.mkfifo(fifo, 0o600)
    t = threading.Thread(target=_write_once_fifo, args=(fifo, password), daemon=True)
    t.start()

    ssh_opts = []
    if known_hosts_path:
        ssh_opts += ["-o", f"UserKnownHostsFile={known_hosts_path}",
                     "-o", "StrictHostKeyChecking=accept-new"]
    else:
        ssh_opts += ["-o", "StrictHostKeyChecking=accept-new"]

    # Resolve sshpass and scp binaries
    sshpass = shutil.which("sshpass") or ("/app/bin/sshpass" if os.path.exists("/app/bin/sshpass") and os.access("/app/bin/sshpass", os.X_OK) else None)
    scpbin = shutil.which("scp") or "/usr/bin/scp"
    
    if not sshpass:
        # sshpass not available → use askpass fallback
        from .askpass_utils import get_ssh_env_with_askpass
        askpass_env = get_ssh_env_with_askpass("force")
        env = (inherit_env or os.environ).copy()
        env.update(askpass_env)
        # Build cmd WITHOUT sshpass, just scp + opts
        cmd = [scpbin, "-v", "-P", str(port), *ssh_opts, *local_paths, f"{user}@{host}:{remote_dir}"]
    else:
        cmd = [sshpass, "-f", fifo, scpbin, "-v", "-P", str(port), *ssh_opts, *local_paths, f"{user}@{host}:{remote_dir}"]

    # Only strip askpass vars if we're using sshpass (not askpass fallback)
    if sshpass:
        env = (inherit_env or os.environ).copy()
        env.pop("SSH_ASKPASS", None)
        env.pop("SSH_ASKPASS_REQUIRE", None)
    # If using askpass fallback, env is already set up above

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
