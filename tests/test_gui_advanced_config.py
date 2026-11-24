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


def _filter_by(win, host):
    """Type a host in the sidebar and wait for OpenSSH's answer on it."""
    assert _pump(lambda: bool(win.draft.blocks))
    win.target_entry.set_text(host)
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
    return [row._text for row in _children(win._block_list)]


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
    window.target_entry.set_text("db1")
    assert _pump(lambda: _effective(window, "user") == "fallback")
    # The block OpenSSH skips for the host folds, where it is.
    assert _pump(lambda: _dimmed(window) == ["web1 web2"])
    web = window.draft.blocks[1]
    assert [_status(window, o) for o in web.options] == ["Inactive"] * 3
    window.target_entry.set_text("web1")
    assert _pump(lambda: _dimmed(window) == [] and _effective(window, "user") == "deploy")
    window.target_entry.set_text("")           # no host: every block open
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
    window.target_entry.set_text("db1")        # another host starts afresh
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
