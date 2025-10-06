import os
import shutil
import subprocess
import textwrap

import pytest
from sshpilot.connection_manager import ConnectionManager

def _generate_test_key(path):
    """Create an SSH key pair for tests using Paramiko or ssh-keygen."""
    private_path = path
    public_path = path.with_suffix(path.suffix + ".pub")

    for key_path in (private_path, public_path):
        if key_path.exists():
            key_path.unlink()

    try:
        import paramiko  # type: ignore
    except ImportError:  # pragma: no cover - handled via ssh-keygen fallback
        paramiko = None  # type: ignore

    if paramiko is not None:
        if hasattr(paramiko, "Ed25519Key") and hasattr(paramiko.Ed25519Key, "generate"):
            key = paramiko.Ed25519Key.generate()
        else:
            key = paramiko.RSAKey.generate(2048)
        key.write_private_key_file(str(private_path))
        with public_path.open("w", encoding="utf-8") as public_file:
            public_file.write(f"{key.get_name()} {key.get_base64()}\n")
        return

    ssh_keygen = shutil.which("ssh-keygen")
    if not ssh_keygen:
        pytest.skip("ssh-keygen not available and Paramiko missing")

    subprocess.run(
        [ssh_keygen, "-t", "ed25519", "-f", str(private_path), "-N", ""],
        check=True,
        capture_output=True,
    )


def test_discover_keys_recurses(tmp_path):
    # Skip if gi (PyGObject) is unavailable in system python
    gi_check = subprocess.run([
        "/usr/bin/python3", "-c", "import gi"
    ])
    if gi_check.returncode != 0:
        pytest.skip("gi not available")

    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()

    root_key = ssh_dir / "id_root"
    _generate_test_key(root_key)

    nested_dir = ssh_dir / "nested"
    nested_dir.mkdir()
    nested_key = nested_dir / "id_nested"
    _generate_test_key(nested_key)

    script = textwrap.dedent(
        """
        import sys
        from pathlib import Path
        from sshpilot.key_manager import KeyManager
        km = KeyManager(Path(sys.argv[1]))
        for k in km.discover_keys():
            print(k.private_path)
        """
    )

    env = os.environ.copy()
    env["PYTHONPATH"] = os.getcwd()
    proc = subprocess.run(
        ["/usr/bin/python3", "-c", script, str(ssh_dir)],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    paths = set(proc.stdout.strip().splitlines())
    assert str(root_key) in paths
    assert str(nested_key) in paths


def test_connection_manager_loads_keys_standard(tmp_path, monkeypatch):
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    key = ssh_dir / "id_test"
    _generate_test_key(key)

    monkeypatch.setenv("SSHPILOT_SSH_DIR", str(ssh_dir))
    cm = ConnectionManager.__new__(ConnectionManager)
    cm.isolated_mode = False
    keys = ConnectionManager.load_ssh_keys(cm)
    assert keys == [str(key)]


def test_connection_manager_loads_keys_isolated(tmp_path, monkeypatch):
    home_ssh = tmp_path / ".ssh"
    home_ssh.mkdir()
    home_key = home_ssh / "id_home"
    _generate_test_key(home_key)

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_key = config_dir / "id_iso"
    _generate_test_key(config_key)

    cm = ConnectionManager.__new__(ConnectionManager)
    cm.isolated_mode = True
    monkeypatch.setenv("SSHPILOT_SSH_DIR", str(home_ssh))
    monkeypatch.setattr(
        "sshpilot.connection_manager.get_config_dir",
        lambda: str(config_dir),
    )
    keys = ConnectionManager.load_ssh_keys(cm)
    assert sorted(keys) == sorted([str(config_key), str(home_key)])

