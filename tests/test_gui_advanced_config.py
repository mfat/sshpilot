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
    def __init__(self, path):
        self.path = path
        self.saves = 0

    @staticmethod
    def _revision(text):
        return "r" + hashlib.sha1(text.encode()).hexdigest()[:12]

    def get_ssh_config_text(self):
        from sshpilot.api.models.connections import SshConfigText

        text = self.path.read_text()
        return SshConfigText(text, self._revision(text), "config", True)

    def save_ssh_config_text(self, request):
        from sshpilot.api.errors import ErrorCode, SshPilotError
        from sshpilot.api.models.connections import SshConfigText

        if request.expected_revision != self._revision(self.path.read_text()):
            raise SshPilotError(ErrorCode.STALE_EDITOR, "stale")
        self.path.write_text(request.text)
        self.saves += 1
        return SshConfigText(request.text, self._revision(request.text), "config", True)

    def evaluate_ssh_config_draft(self, request):
        from sshpilot.core.ssh_config_draft import DraftContext, evaluate_draft

        context = DraftContext(
            str(self.path), isolated=True, overrides=("-o", "ConnectTimeout=7")
        )
        return evaluate_draft(request, context, self.path.read_text())


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
    win = AdvancedConfigWindow(None, client, hosts=lambda: ["db1"])
    win._test_client = client
    _filter_by(win, "web1")
    yield win
    win._closing = True
    win.destroy()


def _filter_by(win, host):
    """Type a host into the filter and wait for OpenSSH's answer on it."""
    assert _pump(lambda: bool(win.draft.blocks))
    win.target_entry.set_text(host)
    assert _pump(lambda: (
        win._evaluation is not None and win._evaluation.evaluated
        and win._evaluated_serial == win.editor.serial
        and win.results.summary_rows["command"].get_subtitle() == f"ssh {host}"
    ))


def _shown(win):
    return [
        block.patterns or block.kind for block in win.draft.blocks
        if win.rules.cards[block.uid].card.get_visible()
    ]


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
    assert _shown(win) == ["global", "web1 web2", "*"]
    assert not any(
        win.rules.cards[b.uid].status.get_visible() for b in win.draft.blocks
    )
    win._closing = True
    win.destroy()


def test_filtering_by_a_host_shows_openssh_s_answer(window):
    assert [row._host for row in _children(window._host_list)] == [
        "web1", "web2", "db1",
    ]
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
    # The filter leaves the blocks OpenSSH applies to the host.
    assert _pump(lambda: _shown(window) == ["global", "*"])
    web = window.draft.blocks[1]
    assert [_status(window, o) for o in web.options] == ["Inactive"] * 3
    window.target_entry.set_text("web1")
    assert _pump(lambda: _shown(window) == ["global", "web1 web2", "*"])
    window.target_entry.set_text("")           # no filter: every block
    assert _pump(lambda: window._evaluation is None)
    assert _shown(window) == ["global", "web1 web2", "*"]


def test_a_block_being_worked_on_stays_while_filtering(window):
    """Adding or editing a block must not make it vanish under the filter."""
    window.add_block("host")
    block = window.draft.blocks[-1]
    window.rules.cards[block.uid].patterns.set_text("elsewhere")
    assert _settled(window)
    assert window.rules.cards[block.uid].status.get_label() == "Skipped"
    assert "elsewhere" in _shown(window)
    window.target_entry.set_text("db1")        # a new filter starts afresh
    assert _pump(lambda: _shown(window) == ["global", "*"])


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
    assert "web1 web2" in _shown(window)        # just switched off: still here


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


def test_the_target_dropdown_lists_hosts_and_sets_the_target(window):
    rows = _children(window._host_list)
    window._host_list.emit("row-activated", rows[2])
    assert window.target_entry.get_text() == "db1"
    assert _pump(lambda: _effective(window, "user") == "fallback")
    # A host named by a new block is offered too.
    window.add_block("host")
    block = window.draft.blocks[-1]
    window.rules.cards[block.uid].patterns.set_text("cache1 *.internal")
    assert [row._host for row in _children(window._host_list)] == [
        "web1", "web2", "cache1", "db1",
    ]


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


def test_selecting_a_host_s_block_loads_it_without_narrowing_the_rules(gtk, tmp_path):
    """With no filter, clicking a block asks OpenSSH about the host it names
    and shows the answer on the right; every block stays on screen."""
    from sshpilot.advanced_config_window import AdvancedConfigWindow

    path = tmp_path / "config"
    path.write_text(CONFIG + "\nHost db1\n    User postgres\n")
    win = AdvancedConfigWindow(None, _Client(path))
    assert _pump(lambda: len(win.draft.blocks) == 4)
    _glob, web, star, db = win.draft.blocks
    everything = ["global", "web1 web2", "*", "db1"]

    win.select_block(web.uid)
    assert _pump(lambda: win.results.summary_rows["command"].get_subtitle() == "ssh web1")
    assert _effective(win, "user") == "deploy"
    assert _shown(win) == everything                      # nothing is hidden
    assert win.target_entry.get_text() == ""              # the filter is untouched
    assert win.rules.cards[db.uid].status.get_label() == "Skipped"

    win.select_block(db.uid)
    assert _pump(lambda: win.results.summary_rows["command"].get_subtitle() == "ssh db1")
    assert _effective(win, "user") == "fallback"          # Host * comes first
    assert _shown(win) == everything

    win.select_block(star.uid)                            # a pattern names no host
    assert _pump(lambda: win._evaluation is None)
    assert not win.results.summary.get_visible() and _shown(win) == everything

    win.target_entry.set_text("web2")                     # the filter wins, and narrows
    assert _pump(lambda: win.results.summary_rows["command"].get_subtitle() == "ssh web2")
    assert _pump(lambda: _shown(win) == ["global", "web1 web2", "*"])
    win._closing = True
    win.destroy()


def test_renaming_the_selected_block_s_host_follows_it(gtk, tmp_path):
    from sshpilot.advanced_config_window import AdvancedConfigWindow

    path = tmp_path / "config"
    path.write_text(CONFIG)
    win = AdvancedConfigWindow(None, _Client(path))
    assert _pump(lambda: len(win.draft.blocks) == 3)
    web = win.draft.blocks[1]
    win.select_block(web.uid)
    assert _pump(lambda: win.results.summary_rows["command"].get_subtitle() == "ssh web1")
    win.rules.cards[web.uid].patterns.set_text("edge9 web2")
    assert _pump(lambda: win.results.summary_rows["command"].get_subtitle() == "ssh edge9")
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
    def get_ssh_config_text(self):
        from sshpilot.api.models.connections import SshConfigText

        text = self.path.read_text()
        return SshConfigText(text, self._revision(text), "config", False)


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
