"""IdentitiesOnly is written whether or not the block names a key.

A Host block may set ``IdentitiesOnly yes`` with no ``IdentityFile`` of its
own: the identities then come from other matching blocks or the agent. The
loader reads that as specific-key mode with an empty list; the writer used
to emit ``IdentitiesOnly yes`` only next to a key file, so saving dropped it.
"""

import pytest

from sshpilot.core.connections.ssh_config_loader import _resolve_key_select_mode
from sshpilot.ssh_config_formatter import format_ssh_config_entry


def _entry(**data):
    return format_ssh_config_entry({"nickname": "demo", "hostname": "demo", **data})


def test_identities_only_yes_without_a_key_survives_load_and_save():
    mode, explicit_no = _resolve_key_select_mode({"identitiesonly": "yes"}, False)
    entry = _entry(key_select_mode=mode, identities_only_explicit_no=explicit_no,
                   identity_files=[])
    assert entry.splitlines()[1:] == ["    IdentitiesOnly yes"]


def test_identities_only_no_without_a_key_survives_load_and_save():
    mode, explicit_no = _resolve_key_select_mode({"identitiesonly": "no"}, False)
    entry = _entry(key_select_mode=mode, identities_only_explicit_no=explicit_no,
                   identity_files=[])
    assert entry.splitlines()[1:] == ["    IdentitiesOnly no"]


@pytest.mark.parametrize("mode, lines", [
    (1, ["    IdentityFile ~/.ssh/work", "    IdentitiesOnly yes"]),
    (2, ["    IdentityFile ~/.ssh/work"]),
    (0, []),
])
def test_key_files_and_identities_only_as_before(mode, lines):
    entry = _entry(key_select_mode=mode, identity_files=["~/.ssh/work"])
    assert entry.splitlines()[1:] == lines


def test_identity_file_none_keeps_its_line_next_to_identities_only():
    entry = _entry(key_select_mode=1, identity_files=[], identity_file_none=True)
    assert entry.splitlines()[1:] == ["    IdentityFile none", "    IdentitiesOnly yes"]
