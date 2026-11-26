"""The editor agrees with OpenSSH about lines not saved yet (real app, daemon).

* A ``Match`` block after the host's own decides a switch once the host's
  line is gone: the checkbox and the "OpenSSH will try" line follow, before
  Save.
* The identity list edits only the host's own ``IdentityFile`` lines; what
  other blocks add is listed read-only, in OpenSSH's order, and saving an
  unrelated field copies none of it into the host's block.
"""
from __future__ import annotations

import getpass
import shutil

import pytest

from tests.test_gui_connection_editor_contracts import (  # noqa: F401 - fixtures
    _block,
    _open,
    root,
)
from tests.test_gui_editor_openssh_roundtrip import _find, _pump, _save, _ssh_g

pytestmark = [
    pytest.mark.gui,
    pytest.mark.skipif(shutil.which("ssh") is None, reason="needs OpenSSH"),
]

USER = getpass.getuser()


def _write(root, gui, name, text):
    root.write_text(text)
    assert _pump(lambda: _find(gui.window, name) is not None), (
        f"the daemon never listed {name}"
    )


def _answered(dialog):
    """OpenSSH has answered for the lines the section holds now."""
    return dialog.auth_policy.policy._draft_current()


def _will_try(dialog):
    return dialog.auth_policy.effective_row.get_subtitle()


def test_dropping_the_hosts_line_shows_what_a_later_match_block_decides(gui, root):
    _write(root, gui, "effmatch", (
        "Host effmatch\n    HostName 192.0.2.40\n    PasswordAuthentication yes\n"
        f"Match localuser {USER}\n    PasswordAuthentication no\n"
    ))
    dialog = _open(gui, "effmatch")
    assert _pump(lambda: _answered(dialog)), "OpenSSH never answered for the draft"
    check = dialog.auth_password_row._check
    assert check.get_active() and "password" in _will_try(dialog)

    section = dialog.auth_policy
    assert section._edit(lambda: section.policy.set_switch("password", None))
    assert _pump(lambda: _answered(dialog)), "the edit was never evaluated"
    assert not check.get_active() and not check.get_inconsistent()
    assert "password" not in _will_try(dialog)

    _save(dialog)
    assert _pump(
        lambda: "PasswordAuthentication yes" not in _block(root, "effmatch"), seconds=5
    )
    assert _ssh_g(root, "effmatch")["passwordauthentication"] == "no"


def test_checking_a_method_a_match_block_also_turns_off_writes_yes(gui, root):
    _write(root, gui, "effboth", (
        "Host effboth\n    HostName 192.0.2.41\n    PasswordAuthentication no\n"
        f"Match localuser {USER}\n    PasswordAuthentication no\n"
    ))
    dialog = _open(gui, "effboth")
    assert _pump(lambda: _answered(dialog))
    check = dialog.auth_password_row._check
    assert not check.get_active()

    check.set_active(True)
    assert _pump(lambda: _answered(dialog))
    assert check.get_active() and "password" in _will_try(dialog)

    _save(dialog)
    assert _pump(
        lambda: "PasswordAuthentication yes" in _block(root, "effboth"), seconds=5
    )
    assert _ssh_g(root, "effboth")["passwordauthentication"] == "yes"


def test_inherited_identity_files_are_listed_but_never_copied(gui, root):
    _write(root, gui, "effkeys", (
        "Host effkeys\n    HostName 192.0.2.42\n    IdentitiesOnly yes\n"
        "    IdentityFile ~/.ssh/effkeys_own\n"
        "Host *\n    IdentityFile ~/.ssh/effkeys_shared\n"
    ))
    dialog = _open(gui, "effkeys")
    assert _pump(lambda: len(dialog._effective_identity_rows) == 2), (
        "the effective identity files never arrived"
    )
    assert [p.rsplit("/", 1)[-1] for p in dialog.key_editor.get_paths()] == [
        "effkeys_own"
    ]
    rows = dialog._effective_identity_rows
    assert [r.get_title().rsplit("/", 1)[-1] for r in rows] == [
        "effkeys_own", "effkeys_shared",
    ]
    assert rows[1].get_subtitle().endswith("Host *")
    assert dialog.effective_identity_row.get_subtitle() == "2 keys, tried in this order"

    dialog.port_row.set_text("2222")
    _save(dialog)
    assert _pump(lambda: "Port 2222" in _block(root, "effkeys"), seconds=5)
    assert [
        line for line in _block(root, "effkeys") if line.lower().startswith("identityfile")
    ] == ["IdentityFile ~/.ssh/effkeys_own"]
