from sshpilot.connection_manager import Connection
from sshpilot.search_utils import connection_matches


def make_connection(nickname, hostname, host=None):
    """Build a connection the way real SSH config does.

    ``host`` is the SSH ``Host`` alias (defaults to nickname). ``hostname`` is
    the ``HostName`` / IP the user expects to find via sidebar search.
    """
    data = {
        "nickname": nickname,
        "host": host if host is not None else nickname,
        "hostname": hostname,
        "username": "user",
    }
    return Connection(data)


def test_matches_nickname():
    conn = make_connection("server1", "192.168.0.1")
    assert connection_matches(conn, "server")
    assert not connection_matches(conn, "other")


def test_matches_hostname_ip():
    conn = make_connection("server2", "10.0.0.5")
    assert connection_matches(conn, "10.0.0.5")
    assert connection_matches(conn, "10.0")
    assert not connection_matches(conn, "10.0.0.6")


def test_matches_hostname_when_alias_differs():
    # Nickname/alias is not the IP — searching the IP must still work.
    conn = make_connection("prod-web", "203.0.113.10", host="prod-web")
    assert connection_matches(conn, "203.0.113.10")
    assert connection_matches(conn, "203.0.113")
    assert connection_matches(conn, "prod")


def test_matches_fqdn_hostname():
    conn = make_connection("db1", "db1.example.com")
    assert connection_matches(conn, "db1.example.com")
    assert connection_matches(conn, "example.com")
    assert not connection_matches(conn, "example.org")


def test_does_not_match_aliases_or_hname():
    conn = make_connection("alias", "host.example")
    setattr(conn, "hname", "myalias")
    setattr(conn, "aliases", ["alias1", "alias2"])
    assert not connection_matches(conn, "myalias")
    assert not connection_matches(conn, "alias1")
    assert not connection_matches(conn, "alias2")


def test_matches_tags_case_insensitive_substring():
    conn = make_connection("server3", "10.0.0.6")
    setattr(conn, "tags", ["Production", "web"])
    assert connection_matches(conn, "prod")
    assert connection_matches(conn, "WEB")
    assert not connection_matches(conn, "staging")


def test_missing_or_none_tags_do_not_crash():
    conn = make_connection("server4", "10.0.0.7")
    assert connection_matches(conn, "server4")
    assert not connection_matches(conn, "prod")
    setattr(conn, "tags", None)
    assert connection_matches(conn, "server4")
    assert not connection_matches(conn, "prod")


def test_multiple_keywords_all_must_match():
    conn = make_connection("web-server", "10.0.0.8")
    setattr(conn, "tags", ["production", "frontend"])
    # Each keyword matches a different field (nickname, tag, hostname).
    assert connection_matches(conn, "web prod")
    assert connection_matches(conn, "server 10.0 frontend")
    # One keyword absent -> no match.
    assert not connection_matches(conn, "web staging")


def test_multiple_keywords_order_independent():
    conn = make_connection("alpha", "192.168.1.10")
    setattr(conn, "tags", ["db"])
    assert connection_matches(conn, "alpha db")
    assert connection_matches(conn, "db alpha")


def test_single_keyword_with_dots_still_matches():
    conn = make_connection("server5", "10.0.0.5")
    assert connection_matches(conn, "10.0.0.5")


def test_extra_whitespace_is_ignored():
    conn = make_connection("server6", "10.0.0.9")
    setattr(conn, "tags", ["web"])
    assert connection_matches(conn, "  server   web  ")
    assert connection_matches(conn, "   ")
