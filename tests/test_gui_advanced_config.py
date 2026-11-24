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
    """Keep *host* in question whichever block is selected, as ``go_to``
    with *pin* does (the connection editor's way in). It need not be a host
    the file names. Typing in the search field is another matter: that
    finds blocks, and a name no block has finds nothing."""
    win._set_typed(host)
    win._target_changed()


def _filter_by(win, host):
    """Keep a host in question and wait for OpenSSH's answer on it."""
    assert _pump(lambda: bool(win.draft.blocks))
    _check(win, host)
    assert _pump(lambda: (
        win._evaluation is not None and win._evaluation.evaluated
        and win._evaluated_serial == win.editor.serial
        and win.results.summary_rows["command"].get_subtitle() == f"ssh {host}"
    ))


def _dimmed(win):
    """Blocks dimmed as not applying to the host. Every block stays on
    screen, whole: none is hidden or collapsed."""
    assert all(win.rules.cards[b.uid].card.get_visible() for b in win.draft.blocks)
    return [
        block.patterns or block.kind for block in win.draft.blocks
        if win.rules.cards[block.uid].card.has_css_class("skipped")
    ]


def _block_rows(win):
    """The sidebar's rows, as they read ("" is All Blocks)."""
    return [row._text for row in _children(win._block_list) if hasattr(row, "_text")]


def _pick(win, text):
    """Choose a row of the sidebar ("" is All Blocks), as a click does."""
    row = next(row for row in _children(win._block_list) if row._text == text)
    win._block_list.select_row(row)


def _settled(win):
    return _pump(lambda: (
        win._evaluate_source == 0
        and win._evaluation is not None
        and win._evaluated_serial == win.editor.serial
    ))


def _effective(win, key):
    """The value the Effective page lists for *key* (None: not listed)."""
    win.results.fill_all()       # whichever page is open
    item = win.results._items.get(key)
    return item.values if item is not None and item.present else None


def _children(widget):
    found = []
    child = widget.get_first_child()
    while child is not None:
        found.append(child)
        child = child.get_next_sibling()
    return found


def _row(win, option_uid):
    for card in win.rules.cards.values():
        if option_uid in card.rows:
            return card.rows[option_uid]
    raise AssertionError(f"no row for option {option_uid}")


def _status(win, option):
    pill = _row(win, option.uid).status
    return pill.get_label() if pill.get_visible() else None


def _text_page(win):
    """Open the Text page (pages are filled when they are on screen)."""
    win.results.stack.set_visible_child_name("raw")
    return win.results.raw_view.get_buffer()


def test_it_opens_showing_every_block_and_asks_nothing(gtk, tmp_path):
    from sshpilot.advanced_config_window import AdvancedConfigWindow

    path = tmp_path / "config"
    path.write_text(CONFIG)
    win = AdvancedConfigWindow(None, _Client(path))
    assert _pump(lambda: len(win.draft.blocks) == 3)
    assert win.target_entry.get_text() == "" and win._evaluation is None
    assert win._block_list.get_selected_row() is win._all_row
    assert _dimmed(win) == []
    assert not any(
        win.rules.cards[b.uid].status.get_visible() for b in win.draft.blocks
    )
    win._closing = True
    win.destroy()


def test_filtering_by_a_host_shows_openssh_s_answer(window):
    assert _block_rows(window) == ["", "Global", "Host web1 web2", "Host *"]
    assert not window.dirty and not window.save_button.get_sensitive()
    glob, web, star = window.draft.blocks
    assert [window.rules.cards[b.uid].status.get_label() for b in (glob, web, star)] == [
        "Always", "Matched", "Matched",
    ]
    assert [_status(window, o) for o in web.options] == ["Applied", "Applied", "Accumulated"]
    assert [_status(window, o) for o in star.options] == ["Shadowed", "Overridden"]
    assert _effective(window, "user") == "deploy"
    assert _effective(window, "connecttimeout") == "7"      # Settings beat the file
    assert window.results.summary_rows["command"].get_subtitle() == "ssh web1"
    assert window.results.summary_rows["host"].get_subtitle() == "web1 : 2222"


def test_another_target_is_asked_again(window):
    _check(window, "db1")
    assert _pump(lambda: _effective(window, "user") == "fallback")
    # The block OpenSSH skips for the host folds, where it is.
    assert _pump(lambda: _dimmed(window) == ["web1 web2"])
    web = window.draft.blocks[1]
    # Lines of a block that is not this host's: dimmed with it, no word on them.
    assert [_status(window, o) for o in web.options] == [None] * 3
    assert all(
        _row(window, o.uid).box.has_css_class("advcfg-unused") for o in web.options
    )
    window.target_entry.set_text("web1")       # typed: found, selected, asked about
    assert _pump(lambda: _dimmed(window) == [] and _effective(window, "user") == "deploy")
    assert window.selected_uid == web.uid
    window.target_entry.set_text("")           # the search ends: what it found stays
    _pump(lambda: False, seconds=0.5)
    assert window.selected_uid == web.uid and _effective(window, "user") == "deploy"
    window.select_block(None)                  # no block, no host: every block open
    assert _pump(lambda: window._evaluation is None)
    assert _dimmed(window) == []
    assert window._block_list.get_selected_row() is window._all_row


def test_a_block_being_worked_on_stays_open(window):
    """Adding or editing a block must not dim it under the hands."""
    window.add_block("host")
    block = window.draft.blocks[-1]
    window.rules.cards[block.uid].patterns.set_text("elsewhere")
    assert _settled(window)
    assert window.rules.cards[block.uid].status.get_label() == ""
    assert _dimmed(window) == []
    _check(window, "db1")                      # another host starts afresh
    assert _pump(lambda: _dimmed(window) == ["web1 web2"])
    window.select_block(None)                  # and it folds once it is left
    assert _dimmed(window) == ["web1 web2", "elsewhere"]


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
    assert not window.rules.add_global_button.get_sensitive()   # there is one already


def test_typing_a_value_re_evaluates_without_redrawing(window):
    web = window.draft.blocks[1]
    entry = _row(window, web.options[0].uid).value
    entry.set_text("root")
    assert _pump(lambda: _effective(window, "user") == "root")
    assert _row(window, web.options[0].uid).value is entry
    assert "    User root\n" in window.editor.text


def test_a_rejected_line_is_marked_and_named(window):
    web = window.draft.blocks[1]
    window.add_option(web.uid, "Bogus", "1")
    assert _pump(lambda: window.banner.get_revealed())
    assert "line 9" in window.banner.get_title()
    assert _status(window, web.options[-1]) == "Rejected"
    window.delete_option(web.options[-1].uid)
    assert _pump(lambda: not window.banner.get_revealed())
    assert _effective(window, "user") == "deploy"


def test_a_new_match_exec_runs_only_after_consent(window, tmp_path):
    marker = tmp_path / "ran"
    window.add_block("match")
    block = window.draft.blocks[-1]
    window.rules.cards[block.uid].patterns.set_text(f'exec "touch {marker}"')
    assert _pump(lambda: window.banner.get_revealed())
    assert window.banner.get_button_label() and not marker.exists()
    window.banner.emit("button-clicked")
    assert _pump(marker.exists)
    # The answer that follows the consent, not the one that asked for it.
    assert _pump(lambda: window._evaluation is not None and window._evaluation.evaluated)
    assert window.rules.cards[block.uid].status.get_label() == "Matched"


def test_disabling_a_block_writes_comments_and_openssh_skips_it(window):
    web = window.draft.blocks[1]
    window.toggle_block(web.uid)
    assert _settled(window) and _effective(window, "user") == "fallback"
    assert "# [disabled] Host web1 web2\n" in window.editor.text
    web = window.draft.blocks[1]
    assert window.rules.cards[web.uid].status.get_label() == "Disabled"
    assert "web1 web2" not in _dimmed(window)   # just switched off: still open


def test_options_move_between_blocks(window):
    web, star = window.draft.blocks[1], window.draft.blocks[2]
    port = web.options[1]
    assert window.move_option(port.uid, star.uid, 0)
    assert [o.key for o in window.draft.blocks[2].options][0] == "Port"
    assert _settled(window) and _effective(window, "port") == "2222"


def test_text_page_edits_are_applied_to_the_rules(window):
    buffer = _text_page(window)
    assert buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False) == CONFIG
    buffer.set_text("Host web1\n    User typed\n")
    assert window.results.apply_raw_button.get_sensitive()
    window.apply_raw()
    assert _settled(window) and _effective(window, "user") == "typed"
    assert [b.patterns for b in window.draft.blocks] == ["web1"]


def test_save_writes_only_what_changed_and_clears_dirty(window):
    client = window._test_client
    web = window.draft.blocks[1]
    _row(window, web.options[0].uid).value.set_text("root")
    assert _pump(lambda: window.dirty)
    window.save()
    assert _pump(lambda: client.saves == 1 and not window.dirty)
    assert client.path.read_text() == CONFIG.replace("User deploy", "User root")


def test_a_file_changed_on_disk_is_not_overwritten(window):
    client = window._test_client
    client.path.write_text(CONFIG + "# edited elsewhere\n")
    web = window.draft.blocks[1]
    _row(window, web.options[0].uid).value.set_text("root")
    assert _pump(lambda: window.dirty)
    window.save()
    assert _pump(lambda: (
        window.file_banner.get_revealed() and bool(window.file_banner.get_button_label())
    ))
    assert client.saves == 0 and "edited elsewhere" in client.path.read_text()


def test_the_sidebar_lists_the_blocks_and_a_typed_host_stays(window):
    """The fixture typed web1: that host is watched whichever block is
    selected, from the sidebar or from a card."""
    glob, web, star = window.draft.blocks
    window.add_block("host")
    db = window.draft.blocks[-1]
    window.rules.cards[db.uid].patterns.set_text("db1 *.internal")
    assert _block_rows(window) == [
        "", "Global", "Host web1 web2", "Host *", "Host db1 *.internal",
    ]
    assert window._block_list.get_selected_row()._uid == db.uid   # the new block
    assert _settled(window)
    asked = window._request_serial

    _pick(window, "Host *")
    assert window.selected_uid == star.uid
    window.select_block(web.uid)                      # a click on a card
    assert window._block_list.get_selected_row()._uid == web.uid
    _pump(lambda: False, seconds=0.4)
    assert window._request_serial == asked            # nothing new was asked
    assert window.target_entry.get_text() == "web1" and window.target_text() == "web1"
    assert window.results.summary_rows["command"].get_subtitle() == "ssh web1"
    assert window.rules.cards[db.uid].status.get_label() == ""
    assert _dimmed(window) == []                      # just worked on: left open

    window.target_entry.set_text("")                  # unpinned: the selection says
    assert _pump(lambda: _dimmed(window) == ["db1 *.internal"])
    assert window.target_text() == "web1"             # web's block is selected
    # The sidebar's rows are never dimmed: every block can be picked.
    assert all(row.get_child().get_opacity() == 1 for row in window._block_rows.values())
    _pick(window, "Host db1 *.internal")
    assert window.target_text() == "db1"              # the first plain name
    assert _pump(lambda: _effective(window, "user") == "fallback")
    assert _dimmed(window) == ["web1 web2"]

    _pick(window, "")                                 # All Blocks: no block, no host
    assert window.selected_uid is None
    assert _pump(lambda: window._evaluation is None and _dimmed(window) == [])
    _pick(window, "Host web1 web2")
    window.delete_block(web.uid)                      # a selected block that goes
    assert _block_rows(window) == ["", "Global", "Host *", "Host db1 *.internal"]
    assert window._block_list.get_selected_row() is window._all_row
    assert window.target_text() == ""
def test_agreeing_to_a_command_does_not_cover_an_edited_one(window, tmp_path):
    """Review P1: consent is for the command shown, and for this target."""
    first, second = tmp_path / "first", tmp_path / "second"
    window.add_block("match")
    block = window.draft.blocks[-1]
    entry = window.rules.cards[block.uid].patterns
    entry.set_text(f'exec "touch {first}"')
    assert _pump(lambda: window.banner.get_revealed())
    window.banner.emit("button-clicked")
    assert _pump(first.exists)
    assert _pump(lambda: window._evaluation is not None and window._evaluation.evaluated)

    entry.set_text(f'exec "touch {second}"')          # another command
    assert _pump(lambda: window._evaluation is not None and bool(window._evaluation.pending_exec))
    assert not second.exists() and window.banner.get_revealed()

    entry.set_text(f'exec "touch {first}"')           # back to the agreed one
    assert _pump(lambda: window._evaluation is not None and window._evaluation.evaluated)
    first.unlink()
    window.target_entry.set_text("web2")              # another target asks again
    assert _pump(lambda: window._evaluation is not None and bool(window._evaluation.pending_exec))
    assert not first.exists()


def test_text_typed_on_the_text_page_is_an_unsaved_change(window):
    """Review P1: closing must not drop it, and Save writes what is shown."""
    client = window._test_client
    assert not window.dirty
    _text_page(window).set_text("Host web1\n    User typed\n")
    assert window.raw_pending and window.dirty
    assert window.save_button.get_sensitive()
    assert window._on_close_request() is True          # asks before discarding
    window.save()
    assert _pump(lambda: client.saves == 1 and not window.dirty)
    assert client.path.read_text() == "Host web1\n    User typed\n"
    assert [b.patterns for b in window.draft.blocks] == ["web1"]


def test_save_writes_the_text_page_not_the_older_rules(window):
    client = window._test_client
    web = window.draft.blocks[1]
    _row(window, web.options[0].uid).value.set_text("root")
    assert _pump(lambda: window.dirty)
    newer = CONFIG.replace("User deploy", "User newest")
    _text_page(window).set_text(newer)
    window.save()
    assert _pump(lambda: client.saves == 1)
    assert client.path.read_text() == newer


def test_clicking_a_block_selects_it_and_alt_arrows_move_it(window):
    glob, web, star = window.draft.blocks
    cards = {b.uid: window.rules.cards[b.uid].card for b in window.draft.blocks}
    assert window.selected_uid is None
    assert not any(card.has_css_class("selected") for card in cards.values())
    window.select_block(star.uid)                 # what a click on the card does
    assert cards[star.uid].has_css_class("selected")
    window.select_block(web.uid)
    assert not cards[star.uid].has_css_class("selected")
    assert cards[web.uid].has_css_class("selected")

    window.move_selected(1)                        # Alt+Down
    assert [b.patterns for b in window.draft.blocks] == ["", "*", "web1 web2"]
    assert window.selected_uid == web.uid          # still selected after the redraw
    assert window.rules.cards[web.uid].card.has_css_class("selected")
    window.move_selected(-1)                       # Alt+Up
    window.move_selected(-1)                       # not above the global options
    assert [b.patterns for b in window.draft.blocks] == ["", "web1 web2", "*"]

    window.delete_block(web.uid)
    assert window.selected_uid is None


def test_a_real_click_on_a_card_selects_it(window):
    from gi.repository import Gtk

    web = window.draft.blocks[1]
    click = window.rules.cards[web.uid].click
    assert click.get_propagation_phase() == Gtk.PropagationPhase.CAPTURE
    click.emit("pressed", 1, 5.0, 5.0)
    assert window.selected_uid == web.uid


def test_selecting_a_card_is_selecting_its_row_in_the_sidebar(gtk, tmp_path):
    """One selection, two views of it. With no host typed, the selected
    block says which host OpenSSH is asked about."""
    from sshpilot.advanced_config_window import AdvancedConfigWindow

    path = tmp_path / "config"
    path.write_text(CONFIG + "\nHost db1\n    User postgres\n")
    win = AdvancedConfigWindow(None, _Client(path))
    assert _pump(lambda: len(win.draft.blocks) == 4)
    glob, web, star, db = win.draft.blocks
    command = win.results.summary_rows["command"]

    win.select_block(web.uid)                             # a click on the card
    assert win._block_list.get_selected_row()._uid == web.uid
    assert _pump(lambda: command.get_subtitle() == "ssh web1")
    assert _effective(win, "user") == "deploy"
    assert win.rules.cards[db.uid].status.get_label() == ""
    assert _dimmed(win) == ["db1"]

    win.select_block(db.uid)                              # a dimmed block is at full strength at once
    assert "db1" not in _dimmed(win)
    assert _pump(lambda: command.get_subtitle() == "ssh db1")
    assert _dimmed(win) == ["web1 web2"]

    _pick(win, "Host web1 web2")                          # the same, from the sidebar
    assert win.selected_uid == web.uid
    assert win.rules.cards[web.uid].card.has_css_class("selected")
    assert _pump(lambda: command.get_subtitle() == "ssh web1")

    win.select_block(glob.uid)                            # names no host: nothing asked
    assert _pump(lambda: win._evaluation is None and _dimmed(win) == [])
    win.select_block(None)
    assert win._block_list.get_selected_row() is win._all_row
    win._closing = True
    win.destroy()


def test_a_source_s_line_is_shown_without_losing_the_host(gtk, tmp_path):
    """Going from the Effective page to the line behind a value selects the
    line's block: the host being read about is typed in, so it stays."""
    from sshpilot.advanced_config_window import AdvancedConfigWindow

    path = tmp_path / "config"
    path.write_text(CONFIG)
    win = AdvancedConfigWindow(None, _Client(path))
    assert _pump(lambda: len(win.draft.blocks) == 3)
    glob, web, star = win.draft.blocks
    win.select_block(web.uid)
    assert _pump(lambda: win.results.summary_rows["command"].get_subtitle() == "ssh web1")
    win.reveal_line(win._evaluated_lines.headers[star.uid])
    assert win.selected_uid == star.uid
    assert win.target_entry.get_text() == "web1" and win.target_text() == "web1"
    _pump(lambda: False, seconds=0.4)
    assert win.results.summary_rows["command"].get_subtitle() == "ssh web1"
    win._closing = True
    win.destroy()
def test_picking_a_pattern_block_marks_the_blocks_related_to_it(gtk, tmp_path):
    """A pattern or a Match rule names no host. Picking one marks the blocks
    that apply to a host it applies to, each host being OpenSSH's answer."""
    from sshpilot.advanced_config_window import AdvancedConfigWindow

    path = tmp_path / "config"
    path.write_text(
        "Host a.prod\n    User a\n\nHost b.prod c.test\n    User b\n\n"
        "Host d.test\n    User d\n\nHost *.prod\n    Port 2200\n\n"
        "Match host *.test\n    Port 2300\n\nHost *.lab\n    Port 2400\n\n"
        "Host *\n    Compression yes\n"
    )
    win = AdvancedConfigWindow(None, _Client(path))
    assert _pump(lambda: len(win.draft.blocks) == 7)
    a, b, d, prod, test, lab, star = win.draft.blocks

    def pills():
        return [win.rules.cards[block.uid].status.get_label() for block in win.draft.blocks]

    _pick(win, "Host *.prod")
    assert _pump(lambda: pills()[0] == "Matched")
    assert pills() == ["Matched", "Matched", "", "Matched", "", "", "Matched"]
    assert _dimmed(win) == ["d.test", "host *.test", "*.lab"]
    assert win._evaluation is None and not win.results.summary.get_visible()
    assert "a.prod, b.prod" in win.results.note.get_label()
    assert _status(win, a.options[0]) is None             # lines are told per host

    _pick(win, "Match host *.test")                       # a rule, as a pattern
    assert _pump(lambda: pills()[2] == "Matched")
    assert pills() == ["", "Matched", "Matched", "", "Matched", "", "Matched"]
    assert "c.test, d.test" in win.results.note.get_label()

    _pick(win, "Host *.lab")                              # no listed host fits it:
    assert _pump(lambda: pills()[5] == "Matched")         # a made-up one is asked about
    assert pills() == [""] * 5 + ["Matched", "Matched"]
    assert "none of the hosts" in win.results.note.get_label()

    _pick(win, "Host a.prod")                             # a host: the usual answer
    assert _pump(lambda: win.results.summary_rows["command"].get_subtitle() == "ssh a.prod")
    assert pills() == ["Matched", "", "", "Matched", "", "", "Matched"]
    assert _status(win, a.options[0]) == "Applied"
    _pick(win, "")
    assert _pump(lambda: pills() == [""] * 7 and _dimmed(win) == [])
    win._closing = True
    win.destroy()


def test_retyping_the_picked_block_s_host_follows_it(gtk, tmp_path):
    from sshpilot.advanced_config_window import AdvancedConfigWindow

    path = tmp_path / "config"
    path.write_text(CONFIG)
    win = AdvancedConfigWindow(None, _Client(path))
    assert _pump(lambda: len(win.draft.blocks) == 3)
    web = win.draft.blocks[1]
    _pick(win, "Host web1 web2")
    assert _pump(lambda: win.results.summary_rows["command"].get_subtitle() == "ssh web1")
    win.rules.cards[web.uid].patterns.set_text("edge9 web2")
    assert _block_rows(win) == ["", "Global", "Host edge9 web2", "Host *"]
    assert win._block_list.get_selected_row()._uid == web.uid
    assert _pump(lambda: win.results.summary_rows["command"].get_subtitle() == "ssh edge9")
    assert win.target_entry.get_text() == ""
    win._closing = True
    win.destroy()


# -- review findings -----------------------------------------------------------

def _open_on(gtk, tmp_path, text, *, writable=True, client_class=_Client):
    from sshpilot.advanced_config_window import AdvancedConfigWindow

    path = tmp_path / "config"
    path.write_text(text)
    client = client_class(path)
    client.writable = writable
    win = AdvancedConfigWindow(None, client)
    win._test_client = client
    assert _pump(lambda: win._loaded)
    return win


class _ReadOnlyClient(_Client):
    def get_ssh_config_text(self, file=""):
        from sshpilot.api.models.connections import SshConfigText

        return SshConfigText(self.path.read_text(), self._revision(), "config", False)


def test_a_file_notice_does_not_hide_what_openssh_says(gtk, tmp_path):
    """Review: a read-only notice suppressed every later banner, the Match
    exec prompt included, so such a draft could never be evaluated."""
    marker = tmp_path / "ran"
    win = _open_on(gtk, tmp_path, CONFIG, client_class=_ReadOnlyClient)
    assert win.file_banner.get_revealed() and not win.save_button.get_sensitive()
    win.target_entry.set_text("web1")
    win.add_block("match")
    block = win.draft.blocks[-1]
    win.rules.cards[block.uid].patterns.set_text(f'exec "touch {marker}"')
    assert _pump(lambda: win.banner.get_revealed() and bool(win.banner.get_button_label()))
    assert win.file_banner.get_revealed()               # both are up
    win.banner.emit("button-clicked")
    assert _pump(marker.exists)
    win._closing = True
    win.destroy()


def test_a_failed_save_notice_goes_with_the_next_good_save(window):
    client = window._test_client
    real = client.save_ssh_config_text
    client.save_ssh_config_text = lambda request: (_ for _ in ()).throw(OSError("disk"))
    web = window.draft.blocks[1]
    _row(window, web.options[0].uid).value.set_text("root")
    assert _pump(lambda: window.dirty)
    window.save()
    assert _pump(lambda: window.file_banner.get_revealed())
    assert window.file_banner.get_button_label()         # it can be dismissed
    client.save_ssh_config_text = real
    window.save()
    assert _pump(lambda: client.saves == 1 and not window.file_banner.get_revealed())


def test_an_answer_for_a_host_no_longer_asked_about_is_dropped(window):
    """Review: clearing the filter did not outdate the question in flight,
    so its answer came back and filled the pages again."""
    import threading

    client = window._test_client
    gate = threading.Event()
    real = client.evaluate_ssh_config_draft

    def slow(request):
        if request.host == "slowhost":
            gate.wait(5)
        return real(request)

    client.evaluate_ssh_config_draft = slow
    window.select_block(None)
    window.target_entry.set_text("slowhost")
    assert _pump(lambda: window._evaluate_source == 0, seconds=3)
    window.target_entry.set_text("")
    assert _pump(lambda: window._evaluation is None and not window.results.summary.get_visible())
    gate.set()
    _pump(lambda: False, seconds=0.6)
    assert window._evaluation is None and not window.results.summary.get_visible()


def test_a_dragged_block_is_not_text_an_entry_would_take(window):
    """Review: the payload was a string, so dropping it on a field typed
    "advcfg-block:12" into the configuration."""
    from gi.repository import Gtk

    from sshpilot.advanced_config_rules_view import DragItem

    card = window.rules.cards[window.draft.blocks[1].uid]
    targets = [
        c for c in card.card.observe_controllers() if isinstance(c, Gtk.DropTarget)
    ]
    assert targets and all(
        list(t.get_formats().get_gtypes()) == [DragItem.__gtype__] for t in targets
    )
    row = next(iter(card.rows.values()))
    row_targets = [
        c for c in row.box.observe_controllers() if isinstance(c, Gtk.DropTarget)
    ]
    assert all(
        list(t.get_formats().get_gtypes()) == [DragItem.__gtype__] for t in row_targets
    )
    # And the drop itself still moves the block.
    star = window.draft.blocks[2]
    assert window.drop_block(star.uid, window.draft.blocks[1].uid, True)
    assert [b.patterns for b in window.draft.blocks] == ["", "*", "web1 web2"]


def test_the_rules_wait_while_text_is_being_typed(window):
    """Review: with text pending, rule edits were made beside it and then
    thrown away by Save, which wrote the older text."""
    _text_page(window).set_text(CONFIG.replace("User deploy", "User typed"))
    assert window.raw_pending and not window.rules.get_sensitive()
    assert window.results.revert_raw_button.get_sensitive()
    window.move_selected(1)                               # keys are held back too
    window.discard_raw()
    assert not window.raw_pending and window.rules.get_sensitive() and not window.dirty
    buffer = window.results.raw_view.get_buffer()
    assert buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False) == CONFIG


def test_save_asks_openssh_first_however_fast_it_is_pressed(window):
    """Review: the warning was skipped when the draft had not been evaluated
    yet (within the typing delay, or with no host in question)."""
    client = window._test_client
    window.target_entry.set_text("")                      # no host: nothing evaluated
    assert _pump(lambda: window._evaluation is None)
    web = window.draft.blocks[1]
    _row(window, web.options[1].uid).value.set_text("not-a-port")
    window.save()                                         # at once
    assert _pump(lambda: getattr(window, "_save_alert", None) is not None)
    assert client.saves == 0
    alert = window._save_alert
    _pump(lambda: False, seconds=0.25)
    alert.emit("response", "cancel")
    alert.force_close()
    assert _pump(lambda: not window._saving)
    assert client.saves == 0 and window.dirty and window.save_button.get_sensitive()


def test_a_configuration_openssh_already_rejects_is_said_so_at_open(gtk, tmp_path):
    win = _open_on(gtk, tmp_path, "Host a\n    Bogus yes\n")
    assert _pump(lambda: win.banner.get_revealed())
    assert "line 2" in win.banner.get_title()
    win._closing = True
    win.destroy()


def test_an_edit_redraws_in_place(window):
    """The cards and rows of what did not change are the same widgets
    after an edit: focus, scroll position and typed text survive."""
    web, star = window.draft.blocks[1], window.draft.blocks[2]
    card, row = window.rules.cards[web.uid], _row(window, web.options[0].uid)
    star_card = window.rules.cards[star.uid]
    window.add_option(web.uid, "ProxyJump", "bastion")
    window.move_block(star.uid, 1)
    window.undo()
    assert window.rules.cards[web.uid] is card
    assert window.rules.cards[star.uid] is star_card
    assert _row(window, web.options[0].uid) is row
    assert [c for c in _children(window.rules.box) if c.has_css_class("advcfg-block")] == [
        window.rules.cards[b.uid].card for b in window.draft.blocks
    ]
    assert [o.key for o in web.options] == ["User", "Port", "IdentityFile", "ProxyJump"]
    assert [r.key.get_text() for r in (
        _row(window, o.uid) for o in web.options
    )] == ["User", "Port", "IdentityFile", "ProxyJump"]


def test_only_the_open_page_is_filled(window):
    from sshpilot.advanced_config_results_view import ResultsView  # noqa: F401

    assert window.results.stack.get_visible_child_name() == "effective"
    assert "trace" in window.results._stale and "includes" in window.results._stale
    assert _children(window.results.trace_list) == []
    window.results.stack.set_visible_child_name("trace")
    assert "trace" not in window.results._stale
    assert len(_children(window.results.trace_list)) == 4     # Settings + 3 blocks


# -- going to a place: the entry point the rest of the app uses ------------------

def _selected(win):
    block = win.editor.find_block(win.selected_uid)
    return (block.patterns or block.kind) if block is not None else None


def test_going_to_a_host_before_the_window_has_loaded(gtk, tmp_path):
    from sshpilot.advanced_config_window import AdvancedConfigWindow

    path = tmp_path / "config"
    path.write_text(CONFIG + "\nHost *.prod edge\n    User ops\n")
    win = AdvancedConfigWindow(None, _Client(path))
    win.go_to(host="edge", option="user", pin=True)        # nothing is loaded yet
    assert _pump(lambda: _selected(win) == "*.prod edge")   # a name beside a pattern
    assert win.target_entry.get_text() == "edge"            # pinned
    assert _pump(lambda: win.results.summary_rows["command"].get_subtitle() == "ssh edge")

    win.go_to(host="web2")                                  # the second name of a line
    assert _selected(win) == "web1 web2"
    win.go_to(host="nowhere")                               # says so, stays put
    assert _selected(win) == "web1 web2"
    win._closing = True
    win.destroy()


def test_a_line_of_the_saved_file_is_found_after_unsaved_edits(window):
    """Line numbers are of the file as saved: an edit above the line moves
    it in the draft, and it is still the block that is shown."""
    glob, web, star = window.draft.blocks
    saved_line = window._loaded_lines.headers[star.uid]
    window.add_option(web.uid)
    window.add_option(web.uid)                              # Host * is two lines lower now
    window.select_block(None)
    window.go_to(line=saved_line)
    assert _selected(window) == "*"
    window.go_to(line=window._loaded_lines.options[web.options[1].uid])   # an option's line
    assert _selected(window) == "web1 web2"


def test_a_line_no_block_owns_is_shown_as_text(window):
    window.go_to(line=1)                                    # "# my hosts": a comment
    assert window.results.stack.get_visible_child_name() == "raw"
    buffer = window.results.raw_view.get_buffer()
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
    assert _pump(lambda: len(win.draft.blocks) == 1)
    assert win.file_switcher.get_visible()                  # more than one file

    win.go_to(host="inc1", file=str(included))              # a connection's source file
    assert _pump(lambda: win._file == str(included) and len(win.draft.blocks) == 2)
    assert _selected(win) == "inc1"
    assert win.file_switcher.get_selected() == 1
    assert _pump(lambda: win.results.summary_rows["command"].get_subtitle() == "ssh inc1")
    assert [win.rules.cards[b.uid].status.get_label() for b in win.draft.blocks] == [
        "Matched", "",
    ]
    assert _effective(win, "port") == "2200"                # the root's line counts too

    inc = win.draft.blocks[0]
    _row(win, inc.options[0].uid).value.set_text("carol")
    assert _pump(lambda: win.dirty)
    win.save()
    assert _pump(lambda: client.saves == 1 and not win.dirty)
    assert included.read_text().startswith("Host inc1\n    User carol\n")
    assert "Include" in path.read_text()                    # the root was not written

    win.go_to(host="inc1", file=str(tmp_path / "unknown.conf"))   # not a file it reads:
    assert _pump(lambda: win._file == "" and _selected(win) == "inc1")   # the root's
    win._closing = True
    win.destroy()


# -- the Raw page and the rules are one place ------------------------------------

def _raw_marked(win):
    """The lines of the Raw page marked as the selected block's."""
    buffer = win.results.raw_view.get_buffer()
    tag = win.results._raw_block_tag
    marked = []
    for number in range(buffer.get_line_count()):
        found, start = buffer.get_iter_at_line(number)
        if found and start.has_tag(tag):
            end = start.copy()
            end.forward_to_line_end()
            marked.append(buffer.get_text(start, end, False))
    return marked


def test_the_raw_page_marks_the_selected_block_and_follows_edits(window):
    assert window.results.stack.get_page(
        window.results.stack.get_child_by_name("raw")
    ).get_title() == "Raw"
    _text_page(window)
    glob, web, star = window.draft.blocks
    window.select_block(web.uid)
    assert _raw_marked(window) == [
        "Host web1 web2", "    User deploy", "    Port 2222", "    IdentityFile ~/.ssh/a",
    ]
    window.select_block(star.uid)
    assert _raw_marked(window)[0] == "Host *"
    window.add_option(star.uid, "Compression", "no")        # the block grew
    assert _raw_marked(window)[-1].strip() == "Compression no"
    window.select_block(None)
    assert _raw_marked(window) == []


def test_the_cursor_on_the_raw_page_selects_the_block_it_is_in(window):
    buffer = _text_page(window)
    window.results.raw_view.has_focus = lambda: True         # the user is in it
    glob, web, star = window.draft.blocks
    line = window.editor.lines.options[star.options[0].uid]  # a line of Host *
    found, position = buffer.get_iter_at_line(line - 1)
    buffer.place_cursor(position)
    assert window.selected_uid == star.uid
    assert window._block_list.get_selected_row()._uid == star.uid
    assert _raw_marked(window)[0] == "Host *"

    # Text typed and not applied is not the rules' lines: nothing follows it.
    buffer.insert_at_cursor("x")
    assert window.results.raw_pending and _raw_marked(window) == []
    found, position = buffer.get_iter_at_line(window.editor.lines.headers[web.uid] - 1)
    buffer.place_cursor(position)
    assert window.selected_uid == star.uid


# -- answers are had ahead of time: a selection shows them at once -----------------

def test_a_selection_is_answered_in_the_same_step(gtk, tmp_path):
    """The hosts the configuration names are asked about in the background,
    so selecting one shows which blocks apply with no wait, a pattern block
    too; and an edit asks again rather than showing what no longer holds."""
    from sshpilot.advanced_config_window import AdvancedConfigWindow

    path = tmp_path / "config"
    path.write_text(CONFIG + "\nHost db1\n    User postgres\n")
    client = _Client(path)
    win = AdvancedConfigWindow(None, client)
    assert _pump(lambda: len(win.draft.blocks) == 4)
    glob, web, star, db = win.draft.blocks
    # web1, web2, db1, and the name made up for "Host *"
    assert _pump(lambda: len(win._answers) >= 4, seconds=10), win._answers.keys()

    asked = []
    real = client.evaluate_ssh_config_draft
    client.evaluate_ssh_config_draft = lambda request: asked.append(request.host) or real(request)
    command = win.results.summary_rows["command"]

    win.select_block(web.uid)                              # no pump: at once
    assert command.get_subtitle() == "ssh web1" and _dimmed(win) == ["db1"]
    win.select_block(db.uid)
    assert command.get_subtitle() == "ssh db1" and _dimmed(win) == ["web1 web2"]
    win.select_block(star.uid)                             # a pattern block
    assert [win.rules.cards[b.uid].status.get_label() for b in win.draft.blocks] == [
        "Always", "Matched", "Matched", "Matched",
    ]
    assert asked == []                                     # nothing was asked

    # The tooltip of a pill is kept, and given when the pointer asks.
    assert win.rules.cards[web.uid].status._tooltip

    win.select_block(web.uid)
    _row(win, web.options[0].uid).value.set_text("admin")  # another draft
    assert _pump(lambda: _effective(win, "user") == "admin")
    assert "web1" in asked                                 # asked again, for the new text
    win._closing = True
    win.destroy()


# -- folding blocks ------------------------------------------------------------------

def _open_blocks(win):
    """The blocks whose options are on screen (not folded to the header)."""
    return [
        block.patterns or block.kind for block in win.draft.blocks
        if win.rules.cards[block.uid].revealer.get_reveal_child()
    ]


def test_blocks_fold_when_the_user_folds_them_and_not_otherwise(window):
    glob, web, star = window.draft.blocks
    everything = ["global", "web1 web2", "*"]
    assert _open_blocks(window) == everything

    window.rules.cards[web.uid].fold_button.emit("clicked")      # the arrow
    assert _open_blocks(window) == ["global", "*"]
    assert window.rules.cards[web.uid].card.has_css_class("folded")
    window.select_block(web.uid)                                  # a selection
    window.select_block(star.uid)                                 # folds and opens nothing
    assert _open_blocks(window) == ["global", "*"]
    window.rules.cards[web.uid].fold_button.emit("clicked")
    assert _open_blocks(window) == everything

    window.activate_action("advcfg.collapse-all", None)           # the window's menu
    assert _open_blocks(window) == []
    window.go_to(host="web1", option="port")                      # asked for: it opens
    assert _pump(lambda: _open_blocks(window) == ["web1 web2"])
    window.activate_action("advcfg.expand-all", None)
    assert _open_blocks(window) == everything
    assert window.editor.text == CONFIG and not window.dirty      # a view of it only


def test_dragging_a_block_folds_the_others_while_it_lasts(window):
    glob, web, star = window.draft.blocks
    window.rules.set_folded(glob.uid, True)                       # folded before the drag
    window.rules.begin_block_drag(star.uid)
    assert _open_blocks(window) == ["*"]                          # only the dragged one
    window.rules.end_block_drag()
    assert _open_blocks(window) == ["web1 web2", "*"]             # as they were
    window.rules.end_block_drag()                                 # a second end is nothing
    assert _open_blocks(window) == ["web1 web2", "*"]


def test_where_a_drop_lands_is_shown_as_the_connection_list_shows_it(window):
    """The accent bar of the connection list, in a gap of its own: between
    the blocks for a block, between the options for an option. It is hidden
    when the drag is over and never taken out of the list during one."""
    from sshpilot.sidebar import DragIndicator

    rules = window.rules
    glob, web, star = window.draft.blocks
    cards = {block.uid: rules.cards[block.uid] for block in window.draft.blocks}

    def shown():
        return [mark for mark in rules._drop_marks.values() if mark.get_visible()]

    rules.begin_block_drag(star.uid)
    # (A window that is not on screen has no heights: "above the middle" is -1.)
    rules.on_card_motion(None, 0, -1, cards[web.uid])             # upper half: before it
    (bar,) = shown()
    assert isinstance(bar, DragIndicator) and bar.get_parent() is rules.box
    assert bar.get_next_sibling() is cards[web.uid].card
    rules.on_card_motion(None, 0, 10_000, cards[web.uid])         # lower half: after it
    assert shown() == [bar] and bar.get_prev_sibling() is cards[web.uid].card
    rules.end_block_drag()
    assert shown() == [] and bar.get_parent() is rules.box        # hidden, not removed

    rules.begin_option_drag()
    row = cards[web.uid].rows[web.options[1].uid]
    rules.on_row_motion(None, 0, -1, row)                         # between two options
    (option_bar,) = shown()
    assert option_bar.get_parent() is cards[web.uid].rows_box
    assert option_bar.get_next_sibling() is row.box
    rules.on_card_motion(None, 0, 5, cards[star.uid])             # onto a block: its last
    (option_bar,) = shown()
    assert option_bar.get_parent() is cards[star.uid].rows_box
    assert option_bar.get_next_sibling() is cards[star.uid].footer
    rules.end_option_drag()
    assert shown() == []
    assert window.editor.text == CONFIG                           # showing moved nothing


def test_undo_and_redo_have_keys_wherever_the_keyboard_is(window):
    """Ctrl+Z is the window's undo, not the field's it was pressed in; on
    the Raw page it is left to the text typed there."""
    names = lambda: [b.patterns or b.kind for b in window.draft.blocks]  # noqa: E731
    glob, web, star = window.draft.blocks
    window.delete_block(star.uid)
    assert names() == ["global", "web1 web2"]
    assert window._history_key(window.undo) is True
    assert names() == ["global", "web1 web2", "*"] and window.editor.text == CONFIG
    assert window._history_key(window.redo) is True
    assert names() == ["global", "web1 web2"]
    window.undo()

    raw = window.results.raw_view
    window.get_focus = lambda: raw                         # the keyboard is in the text
    window.delete_block(window.draft.blocks[2].uid)
    assert window._history_key(window.undo) is False       # the text's own undo
    assert names() == ["global", "web1 web2"]
    assert "Ctrl+Z" in window.undo_button.get_tooltip_text()


# -- review, 2026-11-24 ---------------------------------------------------------------

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
    assert _pump(lambda: len(win.draft.blocks) == 1 and len(win._files) == 2)
    _pump(lambda: not win._prefetching and win._prefetch_source == 0, seconds=5)

    gate = threading.Event()
    real = client.evaluate_ssh_config_draft

    def slow(request):                                    # the save's question waits
        if request.host == "sshpilot-config-check":
            gate.wait(5)
        return real(request)

    client.evaluate_ssh_config_draft = slow
    _row(win, win.draft.blocks[0].options[0].uid).value.set_text("root")
    assert _pump(lambda: win.dirty)
    win.save()
    assert win._saving
    # While OpenSSH is being asked, the operator opens the other file and
    # answers "Discard" to the question about the unsaved changes.
    win._confirm_discard = lambda proceed, stay=None: proceed()
    win.open_file(str(included))
    win.go_to(host="inc", file=str(included))             # and from elsewhere in the app
    _pump(lambda: False, seconds=0.6)
    gate.set()
    _pump(lambda: client.saves >= 1 and not win._saving, seconds=5)
    assert included.read_text() == "Host inc\n    User alice\n"   # not written over
    assert path.read_text() == root_text.replace("User me", "User root")
    assert client.saves == 1
    # The place asked for meanwhile is gone to now that the save is over.
    assert _pump(lambda: win._file == str(included))
    win._closing = True
    win.destroy()


def test_agreeing_to_run_a_command_is_for_the_host_on_screen_only(window, tmp_path):
    """Review P1: "Run and Evaluate" is an agreement for one host. The
    hosts asked about ahead of time, a pattern block's hosts and the
    save's check must not carry it: the command (think %h) would run for
    hosts nobody chose."""
    client = window._test_client
    asked = []
    real = client.evaluate_ssh_config_draft

    def record(request):
        asked.append((request.host, request.allowed_exec))
        return real(request)

    client.evaluate_ssh_config_draft = record
    marker = tmp_path / "ran-for"
    window.add_block("match")
    block = window.draft.blocks[-1]
    window.rules.cards[block.uid].patterns.set_text(f'exec "echo %h >> {marker}"')
    assert _pump(lambda: window.banner.get_revealed() and bool(window.banner.get_button_label()))
    window.banner.emit("button-clicked")                  # agreed: for web1
    assert _pump(marker.exists)
    assert _settled(window)
    _pump(lambda: False, seconds=1.2)                     # let the background asking run

    window.target_entry.set_text("")                      # no host typed any more
    _pump(lambda: False, seconds=0.5)
    window.select_block(window.draft.blocks[2].uid)       # Host *: a pattern block
    _pump(lambda: False, seconds=1.0)
    assert window.banner.get_revealed() and not window.banner.get_button_label()  # no button
    window.save()
    assert _pump(lambda: client.saves == 1)

    carried = {host for host, allowed in asked if allowed}
    assert carried == {"web1"}, carried
    assert set(marker.read_text().split()) == {"web1"}    # it ran for web1 and no one else


def test_reload_after_a_save_conflict_asks_before_dropping_the_draft(window):
    """Review P2: the conflict banner's Reload replaced the draft in one
    click, the draft the save was meant to keep."""
    client = window._test_client
    web = window.draft.blocks[1]
    _row(window, web.options[0].uid).value.set_text("mine")
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
    assert "mine" in window.editor.text                   # the draft is still here
    asked[0]()                                            # "Discard"
    assert _pump(lambda: "edited elsewhere" in window.editor.text)


# -- review of the window's use, 2026-11-24 ----------------------------------------

def test_the_consent_prompt_says_what_would_run_and_for_whom(window, tmp_path):
    marker = tmp_path / "ran"
    window.add_block("match")
    block = window.draft.blocks[-1]
    command = f"touch {marker} # for %h"
    window.rules.cards[block.uid].patterns.set_text(f'exec "{command}"')
    assert _pump(lambda: window.banner.get_revealed() and bool(window.banner.get_button_label()))
    said = window.banner.get_title()
    assert command in said and "web1" in said             # the command itself, and the host
    assert not window.banner.get_use_markup()             # shown as it is written
    assert not marker.exists()                            # nothing ran to say so


def test_copy_takes_the_text_on_the_raw_page_when_it_is_the_newer(window):
    copied = []
    window.get_clipboard = lambda: type("Clip", (), {"set": lambda _s, text: copied.append(text)})()
    window._on_copy()
    assert copied[-1] == CONFIG
    buffer = _text_page(window)
    buffer.insert(buffer.get_end_iter(), "\nHost fix\n    User urgent\n")
    assert window.results.raw_pending
    window._on_copy()
    assert copied[-1].endswith("Host fix\n    User urgent\n")
    assert window.results.raw_pending                     # copying applied nothing
    assert "Host fix" not in window.editor.text


def test_a_source_in_an_included_file_is_gone_to_and_back(gtk, tmp_path):
    from sshpilot.advanced_config_window import AdvancedConfigWindow

    included = tmp_path / "hosts.conf"
    included.write_text("Host web1\n    User fromfile\n")
    path = tmp_path / "config"
    path.write_text(f"Include {included}\n\nHost web1\n    Port 2222\n")
    win = AdvancedConfigWindow(None, _Client(path, included=[included]))
    assert _pump(lambda: len(win.draft.blocks) == 2)      # the Include line, and Host web1
    win.select_block(win.draft.blocks[-1].uid)
    assert _pump(lambda: _effective(win, "user") == "fromfile")
    user = win.results._items["user"]
    assert (user.file, user.file_line) == (str(included), 2)
    assert win.source_file(user.file) == str(included)    # one of this configuration's
    assert win.source_file("/etc/ssh/ssh_config") is None

    win.results.fill_all()                                # the Includes page too
    rows = [row for row in _children(win.results.include_list) if row.get_activatable()]
    assert [row.get_title() for row in rows] == [str(included)]

    win.reveal_source(user.file, user.file_line)          # the source, clicked
    assert _pump(lambda: win._file == str(included) and _selected(win) == "web1")
    assert win.target_text() == "web1"                    # still the host read about
    # Once the answer for this file is in: from here, port comes from the root.
    assert _pump(lambda: _effective(win, "port") == "2222"
                 and win.source_file(win.results._items["port"].file) == "")
    port = win.results._items["port"]
    win.reveal_source(port.file, port.file_line)
    assert _pump(lambda: win._file == "" and _selected(win) == "web1")
    win._closing = True
    win.destroy()


def test_embedded_the_panes_take_turns_and_the_list_is_left_to_the_host(window, shell):
    """The list of blocks is the host's to show: the layouts never hide it
    or make room for it."""
    if shell != "embedded":
        pytest.skip("the window's own sidebar is the next test's")

    def shown():
        return tuple(
            name for name, widget in (("rules", window.rules), ("results", window.results))
            if widget.get_visible()
        )

    column = window.sidebar_column
    assert not window.sidebar_button.get_visible()
    assert shown() == ("rules", "results")
    window._set_layout("narrow")
    assert shown() == ("rules", "results") and column.get_visible()
    window._set_layout("single")
    assert shown() == ("rules",) and window.pane_switch.get_visible()
    window.results_tab.set_active(True)
    assert shown() == ("results",) and column.get_visible()
    window.go_to(host="web1")
    assert shown() == ("rules",)
    window._set_layout("wide")
    assert shown() == ("rules", "results") and column.get_visible()


def test_a_narrow_window_closes_the_sidebar_and_then_shows_one_pane(window, shell):
    if shell == "embedded":
        pytest.skip("embedded, the list of blocks is in the host's sidebar")

    def shown():
        return tuple(
            name for name, widget in (
                ("sidebar", window.sidebar), ("rules", window.rules), ("results", window.results)
            ) if widget.get_visible()
        )

    assert shown() == ("sidebar", "rules", "results") and not window.pane_switch.get_visible()
    window._set_layout("narrow")                          # no room for all three
    assert shown() == ("rules", "results")
    window.sidebar_button.set_active(True)                # opened: in the results' place
    assert shown() == ("sidebar", "rules")
    window.sidebar_button.set_active(False)

    window._set_layout("single")                          # no room for two
    assert shown() == ("rules",) and window.pane_switch.get_visible()
    window.results_tab.set_active(True)
    assert shown() == ("results",) and not window.sidebar_button.get_sensitive()
    window.go_to(host="web1")                             # something in the rules is asked for
    assert shown() == ("rules",)
    window.go_to(line=1)                                  # a comment: shown as text
    assert shown() == ("results",)

    window._set_layout("wide")                            # room again: as it was left
    assert shown() == ("sidebar", "rules", "results") and not window.pane_switch.get_visible()


def test_the_window_says_when_it_closes(window):
    """Whoever opened it must learn that it closed, to open a new one next
    time. GTK's "destroy" does not tell them: it waits for the last
    reference, and they hold one. Showing the closed window again gave one
    whose background work no longer reported back."""
    closed = []
    window.add_closed_listener(lambda: closed.append(1))
    web = window.draft.blocks[1]
    _row(window, web.options[0].uid).value.set_text("unsaved")
    assert _pump(lambda: window.dirty)
    asked = []
    window._confirm_discard = lambda proceed, stay=None: asked.append(proceed)
    # (The close request itself: a window that is not on screen ignores close().)
    assert window._on_close_request() is True     # unsaved changes: asked first, kept open
    assert asked and closed == []                 # not closed yet
    asked[0]()                                    # "Discard": closes for good
    assert window._on_close_request() is False
    assert closed == [1]
    assert window._on_close_request() is False    # a second close says nothing more
    assert closed == [1]


# -- a block begun from the connection editor --------------------------------

def test_a_block_handed_over_is_added_unsaved_with_its_host_line_and_options(window):
    win = window
    before = len(win.draft.blocks)
    win.start_block("*.prod !bastion", [("HostName", "%h.example.com"), ("User", "deploy")])
    assert _pump(lambda: len(win.draft.blocks) == before + 1)
    block = win.draft.blocks[-1]
    assert block.patterns == "*.prod !bastion"
    assert [(o.key, o.value) for o in block.options] == [
        ("HostName", "%h.example.com"), ("User", "deploy"),
    ]
    assert win.selected_uid == block.uid
    assert win._test_client.saves == 0
    assert "Host *.prod !bastion\n" in win.editor.text


def test_a_block_handed_over_before_the_file_is_read_waits_for_it(gtk, tmp_path):
    from sshpilot.advanced_config_window import AdvancedConfigWindow

    path = tmp_path / "config"
    path.write_text(CONFIG)
    win = AdvancedConfigWindow(None, _Client(path))
    try:
        win.start_block("*.lab", [])
        assert _pump(lambda: bool(win.draft.blocks) and win.draft.blocks[-1].patterns == "*.lab")
        # Nothing typed beside the pattern: the block keeps its one empty option.
        assert [(o.key, o.value) for o in win.draft.blocks[-1].options] == [("", "")]
    finally:
        win._closing = True
        win.destroy()


def test_the_block_list_names_each_block_and_says_what_it_is(gtk, tmp_path):
    """Rows like the main sidebar's: an icon, the name, and a caption with
    the kind of block."""
    from sshpilot.advanced_config_window import AdvancedConfigWindow

    path = tmp_path / "config"
    path.write_text(
        "Compression yes\n\nHost web1\n    User a\n\nHost db1 db2\n    User b\n\n"
        "Match host *.corp\n    ProxyJump gate\n\nHost edge *.edge\n    Port 2222\n\n"
        "# [disabled] Host *.old\n# [disabled]     User gone\n\nHost *\n    User z\n"
    )
    win = AdvancedConfigWindow(None, _Client(path))
    try:
        assert _pump(lambda: len(win._block_rows) == 7)
        rows = [win._block_rows[block.uid] for block in win.editor.blocks]
        assert [(r.title.get_label(), r.caption.get_label()) for r in rows] == [
            ("Global Options", "All hosts"),
            ("web1", "Host"),
            ("db1 db2", "2 hosts"),
            ("Match host *.corp", "Match rule"),
            ("edge *.edge", "Host pattern"),
            ("*.old", "Host pattern · off"),
            ("Host *", "All hosts"),
        ]
        icons = [r.icon.get_icon_name() for r in rows]
        assert icons[1] == icons[2] and len(set(icons)) == 4     # one per kind
        assert win._all_row.title.get_label() == "All Blocks"
        assert not win._all_row.caption.get_visible()
        # Relabelled in place when the line changes.
        web1 = win.editor.blocks[1]
        win.editor.set_patterns(web1.uid, "web*")
        win._sync_block_list()
        assert rows[1].caption.get_label() == "Host pattern"
        assert win._block_rows[web1.uid] is rows[1]
    finally:
        win._closing = True
        win.destroy()


# -- the search above the list of blocks ---------------------------------------------

def _shown_rows(win):
    """The rows of the list the search leaves on screen, as they read."""
    win._block_list.invalidate_filter()
    return [
        row._text for row in _children(win._block_list)
        if hasattr(row, "_text") and row.get_child_visible()
    ]


def test_the_field_is_a_search_of_the_blocks(gtk, tmp_path):
    """Typing finds blocks by name or address, selects the best of them, and
    is not a host to ask about: a name no block has does nothing."""
    from sshpilot.advanced_config_window import AdvancedConfigWindow

    path = tmp_path / "config"
    path.write_text(
        "Host web1 web2\n    HostName 10.0.0.5\n    User deploy\n\n"
        "Host db1\n    HostName db.internal\n    ProxyJump web1\n\n"
        "Host *.prod\n    User ops\n\nHost *\n    User fallback\n"
    )
    win = AdvancedConfigWindow(None, _Client(path))
    try:
        win.present()
        assert _pump(lambda: len(win._block_rows) == 4)
        web, db, prod, star = win.editor.blocks
        assert win.target_entry.get_placeholder_text() == "Search blocks"

        def typed(text):
            win.target_entry.set_text(text)
            _pump(lambda: False, seconds=0.5)

        typed("db")                                   # by name
        assert _shown_rows(win) == ["Host db1"]
        assert win.selected_uid == db.uid             # found: selected, its config shown
        assert _pump(lambda: win.results.summary_rows["command"].get_subtitle() == "ssh db1")

        typed("10.0.0")                               # by address (HostName)
        assert _shown_rows(win) == ["Host web1 web2"] and win.selected_uid == web.uid

        typed("deploy")                               # not by any other directive
        assert _shown_rows(win) == []
        typed("web1")                                 # nor by one that mentions the host
        assert _shown_rows(win) == ["Host web1 web2"]

        typed("web2")                                 # the second name of a line
        assert win.selected_uid == web.uid
        assert _pump(lambda: win.results.summary_rows["command"].get_subtitle() == "ssh web2")

        # A name no block has: nothing is found, selected or asked about.
        asked = win._request_serial
        typed("nowhere")
        assert _shown_rows(win) == []
        assert win.selected_uid == web.uid and win.target_text() == "web1"
        assert win._typed_host() == ""
        _pump(lambda: False, seconds=0.5)
        assert win.results.summary_rows["command"].get_subtitle() == "ssh web1"
        assert asked == win._request_serial or win.target_text() == "web1"

        typed("")                                     # the search ends: every block again
        assert _shown_rows(win) == ["", "Host web1 web2", "Host db1", "Host *.prod", "Host *"]
        assert win.selected_uid == web.uid
    finally:
        win._closing = True
        win.destroy()
