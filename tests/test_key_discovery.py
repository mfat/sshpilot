import os
import subprocess
import textwrap

import pytest
from paramiko import RSAKey

from sshpilot.connection_manager import ConnectionManager


DISCOVER_SCRIPT = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from sshpilot.key_manager import KeyManager

    km = KeyManager(Path(sys.argv[1]))
    for key in km.discover_keys():
        print(key.private_path)
    """
)


def _generate_private_key(path, create_pub: bool = True):
    key = RSAKey.generate(1024)
    key.write_private_key_file(str(path))
    if create_pub:
        public_path = path.with_suffix(path.suffix + ".pub")
        public_path.write_text(f"{key.get_name()} {key.get_base64()}")


def _require_gi():
    gi_check = subprocess.run([
        "/usr/bin/python3",
        "-c",
        "import gi",
    ])
    if gi_check.returncode != 0:
        pytest.skip("gi not available")


def _run_discover_keys(ssh_dir):
    env = os.environ.copy()
    env["PYTHONPATH"] = os.getcwd()
    proc = subprocess.run(
        ["/usr/bin/python3", "-c", DISCOVER_SCRIPT, str(ssh_dir)],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    output = proc.stdout.strip()
    if not output:
        return set()
    return set(output.splitlines())


def test_discover_keys_recurses(tmp_path):
    _require_gi()

    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()

    root_key = ssh_dir / "id_root"
    _generate_private_key(root_key)

    nested_dir = ssh_dir / "nested"
    nested_dir.mkdir()
    nested_key = nested_dir / "id_nested"
    _generate_private_key(nested_key)

    missing_pub_key = ssh_dir / "id_missing_pub"
    _generate_private_key(missing_pub_key, create_pub=False)

    paths = _run_discover_keys(ssh_dir)
    assert str(root_key) in paths
    assert str(nested_key) in paths
    assert str(missing_pub_key) in paths


def test_discover_keys_without_public_file(tmp_path):
    _require_gi()

    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()

    private_key = ssh_dir / "id_no_pub"
    _generate_private_key(private_key, create_pub=False)

    paths = _run_discover_keys(ssh_dir)
    assert str(private_key) in paths


def test_connection_manager_loads_keys_standard(tmp_path, monkeypatch):
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    key = ssh_dir / "id_test"
    _generate_private_key(key)

    monkeypatch.setenv("SSHPILOT_SSH_DIR", str(ssh_dir))
    cm = ConnectionManager.__new__(ConnectionManager)
    cm.isolated_mode = False
    keys = ConnectionManager.load_ssh_keys(cm)
    assert keys == [str(key)]


def test_connection_manager_loads_keys_isolated(tmp_path, monkeypatch):
    home_ssh = tmp_path / ".ssh"
    home_ssh.mkdir()
    home_key = home_ssh / "id_home"
    _generate_private_key(home_key)

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_key = config_dir / "id_iso"
    _generate_private_key(config_key)

    cm = ConnectionManager.__new__(ConnectionManager)
    cm.isolated_mode = True
    monkeypatch.setenv("SSHPILOT_SSH_DIR", str(home_ssh))
    monkeypatch.setattr(
        "sshpilot.connection_manager.get_config_dir",
        lambda: str(config_dir),
    )
    keys = ConnectionManager.load_ssh_keys(cm)
    assert sorted(keys) == sorted([str(config_key), str(home_key)])

