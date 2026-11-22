"""An edit keeps ``IdentityFile ~/...`` as written.

The loader expands key and certificate paths so the app can open them, and
the expanded path is what an update carries back. Found by the connection
editor contract suite: turning "Only use the selected keys" off rewrote
``IdentityFile ~/.ssh/id_work`` as ``/home/<user>/.ssh/id_work``.
"""

import os

from sshpilot.core.connections.ssh_config_loader import load_ssh_configuration
from sshpilot.core.connections.ssh_config_store import SshConfigStore


def _update(tmp_path, block, change):
    root = tmp_path / "config"
    root.write_text(block, encoding="utf-8")
    record = next(
        item for item in load_ssh_configuration(root, isolated=True).connections
        if item.id == "work"
    )
    store = SshConfigStore(root, isolated=True)
    store.update(
        "work",
        {**record.data, "nickname": "work", **change},
        expected_generation=0,
    )
    return root.read_text(encoding="utf-8")


def test_untouched_tilde_paths_are_written_as_authored(tmp_path):
    text = _update(
        tmp_path,
        "Host work\n"
        "    HostName 192.0.2.40\n"
        "    IdentityFile ~/.ssh/id_work\n"
        "    CertificateFile ~/.ssh/id_work-cert.pub\n"
        "    IdentitiesOnly yes\n",
        {"key_select_mode": 2},
    )
    assert "    IdentityFile ~/.ssh/id_work\n" in text
    assert "    CertificateFile ~/.ssh/id_work-cert.pub\n" in text
    assert "IdentitiesOnly" not in text
    assert os.path.expanduser("~") not in text


def test_quoted_and_env_spellings_survive(tmp_path, monkeypatch):
    monkeypatch.setenv("KEYDIR", str(tmp_path / "keys"))
    text = _update(
        tmp_path,
        "Host work\n"
        "    HostName 192.0.2.40\n"
        '    IdentityFile "~/my keys/id_work"\n'
        "    IdentityFile $KEYDIR/id_other\n"
        "    IdentitiesOnly yes\n",
        {"key_select_mode": 2},
    )
    assert '    IdentityFile "~/my keys/id_work"\n' in text
    assert "    IdentityFile $KEYDIR/id_other\n" in text


def test_a_new_key_is_written_as_given(tmp_path):
    new_key = str(tmp_path / "id_new")
    text = _update(
        tmp_path,
        "Host work\n"
        "    HostName 192.0.2.40\n"
        "    IdentityFile ~/.ssh/id_work\n",
        {"identity_files": [os.path.expanduser("~/.ssh/id_work"), new_key],
         "key_select_mode": 2},
    )
    assert "    IdentityFile ~/.ssh/id_work\n" in text
    assert f"    IdentityFile {new_key}\n" in text
