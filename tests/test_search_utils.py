from sshpilot.connection_manager import Connection
from sshpilot.search_utils import connection_matches


def make_connection(nickname, host):
    data = {"nickname": nickname, "host": host, "username": "user"}
    conn = Connection(data)
    return conn


def test_matches_nickname():
    conn = make_connection("server1", "192.168.0.1")
    assert connection_matches(conn, "server")
    assert not connection_matches(conn, "other")


def test_matches_host():
    conn = make_connection("server2", "10.0.0.5")
    assert connection_matches(conn, "10.0.0.5")
    assert connection_matches(conn, "10.0")


def test_does_not_match_aliases_or_hname():
    conn = make_connection("alias", "host")
    setattr(conn, "hname", "myalias")
    setattr(conn, "aliases", ["alias1", "alias2"])
    assert not connection_matches(conn, "myalias")
    assert not connection_matches(conn, "alias1")
    assert not connection_matches(conn, "alias2")
