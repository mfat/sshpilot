"""The route the connection editor's Routing page draws, from its fields."""

from sshpilot.connection_dialog_route import (
    COMMAND, JUMP, TARGET, command_wins, reorder, route_stops, unused_setting,
)


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


def test_a_proxy_command_is_one_stop_and_by_default_jump_hosts_win():
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


class _Row:
    def __init__(self, text):
        self.text = text

    def get_text(self):
        return self.text

    def set_text(self, text):
        self.text = text


def _dialog(text, inherited=False):
    from sshpilot.connection_dialog_route import ConnectionDialogRouteMixin

    dialog = ConnectionDialogRouteMixin()
    dialog.proxy_jump_row = _Row(text)
    dialog._inherited_row_handlers = {"proxy_jump_row": object()} if inherited else {}
    return dialog


def test_a_jump_host_is_removed_from_the_row():
    dialog = _dialog("gate, admin@edge:2200, core")
    dialog._remove_jump_host(1)
    assert dialog.proxy_jump_row.text == "gate,core"
    dialog._remove_jump_host(0)
    dialog._remove_jump_host(0)
    assert dialog.proxy_jump_row.text == ""            # a direct connection
    dialog._remove_jump_host(0)                        # nothing left: nothing done
    assert dialog.proxy_jump_row.text == ""


def test_the_last_inherited_jump_host_leaves_none():
    # An empty row would inherit the chain again from the other block.
    dialog = _dialog("gate", inherited=True)
    dialog._remove_jump_host(0)
    assert dialog.proxy_jump_row.text == "none"
    dialog = _dialog("gate, edge", inherited=True)
    dialog._remove_jump_host(0)
    assert dialog.proxy_jump_row.text == "edge"


def test_the_setting_openssh_obtains_first_is_the_one_drawn():
    # ssh -G reports only the one in force.
    assert command_wins({"proxycommand": "nc %h %p"})
    assert not command_wins({"proxyjump": "gate", "proxycommand": "nc %h %p"})
    assert not command_wins({"proxycommand": "none"}) and not command_wins(None)
    both = dict(jump="gate", command="nc %h %p")
    assert unused_setting(both["jump"], both["command"], False) == "command"
    assert unused_setting(both["jump"], both["command"], True) == "jump"
    assert unused_setting("gate", "", True) is None
    assert unused_setting("none", "nc %h %p", False) is None
    assert [s[2] for s in _stops(command_first=True, **both)] == [COMMAND, TARGET]
    assert [s[2] for s in _stops(command_first=False, **both)] == [JUMP, TARGET]


def test_an_ssh_uri_hop_is_a_saved_connection_like_any_other():
    saved = {"gate": ("gate.example.org", 22)}
    assert _stops(jump="ssh://admin@gate:2222/", saved=saved)[0] == (
        "ssh://admin@gate:2222/", "gate.example.org:2222", JUMP, False,
    )
    assert _stops(jump="ssh://gate/", saved=saved)[0][1] == "gate.example.org:22"
