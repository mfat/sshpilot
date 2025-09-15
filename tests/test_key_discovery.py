import os
import subprocess
import textwrap

import pytest
from sshpilot.connection_manager import ConnectionManager


def _write_dummy_key(path):
    path.write_text("dummy")
    path.with_suffix(path.suffix + ".pub").write_text("dummy")


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
    _write_dummy_key(root_key)

    nested_dir = ssh_dir / "nested"
    nested_dir.mkdir()
    nested_key = nested_dir / "id_nested"
    _write_dummy_key(nested_key)

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
    _write_dummy_key(key)

    monkeypatch.setenv("HOME", str(tmp_path))
    cm = ConnectionManager.__new__(ConnectionManager)
    cm.isolated_mode = False
    keys = ConnectionManager.load_ssh_keys(cm)
    assert keys == [str(key)]


def test_connection_manager_loads_keys_isolated(tmp_path, monkeypatch):
    home_ssh = tmp_path / ".ssh"
    home_ssh.mkdir()
    home_key = home_ssh / "id_home"
    _write_dummy_key(home_key)

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_key = config_dir / "id_iso"
    _write_dummy_key(config_key)

    cm = ConnectionManager.__new__(ConnectionManager)
    cm.isolated_mode = True
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        "sshpilot.connection_manager.get_config_dir",
        lambda: str(config_dir),
    )
    keys = ConnectionManager.load_ssh_keys(cm)
    assert sorted(keys) == sorted([str(config_key), str(home_key)])
