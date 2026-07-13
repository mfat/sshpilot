"""Regression test for issue #953.

After duplicating a connection and editing its nickname/hostname, opening it
failed with "hostname contains invalid characters" until a second save or a
restart. Cause: the connection cached the parsed Host-line tokens
(``data['__host_tokens']`` / ``data['host']``) from before the edit — e.g. a
duplicate's ``"orig (Copy)"`` — and ``resolve_host_identifier()`` handed that
stale, paren-containing token to ssh as the native target.

``ConnectionManager.update_connection`` now refreshes those tokens from the new
nickname so the in-memory object matches the rewritten ``~/.ssh/config``.
"""

from sshpilot.connection_manager import Connection, ConnectionManager


def _make_cm():
    cm = ConnectionManager.__new__(ConnectionManager)
    cm.connections = []
    cm.ssh_config_path = '/tmp/sshpilot-test-config'
    # Avoid touching the real config file / GObject signal machinery; we only
    # exercise the in-memory token refresh.
    cm.update_ssh_config_file = lambda *a, **k: True
    cm.emit = lambda *a, **k: None
    return cm


def test_edit_refreshes_stale_host_tokens_issue_953():
    # A duplicated connection whose parsed Host line was "orig (Copy)".
    conn = Connection({
        'nickname': 'orig (Copy)',
        'host': 'orig (Copy)',
        'hostname': '1.2.3.4',
        'username': 'u',
        '__host_tokens': ['orig (Copy)'],
    })
    # Pre-edit, the native target is the paren-containing alias.
    assert conn.resolve_host_identifier() == 'orig (Copy)'

    cm = _make_cm()
    cm.connections = [conn]

    # The dialog save sends the new nickname/hostname but no __host_tokens.
    assert cm.update_connection(conn, {
        'nickname': 'cleanname',
        'hostname': '5.6.7.8',
        'username': 'u',
    })

    target = conn.resolve_host_identifier()
    assert target == 'cleanname'
    assert '(' not in target and ')' not in target and ' ' not in target


def test_edit_preserves_supplied_host_tokens():
    """When the caller supplies __host_tokens (config reload), keep them."""
    conn = Connection({
        'nickname': 'web1',
        'host': 'web1',
        'hostname': '10.0.0.1',
        'username': 'u',
        '__host_tokens': ['web1'],
    })
    cm = _make_cm()
    cm.connections = [conn]

    assert cm.update_connection(conn, {
        'nickname': 'web1',
        'hostname': '10.0.0.1',
        'username': 'u',
        '__host_tokens': ['web1', 'web1-alias'],
    })
    # Supplied tokens are respected (not overwritten by the nickname-only refresh).
    assert conn.data['__host_tokens'] == ['web1', 'web1-alias']


def test_update_skips_password_io_prehandled_by_dialog_worker():
    conn = Connection({
        'nickname': 'web1',
        'hostname': '10.0.0.1',
        'username': 'u',
    })
    cm = _make_cm()
    cm.connections = [conn]
    calls = []
    cm.store_connection_password = lambda *a, **k: calls.append((a, k))

    assert cm.update_connection(conn, {
        'nickname': 'web1',
        'hostname': '10.0.0.1',
        'username': 'u',
        'password': 'secret',
        '__secret_storage_done': True,
    })

    assert calls == []
    assert '__secret_storage_done' not in conn.data
