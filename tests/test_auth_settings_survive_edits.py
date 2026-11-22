"""An unrelated edit must not change how a host logs in.

Before, saving a HostName change through the store:

* dropped keyboard-interactive from ``publickey,keyboard-interactive`` when
  no password was saved, breaking one-time-code logins;
* dropped ``password`` from a hand-written ``publickey,password`` the same way;
* deleted IdentityFile, CertificateFile and IdentityAgent from any host read
  as password login (``password`` ahead of ``publickey``).

Which keys to use and the login order are independent, and a saved password
is a credential, not a policy.
"""

import pytest

from sshpilot.core.connections.ssh_config_loader import load_ssh_configuration
from sshpilot.core.connections.ssh_config_store import SshConfigStore


def _load(root):
    return next(
        item for item in load_ssh_configuration(root, isolated=True).connections
        if item.id == "h"
    )


def _edit(tmp_path, body, change=None):
    """Write a block, save an edit through the store, return its lines."""
    root = tmp_path / "config"
    root.write_text("Host h\n    HostName 192.0.2.1\n" + body, encoding="utf-8")
    record = _load(root)
    SshConfigStore(root, isolated=True).update(
        "h",
        {**record.data, "nickname": "h", **(change or {"hostname": "192.0.2.2"})},
        expected_generation=0,
    )
    lines = [line.strip() for line in root.read_text().splitlines()[1:]]
    return lines, _load(root)


def _without_hostname(lines):
    return sorted(line for line in lines if not line.startswith("HostName"))


@pytest.mark.parametrize("body", [
    # One-time codes after the key; nothing saved, a fresh code each login.
    "    PreferredAuthentications publickey,keyboard-interactive\n",
    # Key first, a typed password as fallback.
    "    PreferredAuthentications publickey,password\n",
    # Password first, but this host has its own key, certificate and agent.
    "    PreferredAuthentications password,publickey\n"
    "    IdentityFile ~/.ssh/id_work\n"
    "    CertificateFile ~/.ssh/id_work-cert.pub\n"
    "    IdentityAgent none\n",
    # The app's own password preset with a dedicated key.
    "    PreferredAuthentications "
    "keyboard-interactive,password,gssapi-with-mic,hostbased,publickey\n"
    "    IdentityFile ~/.ssh/id_work\n"
    "    IdentitiesOnly yes\n",
    # Agent and hardware-key sources on a password-first host.
    "    PreferredAuthentications password,publickey\n"
    "    AddKeysToAgent yes\n"
    "    PKCS11Provider /usr/lib/p11.so\n"
    "    SecurityKeyProvider /usr/lib/sk.so\n",
])
def test_an_unrelated_edit_keeps_every_auth_line(tmp_path, body):
    authored = sorted(line.strip() for line in body.splitlines())
    lines, reloaded = _edit(tmp_path, body)
    assert "HostName 192.0.2.2" in lines
    assert _without_hostname(lines) == authored


def test_a_saved_password_does_not_change_the_login_order(tmp_path):
    body = "    PreferredAuthentications publickey,keyboard-interactive\n"
    with_password, _ = _edit(tmp_path, body, {"hostname": "192.0.2.2",
                                              "password": "secret"})
    assert "PreferredAuthentications publickey,keyboard-interactive" in with_password


def test_switching_a_password_first_host_to_key_login_keeps_its_key(tmp_path):
    from tests.test_auth_method_update import _update

    lines, data = _update(
        tmp_path,
        "    PreferredAuthentications password,publickey\n"
        "    IdentityFile ~/.ssh/id_work\n",
        {"auth_method": 0},
    )
    # The list only meant "password first"; key login drops it.
    assert not any(line.startswith("PreferredAuthentications") for line in lines)
    assert "IdentityFile ~/.ssh/id_work" in lines
    assert data["auth_method"] == 0


def test_switching_a_key_host_to_password_login_keeps_its_key(tmp_path):
    from sshpilot.ssh_config_formatter import PASSWORD_MODE_PREFERRED_AUTH
    from tests.test_auth_method_update import _update

    lines, data = _update(
        tmp_path,
        "    PreferredAuthentications publickey,keyboard-interactive\n"
        "    IdentityFile ~/.ssh/id_work\n",
        {"auth_method": 1},
    )
    assert f"PreferredAuthentications {PASSWORD_MODE_PREFERRED_AUTH}" in lines
    assert "IdentityFile ~/.ssh/id_work" in lines
    assert data["auth_method"] == 1
