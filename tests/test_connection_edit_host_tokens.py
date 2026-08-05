"""Regression test for issue #953.

After renaming a connection, the in-memory record must reflect the rewritten
``Host`` alias. A duplicate's parsed Host line (e.g. ``"orig (Copy)"``) must
never survive the rename as a stale, paren-containing native target.
"""

from sshpilot.core.connections.repository import ConnectionRepository
from sshpilot.core.connections.ssh_config_store import SshConfigStore


def _repo(tmp_path) -> ConnectionRepository:
    root = tmp_path / "config"
    root.write_text("Host orig (Copy)\n    HostName 1.2.3.4\n    User u\n")
    return ConnectionRepository(
        ssh_store=SshConfigStore(root),
        state_path=tmp_path / "connections.json",
        legacy_config_path=tmp_path / "config.json",
        isolated=False,
    )


def test_rename_refreshes_stale_host_alias(tmp_path):
    """Renaming through the repository returns a record whose identity is the
    new clean alias, with no stale paren tokens left over from the duplicate."""
    repo = _repo(tmp_path)
    # A multi-token alias line creates one record per token; the first token
    # ("orig") is the connection id. The paren token is a separate (inert)
    # record that must be cleaned up by the rename, not survive as a target.
    assert repo.get_editor_record("orig") is not None

    updated = repo.update_connection(
        "orig",
        {
            "nickname": "cleanname",
            "hostname": "5.6.7.8",
            "username": "u",
            "protocol": "ssh",
        },
    )

    assert updated.id == "cleanname"
    assert updated.data.get("host") == "cleanname"
    assert updated.nickname == "cleanname"
    # No stale host tokens survive the rename.
    data = updated.data
    assert "__host_tokens" not in data
    assert "(" not in data.get("host", "")
    assert repo.get_editor_record("orig") is None
