"""The route the connection editor's Routing page draws, from its fields."""

from sshpilot.connection_dialog_route import COMMAND, JUMP, TARGET, reorder, route_stops


def _stops(**fields):
    base = dict(host="app", hostname="", port="", jump="", command="")
    base.update(fields)
    return [(s.name, s.endpoint, s.kind, s.inherited) for s in route_stops(**base)]


def test_a_direct_connection_goes_straight_to_its_host():
    assert _stops(hostname="10.0.0.5", port="2200") == [
        ("app", "10.0.0.5:2200", TARGET, False),
    ]
    assert _stops() == [("app", "app:22", TARGET, False)]


def test_jump_hosts_come_first_with_the_address_of_a_saved_connection():
    saved = {"gate": ("gate.example.org", 2222)}
    assert _stops(hostname="10.0.0.5", jump="gate, admin@edge:2200", saved=saved) == [
        ("gate", "gate.example.org:2222", JUMP, False),
        ("admin@edge:2200", "", JUMP, False),
        ("app", "10.0.0.5:22", TARGET, False),
    ]
    assert _stops(jump="unknown")[0] == ("unknown", "", JUMP, False)


def test_a_proxy_command_is_one_stop_and_a_jump_host_wins_over_it():
    assert _stops(command="ssh -W %h:%p gate")[0] == (
        "Proxy command", "ssh -W %h:%p gate", COMMAND, False,
    )
    assert _stops(command="none") == [("app", "app:22", TARGET, False)]
    assert [s[2] for s in _stops(jump="gate", command="nc %h %p")] == [JUMP, TARGET]


def test_an_inherited_jump_host_is_dimmed_with_where_it_comes_from():
    (hop, _target) = route_stops(
        host="app", hostname="", port="", jump="gate", command="",
        inherited={"proxy_jump_row"}, source="Inherited",
    )
    assert hop.inherited and hop.source == "Inherited"


def test_a_jump_host_moves_to_where_it_is_dropped():
    hops = ["a", "b", "c"]
    assert reorder(hops, 0, 3) == ["b", "c", "a"]        # dropped after the last
    assert reorder(hops, 2, 0) == ["c", "a", "b"]        # before the first
    assert reorder(hops, 0, 2) == ["b", "a", "c"]        # after the second
    assert reorder(hops, 1, 1) is None and reorder(hops, 1, 2) is None   # its own place
    assert reorder(hops, 5, 0) is None and hops == ["a", "b", "c"]
