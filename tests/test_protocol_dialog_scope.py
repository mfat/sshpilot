"""The connection editor shows each protocol only what applies to it.

The headless half pins the plugin side of the contract: which protocols opt
out of the pre-connection step, and the field-level rules the dialog relies
on. The real-libadwaita half (skipped under the suite's stubbed ``gi``; run
with ``SSHPILOT_GUI_TESTS=1`` on a display) drives the actual dialog, because
visibility, row placement and the protocol switch are widget behaviour that a
fake form cannot show.
"""

from types import SimpleNamespace

import pytest

from sshpilot.plugins.loader import ensure_builtin_protocols
from sshpilot.plugins.registry import protocol_registry


@pytest.fixture(scope="module")
def backends():
    ensure_builtin_protocols()
    return {b.protocol_id: b for b in protocol_registry().all()}


def _field_keys(backend):
    return {spec.key for spec in backend.connection_fields()}


def test_only_serial_opts_out_of_pre_connect(backends):
    assert {pid for pid, b in backends.items() if not b.pre_connect} == {"serial"}


def test_knock_and_wake_on_lan_follow_the_host_field(backends):
    # The dialog offers both only to protocols with a ``host`` to target.
    with_host = {pid for pid, b in backends.items() if "host" in _field_keys(b)}
    assert with_host == {"telnet", "mosh", "rdp"}


def test_rdp_refuses_a_password_it_would_not_send(backends):
    rdp = backends["rdp"]
    errors = rdp.validate({"host": "win", "password": "pw", "username": ""})
    assert any("username" in e for e in errors)
    assert not rdp.validate({"host": "win", "password": "pw", "username": "bob"})


def test_mosh_offers_the_password_its_ssh_bootstrap_uses(backends):
    spec = {s.key: s for s in backends["mosh"].connection_fields()}["password"]
    assert spec.kind == "password"


def _real_gtk_available():
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw
        Adw.init()
        return type(Adw.PreferencesGroup()).__name__ == "PreferencesGroup"
    except Exception:
        return False


def _require_gtk():
    if not _real_gtk_available():
        pytest.skip("needs real libadwaita (gi is stubbed under the suite)")


class _Manager:
    isolated_mode = False

    def __init__(self, connections=(), metadata=None):
        self.connections = list(connections)
        self._metadata = metadata or {}

    def load_ssh_keys(self): return []
    def get_key_passphrase(self, p): return ""
    def find_connection_by_nickname(self, n): return None
    def get_password(self, h, u): return None
    def format_ssh_config_entry(self, data): return ""
    def get_metadata(self, n): return dict(self._metadata)


def _dialog(connections=(), connection=None, metadata=None):
    from gi.repository import Gtk
    from sshpilot.connection_dialog import ConnectionDialog

    ensure_builtin_protocols()
    parent = Gtk.Window()
    parent.connection_manager = _Manager(connections, metadata)
    dlg = ConnectionDialog(parent, connection=None,
                           connection_manager=parent.connection_manager)
    if connection is not None:
        dlg.connection = connection
        dlg.is_editing = True
        dlg.load_connection_data()
    return dlg


def _select(dlg, protocol_id):
    for index, backend in enumerate(dlg._protocol_backends):
        if backend.protocol_id == protocol_id:
            dlg.protocol_row.set_selected(index)
            return
    raise AssertionError(protocol_id)


def _visible_pages(dlg):
    return {name for name, page in dlg._stack_pages.items() if page.get_visible()}


def _visible_row_titles(group):
    from gi.repository import Adw

    titles, pending = [], [group.get_first_child()]
    while pending:
        widget = pending.pop(0)
        while widget is not None:
            if isinstance(widget, Adw.PreferencesRow) and widget.get_visible():
                titles.append(widget.get_title())
            elif widget.get_visible():
                pending.append(widget.get_first_child())
            widget = widget.get_next_sibling()
    return titles


@pytest.mark.integration
@pytest.mark.parametrize("protocol_id,pages", [
    ("ssh", {"connection", "authentication", "forwarding", "commands",
             "advanced", "wol"}),
    ("rdp", {"connection", "commands", "wol"}),
    ("telnet", {"connection", "commands", "wol"}),
    ("mosh", {"connection", "commands", "wol"}),
    ("docker", {"connection", "commands"}),
    ("k8s", {"connection", "commands"}),
    ("serial", {"connection"}),
])
def test_each_protocol_shows_only_its_pages(protocol_id, pages):
    _require_gtk()
    dlg = _dialog()
    _select(dlg, protocol_id)
    assert _visible_pages(dlg) == pages


@pytest.mark.integration
def test_ssh_is_listed_first():
    _require_gtk()
    assert _dialog()._protocol_backends[0].protocol_id == "ssh"


@pytest.mark.integration
def test_plugin_fields_join_the_first_group_without_ssh_rows():
    _require_gtk()
    dlg = _dialog()
    _select(dlg, "rdp")
    assert dlg._host_group.get_title() == "RDP"
    assert _visible_row_titles(dlg._host_group) == [
        "Protocol", "Name (optional)",
        "Host", "Port", "Username", "Domain", "Password",
    ]
    _select(dlg, "ssh")
    assert dlg._host_group.get_title() == "Host"
    # Host, port, user: the order the plugin fields above take too.
    assert _visible_row_titles(dlg._host_group) == [
        "Protocol", "Name (optional)", "SSH Alias (no whitespace allowed)",
        "Hostname / IP address", "Port", "Username",
    ]


@pytest.mark.integration
@pytest.mark.parametrize("protocol_id", ["ssh", "rdp"])
def test_tags_and_group_close_the_page_for_every_protocol(protocol_id):
    _require_gtk()
    dlg = _dialog()
    _select(dlg, protocol_id)
    assert dlg._organize_group.get_title() == "Organize"
    assert _visible_row_titles(dlg._organize_group)[0] == "Tags (comma-separated)"
    # The protocol's own groups come before it, not after.
    page = dlg._organize_group.get_parent()
    assert dlg._plugin_fields_box.get_next_sibling() is dlg._organize_group
    assert page.get_last_child() is dlg._organize_group


def _visible_sidebar_entries(dlg):
    return [name for name, row in dlg._sidebar_rows.items() if row.get_visible()]


@pytest.mark.integration
@pytest.mark.parametrize("protocol_id,pages", [
    ("ssh", ["connection", "authentication", "forwarding", "commands",
             "advanced", "wol"]),
    ("docker", ["connection", "commands"]),
])
def test_the_sidebar_lists_only_the_pages_shown(protocol_id, pages):
    _require_gtk()
    dlg = _dialog()
    _select(dlg, protocol_id)
    assert _visible_sidebar_entries(dlg) == pages
    assert not dlg.split_view.get_collapsed()
    assert dlg.config_buttons_box.get_visible() == (protocol_id == "ssh")


@pytest.mark.integration
def test_a_lone_page_shows_without_a_sidebar():
    _require_gtk()
    dlg = _dialog()
    _select(dlg, "serial")
    assert dlg.split_view.get_collapsed()
    assert dlg.split_view.get_show_content()
    assert not dlg.content_page.get_can_pop()
    _select(dlg, "ssh")
    assert not dlg.split_view.get_collapsed()
    assert dlg.content_page.get_can_pop()


@pytest.mark.integration
def test_hiding_the_shown_page_moves_to_a_shown_one():
    _require_gtk()
    dlg = _dialog()
    assert dlg.select_page("advanced")
    _select(dlg, "docker")
    assert dlg.content_stack.get_visible_child_name() == "connection"
    assert not dlg.select_page("advanced")


@pytest.mark.integration
def test_a_refused_save_shows_the_page_of_the_field_to_fix():
    _require_gtk()
    dlg = _dialog()
    dlg.nickname_row.set_text("web")
    dlg.hostname_row.set_text("not a host!!")
    dlg.select_page("commands")
    saved, _errors = _save(dlg)
    assert not saved
    assert dlg.content_stack.get_visible_child_name() == "connection"


@pytest.mark.integration
def test_the_header_names_the_page_and_the_target():
    _require_gtk()
    dlg = _dialog()
    dlg.hostname_row.set_text("box.example.com")
    dlg.username_row.set_text("alice")
    assert dlg.content_title.get_title() == "Connection"
    assert dlg.content_title.get_subtitle() == "alice@box.example.com"
    dlg.port_row.set_text("2222")
    assert dlg.content_title.get_subtitle() == "alice@box.example.com:2222"
    dlg.select_page("forwarding")
    assert dlg.content_title.get_title() == "Port Forwarding"


@pytest.mark.integration
def test_hostless_protocol_offers_the_command_without_a_knock():
    _require_gtk()
    dlg = _dialog()
    _select(dlg, "docker")
    assert not dlg._pre_command_knock_prow.get_visible()
    assert not dlg.pre_command_command_radio.get_visible()
    assert not dlg.get_pre_command_mode_is_knock()
    assert dlg.pre_command_view.get_sensitive()


@pytest.mark.integration
def test_stored_knock_mode_is_rescoped_for_a_hostless_connection():
    _require_gtk()
    conn = SimpleNamespace(
        nickname="c1", display_name="", protocol="docker", hostname="",
        username="", port=22, data={"container": "web", "command": "sh"},
    )
    dlg = _dialog(connection=conn, metadata={
        "pre_command_mode": "knock", "pre_command_knock": "7000",
        "pre_command": "vpn-up",
    })
    assert not dlg.get_pre_command_mode_is_knock()
    assert dlg.get_pre_command_text() == "vpn-up"


@pytest.mark.integration
def test_placeholders_reach_the_entry():
    _require_gtk()
    from sshpilot.connection_dialog import _entry_row_text

    dlg = _dialog()
    _select(dlg, "rdp")
    row = dlg._plugin_field_widgets["size"][1]
    assert _entry_row_text(row).get_placeholder_text() == "1600x900"


@pytest.mark.integration
def test_switching_protocols_keeps_what_was_typed():
    _require_gtk()
    dlg = _dialog()
    dlg.hostname_row.set_text("box.example.com")
    dlg.username_row.set_text("alice")
    _select(dlg, "rdp")
    fields = dlg._plugin_field_widgets
    assert fields["host"][2]() == "box.example.com"
    assert fields["username"][2]() == "alice"
    assert fields["port"][2]() == 3389  # a port is not carried over
    fields["domain"][3]("CORP")
    _select(dlg, "telnet")
    _select(dlg, "rdp")
    assert dlg._plugin_field_widgets["domain"][2]() == "CORP"


@pytest.mark.integration
def test_test_and_wake_on_lan_target_the_protocols_host():
    _require_gtk()
    dlg = _dialog()
    _select(dlg, "rdp")
    dlg._plugin_field_widgets["host"][3]("10.0.0.5")
    assert dlg._connection_target() == ("10.0.0.5", "", 3389)


def _save(dlg):
    saved, errors = [], []
    dlg.connect("connection-saved", lambda _d, data, *_rest: saved.append(data))
    dlg.show_error = errors.append
    dlg.on_save_clicked()  # also the Ctrl+S path, which skips the button
    return saved, errors


@pytest.mark.integration
def test_an_unnamed_plugin_connection_is_named_after_its_target():
    _require_gtk()
    dlg = _dialog(connections=[SimpleNamespace(nickname="Router")])
    _select(dlg, "telnet")
    dlg._plugin_field_widgets["host"][3]("router")
    dlg._plugin_field_widgets["port"][3](2323)
    saved, errors = _save(dlg)
    assert not errors
    # As Tabby and GNOME Connections do; the ID is clear of an existing one
    # in any case.
    assert saved[-1]["display_name"] == "router:2323"
    assert saved[-1]["nickname"] == "router-2323"


@pytest.mark.integration
def test_a_named_plugin_connection_takes_its_id_from_the_name():
    _require_gtk()
    dlg = _dialog(connections=[SimpleNamespace(nickname="Core-Router")])
    _select(dlg, "telnet")
    dlg._plugin_field_widgets["host"][3]("router")
    dlg.display_name_row.set_text("Core router")
    saved, _errors = _save(dlg)
    assert saved[-1]["nickname"] == "core-router-2"
    assert saved[-1]["display_name"] == "Core router"


def _spin_until(condition, timeout=5.0):
    import time
    from gi.repository import GLib

    deadline = time.monotonic() + timeout
    context = GLib.MainContext.default()
    while not condition() and time.monotonic() < deadline:
        context.iteration(False)
        time.sleep(0.01)
    return condition()


@pytest.mark.integration
def test_a_suggestion_fills_its_field():
    _require_gtk()
    from gi.repository import Adw, Gtk

    dlg = _dialog()
    _select(dlg, "serial")
    _spec, row, getter, _setter = dlg._plugin_field_widgets["baud"]
    button = None
    pending = [row.get_first_child()]
    while pending and button is None:
        widget = pending.pop()
        while widget is not None:
            if isinstance(widget, Gtk.Button) and widget.get_icon_name() == "view-list-symbolic":
                button = widget
                break
            pending.append(widget.get_first_child())
            widget = widget.get_next_sibling()
    assert button is not None
    button.emit("clicked")

    def _rows():
        popover = next((c for c in _children(button) if isinstance(c, Gtk.Popover)), None)
        return [w for w in _walk(popover) if isinstance(w, Adw.ActionRow)] if popover else []

    assert _spin_until(lambda: len(_rows()) >= 10), "list never appeared"
    picked = next(r for r in _rows() if r.get_title() == "921600")
    picked.activate()
    assert _spin_until(lambda: getter() == "921600")


def _children(widget):
    child = widget.get_first_child()
    while child is not None:
        yield child
        child = child.get_next_sibling()


def _walk(widget):
    for child in _children(widget):
        yield child
        yield from _walk(child)


@pytest.mark.integration
def test_renaming_a_plugin_connection_keeps_its_id():
    _require_gtk()
    conn = SimpleNamespace(
        nickname="core-router", display_name="Core router", protocol="telnet",
        hostname="router", username="", port=23, data={},
    )
    dlg = _dialog(connection=conn)
    assert not dlg.nickname_row.get_visible()
    assert dlg.display_name_row.get_text() == "Core router"
    dlg.display_name_row.set_text("Edge router")
    saved, _errors = _save(dlg)
    assert saved[-1]["nickname"] == "core-router"
    assert saved[-1]["display_name"] == "Edge router"


@pytest.mark.integration
@pytest.mark.parametrize("name,expected", [
    ("Office PC", "office-pc"),
    ("  --lab box!! ", "lab-box"),
    ("دفتر مرکزی", "دفتر-مرکزی"),
    ("!!!", "rdp"),
])
def test_generated_ids_are_valid_store_keys(name, expected):
    _require_gtk()
    from sshpilot.api.models.common import validate_ssh_host_alias

    generated = _dialog()._generate_connection_id(name, "rdp")
    assert generated == expected
    assert validate_ssh_host_alias(generated) == generated


@pytest.mark.integration
def test_hidden_ssh_rows_do_not_gate_a_plugin_save():
    _require_gtk()
    dlg = _dialog()
    dlg.nickname_row.set_text("ok-id")
    dlg.hostname_row.set_text("not a host!!")
    _select(dlg, "rdp")
    assert dlg.save_button.get_sensitive()
    _select(dlg, "ssh")
    assert not dlg.save_button.get_sensitive()


@pytest.mark.integration
def test_a_chosen_file_can_be_cleared():
    _require_gtk()
    from gi.repository import Gtk

    dlg = _dialog()
    _select(dlg, "k8s")
    _spec, row, getter, setter = dlg._plugin_field_widgets["kubeconfig"]
    setter("/tmp/kubeconfig")
    clear, pending = None, [row.get_first_child()]
    while pending and clear is None:
        widget = pending.pop()
        while widget is not None:
            if isinstance(widget, Gtk.Button) and widget.get_icon_name() == "edit-clear-symbolic":
                clear = widget
                break
            pending.append(widget.get_first_child())
            widget = widget.get_next_sibling()
    assert clear is not None and clear.get_visible()
    clear.emit("clicked")
    assert getter() == "" and not clear.get_visible()


# -- the alias read as a Host line --------------------------------------------

def _pump_events(count=30):
    from gi.repository import GLib
    context = GLib.MainContext.default()
    for _ in range(count):
        while context.iteration(False):
            pass


@pytest.mark.integration
def test_several_names_are_handed_to_advanced_configuration_as_one_host_line():
    _require_gtk()
    dlg = _dialog()
    opened = []
    dlg.parent_window.open_advanced_config = lambda **kw: opened.append(kw) or object()
    closed = []
    dlg.close = lambda: closed.append(True)
    row = dlg.host_line_offer_row
    assert not row.get_visible()
    dlg.nickname_row.set_text("web1 web2 web3")
    dlg.hostname_row.set_text("10.0.0.5")
    _pump_events()
    assert row.get_visible() and dlg.host_line_offer_button.get_visible()
    assert row.get_title() == "A connection has one name"
    assert dlg._validate_all_required_for_save() is dlg.nickname_row   # not saved here
    assert dlg.display_name_row.get_sensitive()
    dlg.host_line_offer_button.emit("clicked")
    assert opened and opened[0]["new_block"] == (
        "web1 web2 web3", [("HostName", "10.0.0.5")],
    )
    assert closed
    dlg.nickname_row.set_text("web1")
    _pump_events()
    assert not row.get_visible()


@pytest.mark.integration
def test_a_pattern_is_handed_to_advanced_configuration_and_the_new_dialog_closes():
    _require_gtk()
    dlg = _dialog()
    opened = []
    dlg.parent_window.open_advanced_config = lambda **kw: opened.append(kw) or object()
    # A window never shown has nothing to close: watch the call.
    closed = []
    dlg.close = lambda: closed.append(True)
    dlg.nickname_row.set_text("*.prod")
    dlg.hostname_row.set_text("%h.example.com")
    dlg.username_row.set_text("deploy")
    _pump_events()
    assert dlg.host_line_offer_row.get_visible()
    assert dlg.host_line_offer_button.get_visible()
    assert dlg._validate_all_required_for_save() is dlg.nickname_row
    dlg.host_line_offer_button.emit("clicked")
    assert opened and opened[0]["new_block"] == (
        "*.prod", [("HostName", "%h.example.com"), ("User", "deploy")],
    )
    assert closed


@pytest.mark.integration
def test_a_saved_connection_keeps_one_name_and_is_sent_to_its_block():
    _require_gtk()
    connection = SimpleNamespace(
        nickname="web1", hostname="10.0.0.1", username="", port=22, protocol="ssh",
        source="", data={},
    )
    dlg = _dialog(connection=connection)
    opened = []
    dlg.parent_window.open_advanced_config = lambda **kw: opened.append(kw) or object()
    _pump_events()          # the saved values arrive in an idle; type after
    dlg.nickname_row.set_text("web1 web2")
    _pump_events()
    assert dlg.host_line_offer_row.get_visible()
    assert dlg.host_line_offer_button.get_visible()
    dlg.host_line_offer_button.emit("clicked")
    assert opened and opened[0]["host"] == "web1" and "new_block" not in opened[0]


@pytest.mark.integration
def test_the_host_line_row_is_about_ssh_only():
    _require_gtk()
    dlg = _dialog()
    dlg.nickname_row.set_text("*.prod")
    _pump_events()
    assert dlg.host_line_offer_row.get_visible()
    _select(dlg, "telnet")
    _pump_events()
    assert not dlg.host_line_offer_row.get_visible()


@pytest.mark.integration
def test_the_search_button_opens_the_sidebar_search_and_finds_a_field():
    _require_gtk()
    dlg = _dialog()
    _pump_events()
    assert not dlg.search_bar.get_search_mode()
    dlg.search_button.set_active(True)
    _pump_events()
    assert dlg.search_bar.get_search_mode()
    dlg.search_entry.set_text("port")
    # The entry reports what was typed after a short pause.
    import time
    deadline = time.time() + 3
    while (dlg.sidebar_stack.get_visible_child_name() != "results"
           and time.time() < deadline):
        _pump_events()
        time.sleep(0.02)
    assert dlg.sidebar_stack.get_visible_child_name() == "results"
    assert dlg.search_results.get_row_at_index(0) is not None
    dlg.search_button.set_active(False)
    _pump_events()
    assert dlg.sidebar_stack.get_visible_child_name() == "pages"
