from sshpilot.connection_manager import Connection
from sshpilot.search_utils import connection_matches


def make_connection(nickname, host, ip=None):
    data = {"nickname": nickname, "host": host, "username": "user"}
    conn = Connection(data)
    if ip:
        setattr(conn, "ip", ip)
    return conn


def test_matches_nickname():
    conn = make_connection("server1", "192.168.0.1")
    assert connection_matches(conn, "server")
    assert not connection_matches(conn, "other")


def test_matches_host():
    conn = make_connection("server2", "10.0.0.5")
    assert connection_matches(conn, "10.0.0.5")
    assert connection_matches(conn, "10.0")


def test_matches_ip():
    conn = make_connection("server-ip", "example.com", ip="192.168.1.50")
    assert connection_matches(conn, "192.168")
    assert not connection_matches(conn, "10.0")


def test_ignores_aliases():
    conn = Connection({"nickname": "srv", "host": "host", "username": "user", "aliases": ["alias1", "alias2"]})
    assert not connection_matches(conn, "alias1")
    assert not connection_matches(conn, "alias2")
