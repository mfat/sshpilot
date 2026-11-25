"""What the connection editor's forwarding list shows for each kind of rule.

This computer is always the left side and the server the right; the arrow
points the way a forwarded connection travels. One side listens (ear), the
other is where the connection is delivered (target).
"""

from sshpilot.forward_rule_row import (
    DELIVERS,
    LISTENS,
    Side,
    describe_forward,
)


def test_local_listens_here_and_delivers_from_the_server():
    view = describe_forward({
        'type': 'local', 'listen_port': 5432,
        'remote_host': 'db.internal', 'remote_port': 5432,
    })
    assert view.kind == 'local'
    assert view.title == 'Local'
    assert view.flag == '-L 5432:db.internal:5432'
    assert view.this_computer == Side('localhost:5432', LISTENS)
    assert view.server == Side('db.internal:5432', DELIVERS)
    assert view.outbound is True


def test_local_shows_a_bind_address_it_was_given():
    view = describe_forward({
        'type': 'local', 'listen_addr': '0.0.0.0', 'listen_port': 8080,
        'remote_host': 'localhost', 'remote_port': 80,
    })
    assert view.flag == '-L 0.0.0.0:8080:localhost:80'
    assert view.this_computer.address == '0.0.0.0:8080'


def test_remote_listens_on_the_server_and_delivers_here():
    view = describe_forward({
        'type': 'remote', 'listen_port': 9000,
        'local_host': 'localhost', 'local_port': 3000,
    })
    assert view.kind == 'remote'
    assert view.flag == '-R 9000:localhost:3000'
    assert view.this_computer == Side('localhost:3000', DELIVERS)
    assert view.server == Side('localhost:9000', LISTENS)
    assert view.outbound is False


def test_remote_falls_back_to_the_legacy_destination_fields():
    view = describe_forward({
        'type': 'remote', 'listen_addr': '0.0.0.0', 'listen_port': 9000,
        'remote_host': '192.168.1.5', 'remote_port': 3000,
    })
    assert view.flag == '-R 0.0.0.0:9000:192.168.1.5:3000'
    assert view.this_computer.address == '192.168.1.5:3000'
    assert view.server.address == '0.0.0.0:9000'


def test_remote_without_a_destination_is_a_remote_socks_proxy():
    for rule in (
        {'type': 'remote', 'listen_port': 1080},
        {'type': 'remote', 'listen_port': 1080, 'socks': True,
         'local_host': 'localhost', 'local_port': 22},
    ):
        view = describe_forward(rule)
        assert view.kind == 'remote-socks'
        assert view.title == 'Remote SOCKS'
        assert view.flag == '-R 1080'
        assert view.this_computer == Side('any host', DELIVERS)
        assert view.server == Side('SOCKS localhost:1080', LISTENS)
        assert view.outbound is False


def test_dynamic_is_a_socks_proxy_here_reaching_anything_from_the_server():
    view = describe_forward({'type': 'dynamic', 'listen_port': 1080})
    assert view.kind == 'dynamic'
    assert view.flag == '-D 1080'
    assert view.this_computer == Side('SOCKS localhost:1080', LISTENS)
    assert view.server == Side('any host', DELIVERS)
    assert view.outbound is True


def test_unknown_type_is_not_shown():
    assert describe_forward({'type': 'x11', 'listen_port': 6000}) is None
    assert describe_forward({}) is None


def test_server_name_comes_from_the_alias_then_the_host():
    from sshpilot.connection_dialog import ConnectionDialog

    class _Row:
        def __init__(self, text):
            self.text = text

        def get_text(self):
            return self.text

    dialog = ConnectionDialog.__new__(ConnectionDialog)
    dialog.nickname_row = _Row('prod-server')
    dialog.hostname_row = _Row('203.0.113.10')
    assert dialog._forwarding_server_name() == 'prod-server'
    dialog.nickname_row = _Row('  ')
    assert dialog._forwarding_server_name() == '203.0.113.10'
    dialog.hostname_row = _Row('')
    assert dialog._forwarding_server_name() == 'Server'
