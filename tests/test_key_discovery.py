import os
import subprocess
import textwrap

import pytest


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


def test_generate_key_uses_config_dir_when_isolated(tmp_path):
    # Skip if gi (PyGObject) is unavailable in system python
    gi_check = subprocess.run([
        "/usr/bin/python3", "-c", "import gi"
    ])
    if gi_check.returncode != 0:
        pytest.skip("gi not available")

    env = os.environ.copy()
    env["PYTHONPATH"] = os.getcwd()
    env["XDG_CONFIG_HOME"] = str(tmp_path)

    script = textwrap.dedent(
        """
        import subprocess
        from pathlib import Path
        from sshpilot.key_manager import KeyManager
        from sshpilot.platform_utils import get_config_dir

        class DummyResult:
            def __init__(self):
                self.returncode = 0
                self.stdout = ''
                self.stderr = ''

        def fake_run(cmd, capture_output=True, text=True, check=False):
            key_path = Path(cmd[cmd.index('-f') + 1])
            key_path.write_text('dummy')
            key_path.with_suffix(key_path.suffix + '.pub').write_text('dummy')
            return DummyResult()

        subprocess.run = fake_run

        km = KeyManager(Path(get_config_dir()))
        key = km.generate_key('testkey')
        print(key.private_path)
        """
    )

    proc = subprocess.run(
        ["/usr/bin/python3", "-c", script],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )

    key_path = tmp_path / "sshpilot" / "testkey"
    assert proc.stdout.strip() == str(key_path)
    assert key_path.exists()
    assert key_path.with_suffix(".pub").exists()
