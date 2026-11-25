"""Advanced Configuration window, driven against real OpenSSH.

The daemon is stood in for by a client that serves one file and evaluates
drafts with the same core function the daemon calls, in Isolated Mode (the
root is passed with ``-F``).
"""
from __future__ import annotations

import hashlib
import shutil

import pytest

pytestmark = [
    pytest.mark.gui,
    pytest.mark.skipif(shutil.which("ssh") is None, reason="needs the OpenSSH client"),
]

CONFIG = (
    "# my hosts\nCompression yes\n\n# web servers\nHost web1 web2\n    User deploy\n"
    "    Port 2222\n    IdentityFile ~/.ssh/a\n\nHost *\n    User fallback\n"
    "    ConnectTimeout 99\n"
)


@pytest.fixture(autouse=True)
def _keys_named_exist():
    """The key CONFIG names exists (in the test session's own home), so a
    save is not stopped by "Save With Problems?"; tests about a missing key
    name one that is missing."""
    import pathlib

    key = pathlib.Path.home() / ".ssh" / "a"
    made = not key.exists()
    if made:
        key.parent.mkdir(parents=True, exist_ok=True)
        key.write_text("key")
        key.chmod(0o600)
    yield
    if made:
        key.unlink()


@pytest.fixture()
def gtk():
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gtk

    if not isinstance(getattr(Gtk, "MAJOR_VERSION", None), int):
        pytest.skip("gi is stubbed in this environment (no real GTK)")
    if not Gtk.init_check():
        pytest.skip("GTK cannot initialise (no display)")
    Adw.init()
    return Gtk


class _Client:
    """Serves the root at *path* and the files in *included* (named, as the
    daemon names them, by their paths)."""

    def __init__(self, path, included=()):
        self.path = path
        self.included = [str(item) for item in included]
        self.saves = 0

    def _revision(self):
        import pathlib

        text = "".join(pathlib.Path(item).read_text() for item in [self.path, *self.included])
        return "r" + hashlib.sha1(text.encode()).hexdigest()[:12]

    def _target(self, file):
        import pathlib

        if not file:
            return self.path
        if file not in self.included:
            from sshpilot.api.errors import ErrorCode, SshPilotError
            raise SshPilotError(ErrorCode.INVALID_REQUEST, "not a file of the configuration")
        return pathlib.Path(file)

    def _text(self, file):
        import pathlib

        from sshpilot.api.models.connections import SshConfigFileRef, SshConfigText

        target = self._target(file)
        files = (SshConfigFileRef("", "config", True),) + tuple(
            SshConfigFileRef(item, pathlib.Path(item).name, True) for item in self.included
        )
        return SshConfigText(
            target.read_text(), self._revision(), target.name, True,
            file=file, files=files if self.included else (),
        )

    def get_ssh_config_text(self, file=""):
        return self._text(file)

    def save_ssh_config_text(self, request):
        from sshpilot.api.errors import ErrorCode, SshPilotError

        if request.expected_revision != self._revision():
            raise SshPilotError(ErrorCode.STALE_EDITOR, "stale")
        self._target(request.file).write_text(request.text)
        self.saves += 1
        return self._text(request.file)

    def evaluate_ssh_config_draft(self, request):
        from sshpilot.core.ssh_config_draft import DraftContext, evaluate_draft

        target = self._target(request.file)
        context = DraftContext(
            str(self.path), isolated=True, overrides=("-o", "ConnectTimeout=7"),
            target_path=str(target) if request.file else None,
        )
        return evaluate_draft(request, context, target.read_text())


@pytest.fixture(autouse=True, params=["window", "embedded"])
def shell(request, monkeypatch):
    """Every test runs against both shells of the one editor: the window,
    and the widget the main window embeds (its Configuration view). They
    are the same controller (``AdvancedConfigBase``); this is what keeps a
    change that works in one from breaking the other unnoticed.

    Embedded, ``AdvancedConfigWindow`` in the module is a stand-in that
    builds the view inside a plain window laid out as the main one is: the
    list of blocks in a sidebar, the view beside it.
    """
    if request.param == "window":
        return "window"
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    if not isinstance(getattr(Gtk, "MAJOR_VERSION", None), int):
        pytest.skip("gi is stubbed in this environment (no real GTK)")
    import sshpilot.advanced_config_window as module

    class Hosted(module.AdvancedConfigView):
        __gtype_name__ = None

        def __init__(self, parent, client, **kwargs):
            super().__init__(parent, client, **kwargs)
            self._host_window = Gtk.Window()
            self._host_window.set_default_size(1400, 760)
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            self.sidebar_column.set_size_request(210, -1)
            box.append(self.sidebar_column)
            self.set_hexpand(True)
            box.append(self)
            self._host_window.set_child(box)
            self.host_present = self._host_window.present
            self.host_close = self._host_window.close

        def destroy(self):
            self._host_window.destroy()

    del Hosted.__gtype_name__
    monkeypatch.setattr(module, "AdvancedConfigWindow", Hosted)
    return "embedded"


def _pump(condition, seconds=8.0):
    from gi.repository import GLib

    context = GLib.MainContext.default()
    deadline = GLib.get_monotonic_time() + int(seconds * 1_000_000)
    while GLib.get_monotonic_time() < deadline:
        while context.pending():
            context.iteration(False)
        if condition():
            return True
        context.iteration(False)
    return bool(condition())


@pytest.fixture()
def window(gtk, tmp_path):
    from sshpilot.advanced_config_window import AdvancedConfigWindow

    path = tmp_path / "config"
    path.write_text(CONFIG)
    client = _Client(path)
    win = AdvancedConfigWindow(None, client)
    win._test_client = client
    _filter_by(win, "web1")
    yield win
    win._closing = True
    win.destroy()


def _check(win, host):
    """Keep *host* in question whichever block the window selects, as a
    source followed or ``go_to`` with *pin* does (a block picked in the
    sidebar drops it). It need not be a host the file names."""
    win._set_typed(host)
    win._target_changed()


def _command(win):
    """The question the Effective view shows (it is filled when shown)."""
    win.effective.fill()
    return win.effective.command.get_label()


def _filter_by(win, host):
    """Keep a host in question and wait for OpenSSH's answer on it."""
    assert _pump(lambda: bool(win.draft.blocks))
    _check(win, host)
    assert _pump(lambda: (
        win._evaluation is not None and win._evaluation.evaluated
        and win._evaluated_serial == win.editor.serial
        and _command(win) == f"ssh -G {host}"
    ))


def _children(widget):
    found = []
    child = widget.get_first_child()
    while child is not None:
        found.append(child)
        child = child.get_next_sibling()
    return found


def _rows(win):
    """The sidebar's block rows, in order (not the rows of files)."""
    from sshpilot.advanced_config_block_row import BlockRow

    return [row for row in _children(win.sidebar.list) if isinstance(row, BlockRow)]


def _block_rows(win):
    """The sidebar's rows, as they read."""
    return [row._text for row in _rows(win)]


def _pick(win, text):
    """Choose a row of the sidebar, as a click does."""
    win.sidebar.list.select_row(next(row for row in _rows(win) if row._text == text))


def _dimmed(win):
    """Sidebar rows drawn fainter than the others. None ever is: the user
    found blocks dimmed for another host unexplained."""
    return [
        block.patterns or block.kind for block in win.draft.blocks
        if win.sidebar._rows[block.uid].get_opacity() < 1
        or win.sidebar._rows[block.uid].has_css_class("dim-label")
    ]


def _settled(win):
    return _pump(lambda: (
        win._evaluate_source == 0
        and win._evaluation is not None
        and win._evaluated_serial == win.editor.serial
    ))


def _applies(win):
    """The hosts the title card says a pattern or Match block applies to
    ([] for none; None for a block that names its hosts)."""
    structured = win.structured
    if not structured.applies_chips.get_visible():
        return [] if "none of the hosts" in structured.applies_label.get_label() else None
    names, child = [], structured.applies_chips.get_first_child()
    while child is not None:
        inner = child.get_child()
        if isinstance(inner, type(structured.duplicate_button)):
            names.append(inner.get_label())
        child = child.get_next_sibling()
    return names


def _effective(win, key):
    """The value the Effective view lists for *key* (None: not listed)."""
    win.effective.fill()        # whichever view is open
    item = win.effective._items.get(key)
    return item.values if item is not None and item.present else None


def _field(win, keyword):
    """The row of *keyword*'s first line in the block on screen (None: it
    has none). Every line is a keyword and a value."""
    rows = win.structured.line_rows
    block = win.editor.find_block(win.selected_uid)
    for option in (block.options if block is not None else ()):
        if option.key.lower() == keyword:
            return rows.get(option.uid)
    return None


def _status(win, option):
    """What the Structured view says beside an option of the selected block."""
    block, _found = win.editor.find_option(option.uid)
    assert block is not None and block.uid == win.selected_uid, "not the block on screen"
    return win.structured.line_status(option.uid)


def _raw(win):
    """Open the Raw view (views are filled when they are on screen)."""
    win.show_page("raw")
    return win.raw.view.get_buffer()


def _selected(win):
    block = win.editor.find_block(win.selected_uid)
    return (block.patterns or block.kind) if block is not None else None


def _show(win):
    """Put the window on screen: drags and drops need sizes."""
    host = getattr(win, "_host_window", None)
    (host or win).present()
    _pump(lambda: win.sidebar.list.get_height() > 0, seconds=5)
    _pump(lambda: False, seconds=0.3)


def _open_on(gtk, tmp_path, text, *, client_class=None):
    from sshpilot.advanced_config_window import AdvancedConfigWindow

    path = tmp_path / "config"
    path.write_text(text)
    client = (client_class or _Client)(path)
    win = AdvancedConfigWindow(None, client)
    win._test_client = client
    assert _pump(lambda: win._loaded)
    return win


def _close(win):
    win._closing = True
    win.destroy()


# -- opening, and what OpenSSH answers ---------------------------------------------

def test_it_opens_on_the_first_host_block_and_asks_about_it(gtk, tmp_path):
    win = _open_on(gtk, tmp_path, CONFIG)
    try:
        assert _block_rows(win) == ["Global", "Host web1 web2", "Host *"]
        assert _selected(win) == "web1 web2"            # one block is always shown
        assert win._typed_host() == "" and win.target_text() == "web1"
        assert _pump(lambda: _command(win) == "ssh -G web1")
        assert win.structured.patterns_row.get_text() == "web1 web2"
        assert win.structured.head_title.get_label() == "web1 web2"
    finally:
        _close(win)


def test_filtering_by_a_host_shows_openssh_s_answer(window):
    assert not window.dirty and not window.save_button.get_sensitive()
    glob, web, star = window.draft.blocks
    # Lines OpenSSH uses need no word; the ones it does not say so.
    assert [_status(window, o) for o in web.options] == [None, None, None]
    window.select_block(star.uid)
    assert [_status(window, o) for o in star.options] == ["Not used", "Overridden"]
    assert _effective(window, "user") == "deploy"
    assert _effective(window, "connecttimeout") == "7"      # Settings beat the file


def test_lines_are_listed_in_the_block_order(window):
    glob, web, star = window.draft.blocks
    rows = window.structured.options_list
    assert rows.get_row_at_index(0) is window.structured.header_row   # the Host line first
    order = [rows.get_row_at_index(index) for index in range(1, len(web.options) + 1)]
    shown = {row.row: uid for uid, row in window.structured._rows.items()}
    assert [shown[row] for row in order] == [o.uid for o in web.options]
    assert rows.get_row_at_index(len(web.options) + 1) is window.structured.add_option_row
    window.select_block(star.uid)                           # web1 is still the host
    assert _field(window, "port") is None                    # Host * sets none
    user = _field(window, "user")
    assert user.value.get_text() == "fallback"
    assert _pump(lambda: user.state.get_label() == "Not used")   # web's block set it first
    assert "Host web1 web2" in user.state._tooltip


def test_another_target_is_asked_again(window):
    _check(window, "db1")
    assert _pump(lambda: _effective(window, "user") == "fallback")
    web = window.draft.blocks[1]
    window.select_block(window.draft.blocks[2].uid)
    assert _pump(lambda: _command(window) == "ssh -G db1")
    assert _dimmed(window) == []               # web's block does not apply: listed alike
    window.target_entry.set_text("web1")       # a name the file names: checked, selected
    assert _pump(lambda: _effective(window, "user") == "deploy")
    assert window.selected_uid == web.uid and window._typed_host() == "web1"
    window.target_entry.set_text("")           # the search ends: what it found stays
    _pump(lambda: False, seconds=0.5)
    assert window.selected_uid == web.uid and window.target_text() == "web1"
    assert _effective(window, "user") == "deploy"


def test_moving_a_block_changes_what_wins_and_undo_restores_it(window):
    star = window.draft.blocks[2]
    assert window.move_block(star.uid, 1)
    assert _settled(window) and _effective(window, "user") == "fallback"
    assert window.dirty and window.save_button.get_sensitive()
    window.undo()
    assert _settled(window) and _effective(window, "user") == "deploy"
    assert not window.dirty
    window.redo()
    assert _settled(window) and _effective(window, "user") == "fallback"


def test_global_options_cannot_end_up_under_a_host(window):
    glob, web, _star = window.draft.blocks
    assert not window.move_block(web.uid, 0)
    assert not window.move_block(glob.uid, 2)
    assert [b.kind for b in window.draft.blocks] == ["global", "host", "host"]
    assert not window.sidebar._add_global.get_enabled()      # there is one already


def test_typing_a_value_re_evaluates_without_redrawing(window):
    entry = _field(window, "user").value
    entry.set_text("root")
    assert _pump(lambda: _effective(window, "user") == "root")
    assert _field(window, "user").value is entry and entry.get_text() == "root"
    assert "    User root\n" in window.editor.text


def test_a_rejected_line_is_marked_and_named(window):
    web = window.draft.blocks[1]
    window.add_option(web.uid, "Bogus", "1")
    assert _pump(lambda: window.banner.get_revealed())
    assert "line 9" in window.banner.get_title()
    assert _pump(lambda: _status(window, web.options[-1]) == "Rejected")
    window.delete_option(web.options[-1].uid)
    assert _pump(lambda: not window.banner.get_revealed())
    assert _effective(window, "user") == "deploy"


def test_a_new_match_exec_runs_only_after_consent(window, tmp_path):
    marker = tmp_path / "ran"
    window.add_block("match")
    block = window.draft.blocks[-1]
    assert window.selected_uid == block.uid
    window.structured.patterns_row.set_text(f'exec "touch {marker}"')
    assert _pump(lambda: window.banner.get_revealed())
    assert window.banner.get_button_label() and not marker.exists()
    window.banner.emit("button-clicked")
    assert _pump(marker.exists)
    # The answer that follows the consent, not the one that asked for it.
    assert _pump(lambda: window._evaluation is not None and window._evaluation.evaluated)
    assert _pump(lambda: window._evaluated_lines.headers[block.uid]
                 in window._evaluation.matched_blocks)          # it applies to web1
    assert not window.structured.error_row.get_visible()


def test_the_note_is_saved_cancelled_or_removed_and_the_title_follows(window):
    structured = window.structured
    web = window.draft.blocks[1]
    assert structured.head_title.get_label() == "web1 web2"
    structured.note_popover.popup()
    structured.note_row.set_text("typed, then cancelled")
    structured.note_popover.popdown()                    # Cancel: nothing written
    assert web.note == "" and not structured.note_label.get_visible()
    structured.note_popover.popup()
    assert structured.note_row.get_text() == ""          # what was cancelled is gone
    structured.note_row.set_text("Front ends")
    structured.note_row.emit("activate")                 # Enter saves
    assert window.draft.blocks[1].note == "Front ends"
    assert structured.note_label.get_label() == "# Front ends"
    structured.note_popover.popup()
    assert structured.remove_note_button.get_visible()
    structured.remove_note_button.emit("clicked")
    assert window.draft.blocks[1].note == "" and not structured.note_label.get_visible()
    structured.patterns_row.set_text("web1 web3")        # the title follows the typing
    assert structured.head_title.get_label() == "web1 web3"


def test_the_title_card_says_what_needs_attention_and_shows_it(gtk, tmp_path):
    win = _open_on(gtk, tmp_path, (
        "Host web1\n    User deploy\n    User other\n    ConnectTimeout 30\n"
    ))
    try:
        structured = win.structured
        assert structured.head_caption.get_label().startswith("Host")
        assert not structured.applies_box.get_visible()   # it names its host: no tile
        assert _pump(lambda: structured.problems_tile.get_visible())
        assert structured.problems_label.get_label() == (
            "1 directive will not take effect due to configuration precedence.\n"
            "1 directive is overridden by Settings ▸ SSH Options."
        )
        assert structured.problems_tile.has_css_class("advcfg-attn-error")   # not used: red
        win.show_page("raw")
        structured._show_first_problem()
        assert win.stack.get_visible_child_name() == "structured"
    finally:
        _close(win)


def test_escape_leaves_a_field_and_keeps_what_was_typed(window):
    from gi.repository import Gdk, Gtk

    entry = _field(window, "user").value
    keys = [c for c in _controllers(entry) if isinstance(c, Gtk.EventControllerKey)]
    entry.grab_focus()
    entry.set_text("someone-else")
    assert any(k.emit("key-pressed", Gdk.KEY_Escape, 0, 0) for k in keys)
    assert entry.get_text() == "someone-else"
    assert "User someone-else" in window.editor.text
    assert window.get_focus() is None


def _controllers(widget):
    found = widget.observe_controllers()
    return [found.get_item(index) for index in range(found.get_n_items())]


def test_disabling_a_block_writes_comments_and_openssh_skips_it(window):
    web = window.draft.blocks[1]
    window.structured.enabled_row.set_active(False)          # the switch
    assert _settled(window) and _effective(window, "user") == "fallback"
    assert "# [disabled] Host web1 web2\n" in window.editor.text
    assert window.structured.disabled_label.get_visible()
    assert window.structured.tiles.has_css_class("advcfg-dimmed")
    assert window.structured.options_group.has_css_class("advcfg-dimmed")
    assert window.draft.blocks[1].uid == web.uid == window.selected_uid


def test_raw_edits_are_applied_to_the_blocks(window):
    buffer = _raw(window)
    assert buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False) == CONFIG
    buffer.set_text("Host web1\n    User typed\n")
    assert window.raw.apply_button.get_sensitive()
    window.apply_raw()
    assert _settled(window) and _effective(window, "user") == "typed"
    assert [b.patterns for b in window.draft.blocks] == ["web1"]
    assert _selected(window) == "web1"                      # the draft read anew: one is shown


def test_save_writes_only_what_changed_and_clears_dirty(window):
    client = window._test_client
    _field(window, "user").value.set_text("root")
    assert _pump(lambda: window.dirty)
    window.save()
    assert _pump(lambda: client.saves == 1 and not window.dirty)
    assert client.path.read_text() == CONFIG.replace("User deploy", "User root")


def test_a_file_changed_on_disk_is_not_overwritten(window):
    client = window._test_client
    client.path.write_text(CONFIG + "# edited elsewhere\n")
    _field(window, "user").value.set_text("root")
    assert _pump(lambda: window.dirty)
    window.save()
    assert _pump(lambda: (
        window.file_banner.get_revealed() and bool(window.file_banner.get_button_label())
    ))
    assert client.saves == 0 and "edited elsewhere" in client.path.read_text()


# -- the lines of the Structured view ------------------------------------------------

def test_a_common_option_is_added_at_the_end_and_edited_in_its_row(window):
    web = window.draft.blocks[1]
    window.structured._add_common("hostname")
    assert web.options[-1].key == "HostName"
    hostname = _field(window, "hostname")
    assert hostname.uid == web.options[-1].uid
    assert hostname.key.get_text() == "HostName"
    hostname.value.set_text("10.0.0.5")
    assert "    HostName 10.0.0.5\n" in window.editor.text
    assert _pump(lambda: _effective(window, "hostname") == "10.0.0.5")
    hostname.value.set_text("")                              # emptied: the row stays
    assert _field(window, "hostname") is hostname and "HostName" in window.editor.text
    window.delete_option(hostname.uid)
    assert "HostName" not in window.editor.text and _field(window, "hostname") is None
    window.delete_option(_field(window, "port").uid)
    assert "Port 2222" not in window.editor.text
    window.undo()
    assert "Port 2222" in window.editor.text
    assert _field(window, "port").value.get_text() == "2222"
    assert [o.key for o in web.options][:3] == ["User", "Port", "IdentityFile"]


def test_every_line_is_a_keyword_and_a_value(gtk, tmp_path):
    """No line gets a control of its own: ForwardAgent with a socket, a
    second User, an IdentityFile and a forward are rows like any other."""
    win = _open_on(gtk, tmp_path, (
        "Host a\n    User one\n    ForwardAgent $SSH_AUTH_SOCK\n    User two\n"
        "    IdentityFile ~/.ssh/id\n    DynamicForward 1080\n"
    ))
    try:
        block = win.draft.blocks[0]
        rows = win.structured.line_rows
        assert list(rows) == [option.uid for option in block.options]
        assert [(rows[o.uid].key.get_text(), rows[o.uid].value.get_text()) for o in block.options] == [
            ("User", "one"), ("ForwardAgent", "$SSH_AUTH_SOCK"), ("User", "two"),
            ("IdentityFile", "~/.ssh/id"), ("DynamicForward", "1080"),
        ]
    finally:
        _close(win)


def test_lines_move_up_and_down_and_undo_restores_them(window):
    web = window.draft.blocks[1]
    keys = lambda: [o.key for o in web.options]
    rows = lambda: [window.structured.line_rows[o.uid] for o in web.options]
    assert keys() == ["User", "Port", "IdentityFile"]
    assert not rows()[0].up.get_sensitive() and not rows()[-1].down.get_sensitive()
    rows()[0].down.emit("clicked")
    assert keys() == ["Port", "User", "IdentityFile"]
    rows()[2].up.emit("clicked")
    assert keys() == ["Port", "IdentityFile", "User"]
    window.undo()
    assert keys() == ["Port", "User", "IdentityFile"]


def test_a_line_dropped_by_its_grip_lands_where_the_gap_opened(window):
    from sshpilot.advanced_config_widgets import LineDragItem

    web = window.draft.blocks[1]
    structured = window.structured
    first, last = web.options[0], web.options[-1]
    structured.line_drag_began(last.uid)
    found, bounds = structured.line_rows[first.uid].row.compute_bounds(structured.options_list)
    assert found
    top = bounds.get_y() + 3
    assert structured._on_line_motion(None, 0, top)
    assert structured._gap.get_parent() is structured.options_list
    assert structured._on_line_drop(None, LineDragItem(last.uid), 0, top)
    assert [o.key for o in web.options] == ["IdentityFile", "User", "Port"]
    assert structured._gap.get_parent() is None


def test_key_lines_have_browse_and_say_what_the_file_on_disk_is(window, tmp_path):
    import os

    web = window.draft.blocks[1]
    key = tmp_path / "id_test"
    key.write_text("key")
    os.chmod(key, 0o644)
    identity = _field(window, "identityfile")
    user = _field(window, "user")
    assert identity.browse.get_visible() and not user.browse.get_visible()
    identity.value.set_text("~/.ssh/missing")
    assert identity.key_line.get_visible() and identity.key_status.get_label() == "File not found"
    assert not identity.fix.get_visible()
    identity.value.set_text(str(key))
    assert "0644" in identity.key_status.get_label() and identity.fix.get_visible()
    identity.fix.emit("clicked")
    assert os.stat(key).st_mode & 0o777 == 0o600
    assert not identity.key_line.get_visible()
    assert f"    IdentityFile {key}\n" in window.editor.text
    assert web.options[2].value == str(key)


def test_keyword_suggestions_are_chosen_with_the_keyboard(window):
    from gi.repository import Gdk, Gtk

    web = window.draft.blocks[1]
    window.structured.add_option_row.emit("activated")
    row = window.structured.line_rows[web.options[-1].uid]
    row.key.grab_focus()
    row.key.set_text("Pass")
    row.suggestions.typed()
    assert row.suggestions.visible()
    keys = [c for c in _controllers(row.key) if isinstance(c, Gtk.EventControllerKey)
            and c.get_propagation_phase() == Gtk.PropagationPhase.CAPTURE]
    assert keys                                         # ahead of the text widget
    press = lambda keyval: any(k.emit("key-pressed", keyval, 0, 0) for k in keys)
    assert press(Gdk.KEY_Down) and press(Gdk.KEY_Down) and press(Gdk.KEY_Up)
    assert press(Gdk.KEY_Return)
    assert row.key.get_text() == "PasswordAuthentication" and not row.suggestions.visible()


def test_the_common_options_menu_is_a_menu_model_that_adds_lines(window):
    from gi.repository import GLib

    web = window.draft.blocks[1]
    window.structured.activate_action("directives.add-common", GLib.Variant.new_string("proxyjump"))
    assert web.options[-1].key == "ProxyJump"


def test_a_common_yes_no_option_is_added_as_yes(window):
    window.structured._add_common("identitiesonly")
    assert "    IdentitiesOnly yes\n" in window.editor.text


def test_an_empty_value_shows_what_the_host_gets_from_elsewhere(window):
    star = window.draft.blocks[2]
    window.select_block(star.uid)                            # web1 stays in question
    window.add_option(star.uid, "Port", "")
    row = window.structured.line_rows[star.options[-1].uid]
    assert _pump(lambda: row.value.get_placeholder_text() == "2222")   # from web's block


# -- the Effective view ---------------------------------------------------------------

def test_the_effective_view_counts_where_values_come_from(window):
    window.show_page("effective")
    effective = window.effective
    assert _pump(lambda: effective._chips["all"].get_label().startswith("All "))
    counts = {kind: int(chip.get_label().rsplit(" ", 1)[1])
              for kind, chip in effective._chips.items()}
    assert counts["all"] == counts["this"] + counts["other"] + counts["default"]
    assert counts["this"] >= 2 and counts["other"] >= 2      # web's lines, Compression, Settings
    effective.select_filter("this")
    assert {item.key for item in effective.items()} >= {"user", "port"}
    assert all(item.kind == "this" for item in effective.items())
    effective.select_filter("default")
    assert "user" not in {item.key for item in effective.items()}
    effective.select_filter("all")
    effective.search.set_text("connecttimeout")
    assert _pump(lambda: [item.key for item in effective.items()] == ["connecttimeout"])


def test_effective_is_hidden_for_a_block_that_names_no_host(window):
    glob, web, star = window.draft.blocks
    page = window.stack.get_page(window.effective)
    window.show_page("effective")
    _check(window, "")                                # the selection says
    window.select_block(star.uid)                     # Host *: no host to ask about
    assert _pump(lambda: not page.get_visible())
    assert window.stack.get_visible_child_name() == "structured"
    window.show_page("effective")                     # not offered: not shown
    assert window.stack.get_visible_child_name() == "structured"
    _check(window, "web1")                            # a host in question: offered again
    assert _pump(page.get_visible)
    assert window.stack.get_visible_child_name() == "effective"   # where it was left
    _check(window, "")
    assert _pump(lambda: not page.get_visible())
    window.show_page("raw")                           # another page picked meanwhile
    window.select_block(web.uid)
    assert _pump(page.get_visible)
    assert window.stack.get_visible_child_name() == "raw"


def test_effective_values_are_not_dimmed_and_problems_are_marked(window):
    window.show_page("effective")
    effective = window.effective
    assert _pump(lambda: effective._chips["all"].get_label().startswith("All "))
    effective.select_filter("default")
    assert _pump(lambda: bool(effective.items()))
    item = effective.items()[0]
    if item.cell is not None:                        # value and source alike
        assert "dim-label" not in item.cell.value.get_css_classes()
        assert "dim-label" not in item.cell.label.get_css_classes()
        assert item.cell.source.get_sensitive()      # inert, not greyed out
        assert not item.cell.source.get_can_target()
    web = window.draft.blocks[1]
    window.option_typed(web.options[2].uid, "value", "~/.ssh/missing")
    assert _settled(window)
    effective.fill()
    effective.select_filter("this")
    (key,) = [i for i in effective.items() if i.key == "identityfile"]
    assert key.problems and "File not found" in key.problems[-1]


def test_effective_marks_what_needs_attention_by_severity(gtk, tmp_path):
    """Bold: set by the block on screen. Red: a key file OpenSSH will not
    use, or lines an earlier line beats; purple: lines Settings beat.
    A Needs attention filter lists just those rows."""
    win = _open_on(gtk, tmp_path, (
        "Host web1\n    User deploy\n    IdentityFile ~/.ssh/nope_key\n    User other\n"
        "    ConnectTimeout 30\n    Port 2222\n\nHost *\n    User fallback\n"
    ))
    try:
        _pick(win, "Host web1")
        win.show_page("effective")
        effective = win.effective
        assert _pump(lambda: effective._chips["attention"].get_visible())
        items = effective._items
        assert items["identityfile"].severity == "error"
        assert items["identityfile"].kind == "this"           # its line, though collected
        assert items["identityfile"].source.startswith("line 3")
        assert items["user"].severity == "ignored"
        assert items["connecttimeout"].severity == "overridden"
        assert items["port"].severity == "" and items["port"].kind == "this"
        effective.select_filter("attention")
        assert _pump(lambda: sorted(i.key for i in effective.items())
                     == ["connecttimeout", "identityfile", "user"])
        port = items["port"]
        effective.select_filter("all")
        assert _pump(lambda: port.cell is not None)
        assert "advcfg-explicit" in port.cell.key.get_css_classes()
        user = items["user"]
        assert _pump(lambda: user.cell is not None)
        assert "advcfg-attn-error" in user.cell.outer.get_css_classes()   # not used: red
    finally:
        _close(win)


def test_an_unused_line_says_what_is_used_instead(gtk, tmp_path):
    win = _open_on(gtk, tmp_path, (
        "Host zoo\n    User early\n\nHost zoo\n    HostName 10.0.0.5\n"
        "    HostName google.com\n    User late\n    ConnectTimeout 30\n"
    ))
    try:
        win.select_block(win.draft.blocks[1].uid)
        assert _settled(win)
        structured = win.structured
        user = _field(win, "user")
        assert _pump(lambda: user.instead.get_label()
                     == "OpenSSH uses <tt>early</tt> from line 2 (Host zoo)")
        rows = list(structured.line_rows.values())
        second_hostname = [r for r in rows if r.key.get_text() == "HostName"][1]
        assert second_hostname.instead.get_label() == "OpenSSH uses <tt>10.0.0.5</tt> from line 5 (Host zoo)"
        timeout = next(r for r in rows if r.key.get_text() == "ConnectTimeout")
        assert timeout.instead.get_label() == "Settings ▸ SSH Options use <tt>7</tt>"
        hostname = _field(win, "hostname")                  # used: nothing said
        assert not hostname.instead.get_visible()
    finally:
        _close(win)


def test_lines_that_do_not_take_effect_are_struck_through_and_rejected_ones_red(gtk, tmp_path):
    win = _open_on(gtk, tmp_path, "Host zoo\n    User early\n    User late\n    Port 22\n")
    try:
        block = win.draft.blocks[0]
        rows = [win.structured.line_rows[o.uid] for o in block.options]
        assert _pump(lambda: rows[1].state.get_label() == "Not used")
        assert rows[1].value._struck                                  # struck through
        assert not rows[1].value.has_css_class("dim-label")          # not dimmed
        assert not getattr(rows[0].value, "_struck", False)          # used: plain
        rows[2].value.set_text("abc")                                # OpenSSH rejects it
        assert _pump(lambda: rows[2].state.get_label() == "Rejected")
        assert rows[2].value.has_css_class("error") and rows[2].state.has_css_class("error")
    finally:
        _close(win)


def test_a_not_used_pill_shows_the_line_used_instead(gtk, tmp_path):
    """Structured's "Not used" pill is a link, as a source is in Effective."""
    from gi.repository import Gtk
    win = _open_on(gtk, tmp_path, (
        "Host db1\n    User postgres\n    Port 5432\n\nHost *\n    User fallback\n"
    ))
    try:
        db, star = win.draft.blocks
        _check(win, "db1")
        win.select_block(star.uid)                           # db1 stays in question
        assert _settled(win)
        user = _field(win, "user")
        assert _pump(lambda: user.state.get_label() == "Not used")
        assert user.state._target == ("", 2)                 # db1's User line
        controllers = user.state.observe_controllers()
        click = next(controllers.get_item(i) for i in range(controllers.get_n_items())
                     if isinstance(controllers.get_item(i), Gtk.GestureClick))
        click.emit("released", 1, 5.0, 5.0)
        assert _pump(lambda: win.selected_uid == db.uid)
        assert win.target_text() == "db1"                    # the host checked stays
    finally:
        _close(win)


def test_effective_sums_up_the_connection(gtk, tmp_path):
    """The header: a tile each for the address, user, port, route and key,
    flagged like their rows."""
    win = _open_on(gtk, tmp_path, (
        "Host db1\n    HostName 10.20.4.15\n    User postgres\n    Port 5432\n"
        "    Port 6543\n    ProxyJump bastion\n\nHost *\n    User fallback\n"
    ))
    try:
        _pick(win, "Host db1")
        win.show_page("effective")
        effective = win.effective
        tiles = effective._tiles
        assert _pump(lambda: tiles["host"].value.get_label() == "10.20.4.15")
        assert tiles["host"].value.get_label() == "10.20.4.15"
        assert tiles["host"].caption.get_label() == "alias db1"
        assert tiles["user"].value.get_label() == "postgres"
        assert tiles["port"].value.get_label() == "5432"
        assert "bastion" in tiles["route"].value.get_label()
        # db1's second Port line is not used: the port is flagged, not the
        # user, whose Host * fallback losing to db1 is how fallbacks work.
        assert "advcfg-attn-error" in tiles["port"].box.get_css_classes()
        assert tiles["port"].dot.get_visible() and not tiles["user"].dot.get_visible()
        assert not any(c.startswith("advcfg-attn") for c in tiles["user"].box.get_css_classes())
        effective.select_filter("this")
        effective.search.set_text("zzz")
        tiles["port"].child.activate()                 # shown in the list, filters lifted
        assert effective._filter_kind == "all" and not effective.search.get_text()
    finally:
        _close(win)


def test_effective_column_titles_sit_over_their_columns(window):
    window.show_page("effective")
    effective = window.effective
    assert _pump(lambda: any(item.cell for item in effective._items.values()))
    _pump(lambda: False, seconds=0.3)                  # laid out
    header = effective.table.get_first_child().get_first_child()
    titles = []
    child = header.get_first_child()
    while child is not None:
        titles.append(child)
        child = child.get_next_sibling()
    cell = next(item.cell for item in effective._items.values() if item.cell)

    def x(widget):
        found, bounds = widget.compute_bounds(effective.table)
        assert found
        return round(bounds.get_x())

    assert [x(w) for w in titles] == [x(w) for w in (cell.warning, cell.key, cell.value, cell.source)]
    assert x(titles[-1].get_visible_child().get_child()) == x(cell.label)


def test_only_the_view_on_screen_is_filled(window):
    window.show_page("structured")
    _field(window, "user").value.set_text("other")
    assert _settled(window)
    assert window.effective._stale and window.raw._stale
    window.show_page("effective")
    assert not window.effective._stale and window.raw._stale
    window.show_page("raw")
    assert not window.raw._stale


# -- the sidebar ------------------------------------------------------------------------

def test_the_sidebar_lists_the_blocks_and_a_host_in_question_stays(window):
    """The fixture keeps web1 in question: it is checked whichever block
    the window selects, until a block is picked in the sidebar."""
    glob, web, star = window.draft.blocks
    window.add_block("host")
    db = window.draft.blocks[-1]
    window.structured.patterns_row.set_text("db1 *.internal")
    assert _block_rows(window) == [
        "Global", "Host web1 web2", "Host *", "Host db1 *.internal",
    ]
    assert window.sidebar.list.get_selected_row()._uid == db.uid   # the new block
    assert _settled(window)
    asked = window._request_serial

    window.select_block(star.uid)
    assert window.selected_uid == star.uid
    window.select_block(web.uid)
    assert window.sidebar.list.get_selected_row()._uid == web.uid
    _pump(lambda: False, seconds=0.4)
    assert window._request_serial == asked            # nothing new was asked
    assert window._typed_host() == "web1" and window.target_text() == "web1"

    _check(window, "")                                # unpinned: the selection says
    assert window.target_text() == "web1"             # web's block is selected
    _pick(window, "Host db1 *.internal")
    assert window.target_text() == "db1"              # the first plain name
    assert _pump(lambda: _effective(window, "user") == "fallback")

    window.delete_block(db.uid)                       # a selected block that goes:
    assert _block_rows(window) == ["Global", "Host web1 web2", "Host *"]
    assert window.selected_uid == star.uid            # the one at its place is shown


def test_the_block_list_names_each_block_and_says_what_it_is(gtk, tmp_path):
    """Rows like the main sidebar's: an icon, the name, and a caption with
    the block's own user@address, or else the kind of block."""
    win = _open_on(gtk, tmp_path, (
        "Compression yes\n\nHost web1\n    User a\n\nHost db1 db2\n    User b\n\n"
        "Match host *.corp\n    ProxyJump gate\n\nHost edge *.edge\n    Port 2222\n\n"
        "Host app\n    HostName 10.0.0.9\n    User ops\n\n"
        "# [disabled] Host *.old\n# [disabled]     User gone\n\nHost *\n    User z\n"
    ))
    try:
        assert _pump(lambda: len(win.sidebar._rows) == 8)
        rows = [win.sidebar._rows[block.uid] for block in win.editor.blocks]
        assert [(r.title.get_label(), r.caption.get_label()) for r in rows] == [
            ("Global Options", "All hosts"),
            ("web1", "Host"),
            ("db1 db2", "2 hosts"),
            ("Match host *.corp", "Match rule"),
            ("edge *.edge", "Host pattern"),
            ("app", "ops@10.0.0.9"),
            ("*.old", "Host pattern · off"),
            ("Host *", "Wildcard Host block"),
        ]
        icons = [r.icon.get_icon_name() for r in rows]
        assert icons[1] == icons[2] and len(set(icons)) == 4     # one per kind
        # Relabelled in place when the line changes.
        web1 = win.editor.blocks[1]
        win.select_block(web1.uid)
        win.structured.patterns_row.set_text("web*")
        assert rows[1].caption.get_label() == "Host pattern"
        assert win.sidebar._rows[web1.uid] is rows[1]
    finally:
        _close(win)


def _shown_rows(win):
    """The rows the search leaves on screen, as they read."""
    win.sidebar.refilter()
    return [row._text for row in _rows(win) if row.get_child_visible()]


def test_the_search_finds_blocks_and_an_exact_name_is_checked(gtk, tmp_path):
    """Typing finds blocks by name or address and selects the best of them.
    The exact name of a host the file names is the host checked; anything
    else is no host at all."""
    win = _open_on(gtk, tmp_path, (
        "Host web1 web2\n    HostName 10.0.0.5\n    User deploy\n\n"
        "Host db1\n    HostName db.internal\n    ProxyJump web1\n\n"
        "Host *.prod\n    User ops\n\nHost *\n    User fallback\n"
    ))
    try:
        _show(win)
        web, db, prod, star = win.editor.blocks
        assert win.target_entry.get_placeholder_text() == "Search blocks"

        def typed(text):
            win.target_entry.set_text(text)
            _pump(lambda: False, seconds=0.5)

        typed("db")                                   # by name
        assert _shown_rows(win) == ["Host db1"]
        assert win.selected_uid == db.uid
        assert _pump(lambda: _command(win) == "ssh -G db1")
        typed("10.0.0")                               # by address (HostName)
        assert _shown_rows(win) == ["Host web1 web2"] and win.selected_uid == web.uid
        typed("deploy")                               # not by any other directive
        assert _shown_rows(win) == []
        typed("web2")                                 # the second name of a line
        assert win.selected_uid == web.uid and win._typed_host() == "web2"
        assert _pump(lambda: _command(win) == "ssh -G web2")
        typed("nowhere")                              # no block has it: no host
        assert _shown_rows(win) == []
        assert win.selected_uid == web.uid and win._typed_host() == ""
        assert win.target_text() == "web1"
        typed("")                                     # the search ends: every block
        assert _shown_rows(win) == ["Host web1 web2", "Host db1", "Host *.prod", "Host *"]
        assert win.selected_uid == web.uid
    finally:
        _close(win)


def test_a_block_is_dragged_in_the_sidebar_not_as_text(window):
    """Review: a text payload dropped on a field typed itself into it. The
    sidebar's drag carries a type of its own, and a drop lands beside the
    row the line is drawn on."""
    from gi.repository import Gtk

    from sshpilot.advanced_config_widgets import DragItem

    _show(window)
    sidebar = window.sidebar
    targets = [c for c in sidebar.list.observe_controllers() if isinstance(c, Gtk.DropTarget)]
    assert [list(t.get_formats().get_gtypes()) for t in targets] == [[DragItem.__gtype__]]
    glob, web, star = window.draft.blocks
    assert not any(isinstance(c, Gtk.DragSource) for c in
                   sidebar._rows[glob.uid].observe_controllers()) or not sidebar._rows[glob.uid]._movable
    assert sidebar._on_drag_prepare(None, 0, 0, sidebar._rows[glob.uid]) is None

    row = sidebar._rows[web.uid]
    found, bounds = row.compute_bounds(sidebar.list)
    assert found
    top = bounds.get_y() + 3
    sidebar._dragging = star.uid
    assert sidebar._on_drag_motion(None, 0, top)
    gap = sidebar._placeholder                              # where it would land:
    assert gap.get_parent() is sidebar.list                  # a gap opens above web
    assert gap.get_index() == row.get_index() - 1
    assert sidebar._on_drop(None, DragItem(star.uid), 0, top)
    assert [b.patterns for b in window.draft.blocks] == ["", "*", "web1 web2"]
    assert gap.get_parent() is None
    # Above the global options: refused, and said why.
    found, bounds = sidebar._rows[glob.uid].compute_bounds(sidebar.list)
    assert not sidebar._on_drop(None, DragItem(star.uid), 0, bounds.get_y() + 3)
    assert [b.patterns for b in window.draft.blocks] == ["", "*", "web1 web2"]


def test_picking_a_block_drops_a_host_the_window_put_in_question(window):
    """A source followed from Effective (or a host searched for) puts that
    host in question; a block picked in the sidebar afterwards is shown for
    its own host, so Effective follows the selection."""
    _check(window, "")
    glob, web, star = window.draft.blocks
    window.add_block("host")
    db = window.draft.blocks[-1]
    window.structured.patterns_row.set_text("db1")
    assert _settled(window)
    window.show_page("effective")
    assert _pump(lambda: window.effective.command.get_label() == "ssh -G db1")
    window.reveal_line(window._evaluated_lines.headers[db.uid])     # a source clicked
    assert window._typed_host() == "db1"
    window.show_page("effective")
    _pick(window, "Host web1 web2")
    assert window._typed_host() == "" and window.target_text() == "web1"
    assert _pump(lambda: window.effective.command.get_label() == "ssh -G web1")
    assert _pump(lambda: _effective(window, "user") == "deploy")


def test_clicking_a_block_selects_it_and_alt_arrows_move_it(window):
    glob, web, star = window.draft.blocks
    _pick(window, "Host *")
    assert window.selected_uid == star.uid and window.structured.uid == star.uid
    _pick(window, "Host web1 web2")
    window.move_selected(1)                        # Alt+Down
    assert [b.patterns for b in window.draft.blocks] == ["", "*", "web1 web2"]
    assert window.selected_uid == web.uid          # still selected after the redraw
    assert window.sidebar.list.get_selected_row()._uid == web.uid
    window.move_selected(-1)                       # Alt+Up
    window.move_selected(-1)                       # not above the global options
    assert [b.patterns for b in window.draft.blocks] == ["", "web1 web2", "*"]


# -- the host being checked -------------------------------------------------------------

def test_a_source_s_line_is_shown_without_losing_the_host(gtk, tmp_path):
    """Going from the Effective view to the line behind a value selects the
    line's block: the host being read about is typed in, so it stays."""
    win = _open_on(gtk, tmp_path, CONFIG)
    try:
        glob, web, star = win.draft.blocks
        assert _pump(lambda: _command(win) == "ssh -G web1" and win._evaluation is not None)
        win.show_page("effective")
        win.reveal_line(win._evaluated_lines.headers[star.uid])
        assert win.selected_uid == star.uid
        assert win.stack.get_visible_child_name() == "structured"
        assert win._typed_host() == "web1" and win.target_text() == "web1"
        _pump(lambda: False, seconds=0.4)
        assert _command(win) == "ssh -G web1"
    finally:
        _close(win)


def test_picking_a_pattern_block_marks_the_blocks_related_to_it(gtk, tmp_path):
    """A pattern or a Match rule names no host. Picking one says which
    listed hosts it applies to, each host being OpenSSH's answer; the
    sidebar lists every block alike."""
    win = _open_on(gtk, tmp_path, (
        "Host a.prod\n    User a\n\nHost b.prod c.test\n    User b\n\n"
        "Host d.test\n    User d\n\nHost *.prod\n    Port 2200\n\n"
        "Match host *.test\n    Port 2300\n\nHost *.lab\n    Port 2400\n\n"
        "Host *\n    Compression yes\n"
    ))
    try:
        a, b, d, prod, test, lab, star = win.draft.blocks
        structured = win.structured

        _pick(win, "Host *.prod")
        assert _pump(lambda: _applies(win) == ["a.prod", "b.prod"])
        assert win._evaluation is None and _dimmed(win) == []
        assert _field(win, "port").value.get_text() == "2200"

        _pick(win, "Match host *.test")                      # a rule, as a pattern
        assert _pump(lambda: _applies(win) == ["c.test", "d.test"])

        _pick(win, "Host *.lab")                             # no listed host fits it:
        assert _pump(lambda: _applies(win) == [])

        _pick(win, "Host *.prod")                            # a chip shows that host
        assert _pump(lambda: _applies(win) == ["a.prod", "b.prod"])
        structured.applies_chips.get_child_at_index(1).get_child().emit("clicked")
        assert _pump(lambda: win.selected_uid == b.uid)

        _pick(win, "Host a.prod")                            # a host: the usual answer
        assert _pump(lambda: _command(win) == "ssh -G a.prod")
        assert _applies(win) is None                         # nothing worth saying
        assert _dimmed(win) == []
    finally:
        _close(win)


def test_retyping_the_picked_block_s_host_follows_it(gtk, tmp_path):
    win = _open_on(gtk, tmp_path, CONFIG)
    try:
        web = win.draft.blocks[1]
        assert _pump(lambda: _command(win) == "ssh -G web1")
        win.structured.patterns_row.set_text("edge9 web2")
        assert _block_rows(win) == ["Global", "Host edge9 web2", "Host *"]
        assert win.sidebar.list.get_selected_row()._uid == web.uid
        assert _pump(lambda: _command(win) == "ssh -G edge9")
        assert win._typed_host() == "" and win.structured.head_title.get_label() == "edge9 web2"
    finally:
        _close(win)


# -- review findings ---------------------------------------------------------------------

class _ReadOnlyClient(_Client):
    def get_ssh_config_text(self, file=""):
        from sshpilot.api.models.connections import SshConfigText

        return SshConfigText(self.path.read_text(), self._revision(), "config", False)


def test_a_file_notice_does_not_hide_what_openssh_says(gtk, tmp_path):
    """Review: a read-only notice suppressed every later banner, the Match
    exec prompt included, so such a draft could never be evaluated."""
    marker = tmp_path / "ran"
    win = _open_on(gtk, tmp_path, CONFIG, client_class=_ReadOnlyClient)
    try:
        assert win.file_banner.get_revealed() and not win.save_button.get_sensitive()
        _check(win, "web1")
        win.add_block("match")
        win.structured.patterns_row.set_text(f'exec "touch {marker}"')
        assert _pump(lambda: win.banner.get_revealed() and bool(win.banner.get_button_label()))
        assert win.file_banner.get_revealed()               # both are up
        win.banner.emit("button-clicked")
        assert _pump(marker.exists)
    finally:
        _close(win)


def test_a_failed_save_notice_goes_with_the_next_good_save(window):
    client = window._test_client
    real = client.save_ssh_config_text
    client.save_ssh_config_text = lambda request: (_ for _ in ()).throw(OSError("disk"))
    _field(window, "user").value.set_text("root")
    assert _pump(lambda: window.dirty)
    window.save()
    assert _pump(lambda: window.file_banner.get_revealed())
    assert window.file_banner.get_button_label()         # it can be dismissed
    client.save_ssh_config_text = real
    window.save()
    assert _pump(lambda: client.saves == 1 and not window.file_banner.get_revealed())


def test_an_answer_for_a_host_no_longer_asked_about_is_dropped(window):
    """Review: clearing the host did not outdate the question in flight, so
    its answer came back and filled the views again."""
    import threading

    client = window._test_client
    gate = threading.Event()
    real = client.evaluate_ssh_config_draft

    def slow(request):
        if request.host == "slowhost":
            gate.wait(5)
        return real(request)

    client.evaluate_ssh_config_draft = slow
    window.select_block(window.draft.blocks[0].uid)      # the global options: no host
    _check(window, "slowhost")
    assert _pump(lambda: window._evaluate_source == 0, seconds=3)
    _check(window, "")
    assert _pump(lambda: window._evaluation is None)
    gate.set()
    _pump(lambda: False, seconds=0.6)
    assert window._evaluation is None


def test_the_blocks_wait_while_text_is_being_typed(window):
    """Review: with text pending, edits were made beside it and then thrown
    away by Save, which wrote the older text."""
    _raw(window).set_text(CONFIG.replace("User deploy", "User typed"))
    assert window.raw_pending and not window.structured.get_sensitive()
    assert not window.sidebar.list.get_sensitive()
    assert window.raw.revert_button.get_sensitive()
    assert "Not applied" in window.raw.footer.get_label()
    window.move_selected(1)                               # keys are held back too
    assert [b.patterns for b in window.draft.blocks] == ["", "web1 web2", "*"]
    window.discard_raw()
    assert not window.raw_pending and window.structured.get_sensitive() and not window.dirty
    buffer = window.raw.view.get_buffer()
    assert buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False) == CONFIG


def test_save_asks_openssh_first_however_fast_it_is_pressed(window):
    """Review: the warning was skipped when the draft had not been evaluated
    yet (within the typing delay, or with no host in question)."""
    client = window._test_client
    web = window.draft.blocks[1]
    window.select_block(window.draft.blocks[0].uid)        # no host: nothing evaluated
    _check(window, "")
    assert _pump(lambda: window._evaluation is None)
    window.option_typed(web.options[1].uid, "value", "not-a-port")
    window.save()                                         # at once
    assert _pump(lambda: getattr(window, "_save_alert", None) is not None)
    assert client.saves == 0
    alert = window._save_alert
    _pump(lambda: False, seconds=0.25)
    alert.emit("response", "cancel")
    alert.force_close()
    assert _pump(lambda: not window._saving)
    assert client.saves == 0 and window.dirty and window.save_button.get_sensitive()


def test_a_configuration_openssh_rejects_is_not_saved_and_its_line_is_shown(window):
    client = window._test_client
    web = window.draft.blocks[1]
    window.option_typed(web.options[1].uid, "value", "not-a-port")
    window.save()
    assert _pump(lambda: getattr(window, "_save_alert", None) is not None)
    alert = window._save_alert
    assert not alert.has_response("save")              # no Save Anyway
    assert alert.has_response("show")
    _pump(lambda: False, seconds=0.25)
    alert.emit("response", "show")
    alert.force_close()
    assert _pump(lambda: not window._saving)
    assert client.saves == 0 and window.dirty
    assert window.selected_uid == web.uid and window.stack.get_visible_child_name() == "structured"


def test_nothing_is_saved_when_openssh_cannot_be_asked(window):
    client = window._test_client
    web = window.draft.blocks[1]
    window.option_typed(web.options[1].uid, "value", "2200")

    def unreachable(_request):
        raise RuntimeError("no background service")

    client.evaluate_ssh_config_draft = unreachable
    window._answers, window._answers_key = {}, None
    window.save()
    assert _pump(lambda: not window._saving)
    assert client.saves == 0 and window.dirty


def test_a_missing_key_file_is_asked_about_before_saving(window):
    client = window._test_client
    web = window.draft.blocks[1]
    window.option_typed(web.options[2].uid, "value", "~/.ssh/missing")
    window.save()
    assert _pump(lambda: getattr(window, "_save_alert", None) is not None)
    alert = window._save_alert
    assert alert.has_response("save") and "File not found" in alert.get_body()
    _pump(lambda: False, seconds=0.25)
    alert.emit("response", "save")
    alert.force_close()
    assert _pump(lambda: client.saves == 1)


def test_a_configuration_openssh_already_rejects_is_said_so_at_open(gtk, tmp_path):
    win = _open_on(gtk, tmp_path, "Host a\n    Bogus yes\n")
    try:
        assert _pump(lambda: win.banner.get_revealed())
        assert "line 2" in win.banner.get_title()
    finally:
        _close(win)


def test_an_edit_redraws_in_place(window):
    """The widgets of what did not change are the same widgets after an
    edit: focus and typed text survive."""
    web, star = window.draft.blocks[1], window.draft.blocks[2]
    user, row = _field(window, "user").value, window.sidebar._rows[star.uid]
    window.add_option(web.uid, "Compression", "no")
    other = window.structured.line_rows[web.options[-1].uid]
    window.move_block(star.uid, 1)
    window.undo()
    assert _field(window, "user").value is user and window.sidebar._rows[star.uid] is row
    assert window.structured.line_rows[web.options[-1].uid] is other
    assert [o.key for o in web.options] == ["User", "Port", "IdentityFile", "Compression"]


# -- going to a place: the entry point the rest of the app uses ------------------------

def test_going_to_a_host_before_the_window_has_loaded(gtk, tmp_path):
    from sshpilot.advanced_config_window import AdvancedConfigWindow

    path = tmp_path / "config"
    path.write_text(CONFIG + "\nHost *.prod edge\n    User ops\n")
    win = AdvancedConfigWindow(None, _Client(path))
    try:
        win.go_to(host="edge", option="user", pin=True)        # nothing is loaded yet
        assert _pump(lambda: _selected(win) == "*.prod edge")   # a name beside a pattern
        assert win._typed_host() == "edge"                      # pinned
        assert _pump(lambda: _command(win) == "ssh -G edge")
        win.go_to(host="web2")                                  # the second name of a line
        assert _selected(win) == "web1 web2"
        win.go_to(host="nowhere")                               # says so, stays put
        assert _selected(win) == "web1 web2"
    finally:
        _close(win)


def test_a_line_of_the_saved_file_is_found_after_unsaved_edits(window):
    """Line numbers are of the file as saved: an edit above the line moves
    it in the draft, and it is still the block that is shown."""
    glob, web, star = window.draft.blocks
    saved_line = window._loaded_lines.headers[star.uid]
    window.add_option(web.uid)
    window.add_option(web.uid)                   # Host * is two lines lower now
    window.select_block(glob.uid)
    window.go_to(line=saved_line)
    assert _selected(window) == "*"
    window.go_to(line=window._loaded_lines.options[web.options[1].uid])   # an option's line
    assert _selected(window) == "web1 web2"


def test_a_line_no_block_owns_is_shown_as_text(window):
    window.go_to(line=1)                                    # "# my hosts": a comment
    assert window.stack.get_visible_child_name() == "raw"
    buffer = window.raw.view.get_buffer()
    bounds = buffer.get_selection_bounds()
    assert bounds, "the line is not selected"
    assert buffer.get_text(bounds[0], bounds[1], False) == "# my hosts"


def test_an_included_file_is_shown_evaluated_and_saved(gtk, tmp_path):
    from sshpilot.advanced_config_window import AdvancedConfigWindow

    included = tmp_path / "hosts.conf"
    included.write_text("Host inc1\n    User alice\n\nHost other\n    User bob\n")
    path = tmp_path / "config"
    path.write_text(f"Host inc1\n    Port 2200\n\nInclude {included}\n")
    client = _Client(path, included=[included])
    win = AdvancedConfigWindow(None, client)
    try:
        assert _pump(lambda: len(win.draft.blocks) == 1)
        files = win.sidebar._file_rows                      # a heading per file
        assert list(files) == ["", str(included)]
        assert not files[""].get_activatable() and files[str(included)].get_activatable()
        assert win.raw.tabs.get_visible()                   # and a tab per file

        win.go_to(host="inc1", file=str(included))          # a connection's source file
        assert _pump(lambda: win._file == str(included) and len(win.draft.blocks) == 2)
        assert _selected(win) == "inc1"
        assert win.structured.head_file.get_label() == "hosts.conf"
        assert files[""].get_activatable() and not files[str(included)].get_activatable()
        assert _pump(lambda: _command(win) == "ssh -G inc1")
        assert _effective(win, "port") == "2200"            # the root's line counts too

        _field(win, "user").value.set_text("carol")
        assert _pump(lambda: win.dirty)
        win.save()
        assert _pump(lambda: client.saves == 1 and not win.dirty)
        assert included.read_text().startswith("Host inc1\n    User carol\n")
        assert "Include" in path.read_text()                # the root was not written

        win.sidebar._on_row_activated(win.sidebar.list, files[""])   # the root's heading
        assert _pump(lambda: win._file == "" and _selected(win) == "inc1")
    finally:
        _close(win)


def test_a_source_in_an_included_file_is_gone_to_and_back(gtk, tmp_path):
    from sshpilot.advanced_config_window import AdvancedConfigWindow

    included = tmp_path / "hosts.conf"
    included.write_text("Host web1\n    User fromfile\n")
    path = tmp_path / "config"
    path.write_text(f"Include {included}\n\nHost web1\n    Port 2222\n")
    win = AdvancedConfigWindow(None, _Client(path, included=[included]))
    try:
        assert _pump(lambda: len(win.draft.blocks) == 2)      # the Include line, and Host web1
        win.select_block(win.draft.blocks[-1].uid)
        assert _pump(lambda: _effective(win, "user") == "fromfile")
        user = win.effective._items["user"]
        assert (user.file, user.file_line) == (str(included), 2)
        assert user.kind == "other"
        assert win.source_file(user.file) == str(included)    # one of this configuration's
        assert win.source_file("/etc/ssh/ssh_config") is None

        win.reveal_source(user.file, user.file_line)          # the source, clicked
        assert _pump(lambda: win._file == str(included) and _selected(win) == "web1")
        assert win.target_text() == "web1"                    # still the host read about
        assert _pump(lambda: _effective(win, "port") == "2222"
                     and win.source_file(win.effective._items["port"].file) == "")
        port = win.effective._items["port"]
        win.reveal_source(port.file, port.file_line)
        assert _pump(lambda: win._file == "" and _selected(win) == "web1")
    finally:
        _close(win)


# -- the Raw view and the blocks are one place --------------------------------------------

def _raw_marked(win):
    """The lines of the Raw view marked as the selected block's."""
    buffer = win.raw.view.get_buffer()
    tag = win.raw._block_tag
    marked = []
    for number in range(buffer.get_line_count()):
        found, start = buffer.get_iter_at_line(number)
        if found and start.has_tag(tag):
            end = start.copy()
            end.forward_to_line_end()
            marked.append(buffer.get_text(start, end, False))
    return marked


def test_the_raw_view_marks_the_selected_block_and_follows_edits(window):
    assert window.stack.get_page(window.raw).get_title() == "Raw"
    _raw(window)
    glob, web, star = window.draft.blocks
    window.select_block(web.uid)
    assert _raw_marked(window) == [
        "Host web1 web2", "    User deploy", "    Port 2222", "    IdentityFile ~/.ssh/a",
    ]
    window.select_block(star.uid)
    assert _raw_marked(window)[0] == "Host *"
    window.add_option(star.uid, "Compression", "no")   # the block grew
    assert _raw_marked(window)[-1].strip() == "Compression no"


def test_the_cursor_in_the_raw_view_selects_the_block_it_is_in(window):
    buffer = _raw(window)
    window.raw.view.has_focus = lambda: True                 # the user is in it
    glob, web, star = window.draft.blocks
    line = window.editor.lines.options[star.options[0].uid]  # a line of Host *
    found, position = buffer.get_iter_at_line(line - 1)
    buffer.place_cursor(position)
    assert window.selected_uid == star.uid
    assert window.sidebar.list.get_selected_row()._uid == star.uid
    assert _raw_marked(window)[0] == "Host *"

    # Text typed and not applied is not the blocks' lines: nothing follows it.
    buffer.insert_at_cursor("x")
    assert window.raw.pending and _raw_marked(window) == []
    found, position = buffer.get_iter_at_line(window.editor.lines.headers[web.uid] - 1)
    buffer.place_cursor(position)
    assert window.selected_uid == star.uid


# -- answers are had ahead of time: a selection shows them at once ----------------------

def test_a_selection_is_answered_in_the_same_step(gtk, tmp_path):
    """The hosts the configuration names are asked about in the background,
    so selecting one shows what applies with no wait, a pattern block too;
    and an edit asks again rather than showing what no longer holds."""
    from sshpilot.advanced_config_window import AdvancedConfigWindow

    path = tmp_path / "config"
    path.write_text(CONFIG + "\nHost db1\n    User postgres\n")
    client = _Client(path)
    win = AdvancedConfigWindow(None, client)
    try:
        assert _pump(lambda: len(win.draft.blocks) == 4)
        glob, web, star, db = win.draft.blocks
        # web1, web2, db1, and the name made up for "Host *"
        assert _pump(lambda: len(win._answers) >= 4, seconds=10), win._answers.keys()

        asked = []
        real = client.evaluate_ssh_config_draft
        client.evaluate_ssh_config_draft = lambda request: asked.append(request.host) or real(request)

        win.select_block(web.uid)                              # no pump: at once
        assert _command(win) == "ssh -G web1"
        win.select_block(db.uid)
        assert _command(win) == "ssh -G db1"
        win.select_block(star.uid)                             # a pattern block
        assert asked == []                                     # nothing was asked

        win.select_block(web.uid)
        _field(win, "user").value.set_text("admin")            # another draft
        assert _pump(lambda: _effective(win, "user") == "admin")
        assert "web1" in asked                                 # asked again, for the new text
    finally:
        _close(win)


def test_undo_and_redo_have_keys_wherever_the_keyboard_is(window):
    """Ctrl+Z is the window's undo, not the field's it was pressed in; in
    the Raw view it is left to the text typed there."""
    names = lambda: [b.patterns or b.kind for b in window.draft.blocks]  # noqa: E731
    glob, web, star = window.draft.blocks
    window.delete_block(star.uid)
    assert names() == ["global", "web1 web2"]
    assert window._history_key(window.undo) is True
    assert names() == ["global", "web1 web2", "*"] and window.editor.text == CONFIG
    assert window._history_key(window.redo) is True
    assert names() == ["global", "web1 web2"]
    window.undo()

    raw = window.raw.view
    window.get_focus = lambda: raw                         # the keyboard is in the text
    window.delete_block(window.draft.blocks[2].uid)
    assert window._history_key(window.undo) is False       # the text's own undo
    assert names() == ["global", "web1 web2"]
    assert "Ctrl+Z" in window.undo_button.get_tooltip_text()


# -- review, 2026-11-24 ------------------------------------------------------------------

def test_a_save_is_of_the_file_it_was_clicked_for(gtk, tmp_path):
    """Review P1: the save took its text when clicked and its file name and
    revision when OpenSSH had answered. Opening another file in between
    wrote the first one's text over the second."""
    import threading

    from sshpilot.advanced_config_window import AdvancedConfigWindow

    included = tmp_path / "hosts.conf"
    included.write_text("Host inc\n    User alice\n")
    path = tmp_path / "config"
    root_text = f"Host top\n    User me\n\nInclude {included}\n"
    path.write_text(root_text)
    client = _Client(path, included=[included])
    win = AdvancedConfigWindow(None, client)
    try:
        assert _pump(lambda: len(win.draft.blocks) == 1 and len(win._files) == 2)
        _pump(lambda: not win._prefetching and win._prefetch_source == 0, seconds=5)

        gate = threading.Event()
        real = client.evaluate_ssh_config_draft

        def slow(request):                                    # the save's question waits
            if request.host == "sshpilot-config-check":
                gate.wait(5)
            return real(request)

        client.evaluate_ssh_config_draft = slow
        _field(win, "user").value.set_text("root")
        assert _pump(lambda: win.dirty)
        win.save()
        assert win._saving
        win._confirm_discard = lambda proceed, stay=None: proceed()
        win.open_file(str(included))
        win.go_to(host="inc", file=str(included))             # and from elsewhere in the app
        _pump(lambda: False, seconds=0.6)
        gate.set()
        _pump(lambda: client.saves >= 1 and not win._saving, seconds=5)
        assert included.read_text() == "Host inc\n    User alice\n"   # not written over
        assert path.read_text() == root_text.replace("User me", "User root")
        assert client.saves == 1
        assert _pump(lambda: win._file == str(included))
    finally:
        _close(win)


def test_agreeing_to_run_a_command_is_for_the_host_on_screen_only(window, tmp_path):
    """Review P1: "Run and Evaluate" is an agreement for one host. The
    hosts asked about ahead of time, a pattern block's hosts and the save's
    check must not carry it."""
    client = window._test_client
    asked = []
    real = client.evaluate_ssh_config_draft

    def record(request):
        asked.append((request.host, request.allowed_exec))
        return real(request)

    client.evaluate_ssh_config_draft = record
    marker = tmp_path / "ran-for"
    window.add_block("match")
    window.structured.patterns_row.set_text(f'exec "echo %h >> {marker}"')
    assert _pump(lambda: window.banner.get_revealed() and bool(window.banner.get_button_label()))
    window.banner.emit("button-clicked")                  # agreed: for web1
    assert _pump(marker.exists)
    assert _settled(window)
    _pump(lambda: False, seconds=1.2)                     # let the background asking run

    _check(window, "")                                    # no host typed any more
    _pump(lambda: False, seconds=0.5)
    window.select_block(window.draft.blocks[2].uid)       # Host *: a pattern block
    _pump(lambda: False, seconds=1.0)
    assert window.banner.get_revealed() and not window.banner.get_button_label()
    window.save()
    assert _pump(lambda: client.saves == 1)

    carried = {host for host, allowed in asked if allowed}
    assert carried == {"web1"}, carried
    assert set(marker.read_text().split()) == {"web1"}


def test_reload_after_a_save_conflict_asks_before_dropping_the_draft(window):
    client = window._test_client
    _field(window, "user").value.set_text("mine")
    assert _pump(lambda: window.dirty)
    client.path.write_text(CONFIG + "\n# edited elsewhere\n")
    window.save()
    assert _pump(lambda: window.file_banner.get_revealed() and bool(window.file_banner.get_button_label()))
    asked, loads = [], []
    window._confirm_discard = lambda proceed, stay=None: asked.append(proceed)
    real_load = window._load
    window._load = lambda *args: (loads.append(args), real_load(*args))[1]
    window.file_banner.emit("button-clicked")
    assert asked and not loads                            # asked, nothing loaded
    assert "mine" in window.editor.text
    asked[0]()                                            # "Discard"
    assert _pump(lambda: "edited elsewhere" in window.editor.text)


def test_the_consent_prompt_says_what_would_run_and_for_whom(window, tmp_path):
    marker = tmp_path / "ran"
    window.add_block("match")
    command = f"touch {marker} # for %h"
    window.structured.patterns_row.set_text(f'exec "{command}"')
    assert _pump(lambda: window.banner.get_revealed() and bool(window.banner.get_button_label()))
    said = window.banner.get_title()
    assert command in said and "web1" in said
    assert not window.banner.get_use_markup()
    assert not marker.exists()


# -- where it is shown -------------------------------------------------------------------

def test_embedded_the_list_of_blocks_is_left_to_the_host(window, shell):
    if shell != "embedded":
        pytest.skip("the window's own sidebar is the next test's")
    assert window.split is None and not window.sidebar_button.get_visible()
    assert window.sidebar_column is window.sidebar
    assert window.sidebar.get_parent() is not None             # in the host's box
    window.go_to(line=1)
    assert window.stack.get_visible_child_name() == "raw"


def test_the_window_s_sidebar_folds_away(window, shell):
    if shell == "embedded":
        pytest.skip("embedded, the list of blocks is in the host's sidebar")
    assert window.split.get_sidebar() is window.sidebar
    window.sidebar_button.set_active(False)
    assert not window.split.get_show_sidebar()
    window.split.set_show_sidebar(True)
    assert window.sidebar_button.get_active()


def test_the_window_says_when_it_closes(window):
    closed = []
    window.add_closed_listener(lambda: closed.append(1))
    _field(window, "user").value.set_text("unsaved")
    assert _pump(lambda: window.dirty)
    asked = []
    window._confirm_discard = lambda proceed, stay=None: asked.append(proceed)
    assert window._on_close_request() is True
    assert asked and closed == []
    asked[0]()
    assert window._on_close_request() is False
    assert closed == [1]
    assert window._on_close_request() is False
    assert closed == [1]


# -- a block begun from the connection editor ------------------------------------------

def test_a_block_handed_over_is_added_unsaved_with_its_host_line_and_options(window):
    before = len(window.draft.blocks)
    window.start_block("*.prod !bastion", [("HostName", "%h.example.com"), ("User", "deploy")])
    assert _pump(lambda: len(window.draft.blocks) == before + 1)
    block = window.draft.blocks[-1]
    assert block.patterns == "*.prod !bastion"
    assert [(o.key, o.value) for o in block.options] == [
        ("HostName", "%h.example.com"), ("User", "deploy"),
    ]
    assert window.selected_uid == block.uid
    assert window.structured.patterns_row.get_text() == "*.prod !bastion"
    assert _field(window, "hostname").value.get_text() == "%h.example.com"
    assert window._test_client.saves == 0
    assert "Host *.prod !bastion\n" in window.editor.text


def test_a_block_handed_over_before_the_file_is_read_waits_for_it(gtk, tmp_path):
    from sshpilot.advanced_config_window import AdvancedConfigWindow

    path = tmp_path / "config"
    path.write_text(CONFIG)
    win = AdvancedConfigWindow(None, _Client(path))
    try:
        win.start_block("*.lab", [])
        assert _pump(lambda: bool(win.draft.blocks) and win.draft.blocks[-1].patterns == "*.lab")
        assert [(o.key, o.value) for o in win.draft.blocks[-1].options] == [("", "")]
    finally:
        _close(win)
