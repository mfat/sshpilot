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
        and win._evaluated_serial == win._serial
        and win._summary_rows["command"].get_subtitle() == f"ssh {host}"
    ))


def _shown(win):
    return [
        block.patterns or block.kind for block in win.draft.blocks
        if win._block_widgets[block.uid]["card"].get_visible()
    ]


def _settled(win):
    return _pump(lambda: (
        win._evaluate_source == 0
        and win._evaluation is not None
        and win._evaluated_serial == win._serial
    ))


def _effective(win, key):
    for row in win._children(win._effective_list):
        if row.get_title() == key:
            return row.get_subtitle()
    return None


def _status(win, option):
    pill = win._option_widgets[option.uid]["status"]
    return pill.get_label() if pill.get_visible() else None


def test_it_opens_showing_every_block_and_asks_nothing(gtk, tmp_path):
    from sshpilot.advanced_config_window import AdvancedConfigWindow

    path = tmp_path / "config"
    path.write_text(CONFIG)
    win = AdvancedConfigWindow(None, _Client(path))
    assert _pump(lambda: len(win.draft.blocks) == 3)
    assert win.target_entry.get_text() == "" and win._evaluation is None
    assert _shown(win) == ["global", "web1 web2", "*"]
    assert not any(
        win._block_widgets[b.uid]["status"].get_visible() for b in win.draft.blocks
    )
    win._closing = True
    win.destroy()


def test_filtering_by_a_host_shows_openssh_s_answer(window):
    assert [row._host for row in window._children(window._host_list)] == [
        "web1", "web2", "db1",
    ]
    assert not window.dirty and not window.save_button.get_sensitive()
    glob, web, star = window.draft.blocks
    assert [window._block_widgets[b.uid]["status"].get_label() for b in (glob, web, star)] == [
        "Always", "Matched", "Matched",
    ]
    assert [_status(window, o) for o in web.options] == ["Applied", "Applied", "Accumulated"]
    assert [_status(window, o) for o in star.options] == ["Shadowed", "Overridden"]
    assert _effective(window, "user") == "deploy"
    assert _effective(window, "connecttimeout") == "7"      # Settings beat the file
    assert window._summary_rows["command"].get_subtitle() == "ssh web1"
    assert window._summary_rows["host"].get_subtitle() == "web1 : 2222"


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
    window._block_widgets[block.uid]["patterns"].set_text("elsewhere")
    assert _settled(window)
    assert window._block_widgets[block.uid]["status"].get_label() == "Skipped"
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
    assert not window.add_global_button.get_sensitive()   # there is one already


def test_typing_a_value_re_evaluates_without_redrawing(window):
    web = window.draft.blocks[1]
    entry = window._option_widgets[web.options[0].uid]["value"]
    entry.set_text("root")
    assert _pump(lambda: _effective(window, "user") == "root")
    assert window._option_widgets[web.options[0].uid]["value"] is entry
    assert "    User root\n" in window._text


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
    window._block_widgets[block.uid]["patterns"].set_text(f'exec "touch {marker}"')
    assert _pump(lambda: window.banner.get_revealed())
    assert window.banner.get_button_label() and not marker.exists()
    window.banner.emit("button-clicked")
    assert _pump(marker.exists)
    # The answer that follows the consent, not the one that asked for it.
    assert _pump(lambda: window._evaluation is not None and window._evaluation.evaluated)
    assert window._block_widgets[block.uid]["status"].get_label() == "Matched"


def test_disabling_a_block_writes_comments_and_openssh_skips_it(window):
    web = window.draft.blocks[1]
    window.toggle_block(web.uid)
    assert _settled(window) and _effective(window, "user") == "fallback"
    assert "# [disabled] Host web1 web2\n" in window._text
    web = window.draft.blocks[1]
    assert window._block_widgets[web.uid]["status"].get_label() == "Disabled"
    assert "web1 web2" in _shown(window)        # just switched off: still here


def test_options_move_between_blocks(window):
    web, star = window.draft.blocks[1], window.draft.blocks[2]
    port = web.options[1]
    assert window.move_option(port.uid, star.uid, 0)
    assert [o.key for o in window.draft.blocks[2].options][0] == "Port"
    assert _settled(window) and _effective(window, "port") == "2222"


def test_text_page_edits_are_applied_to_the_rules(window):
    buffer = window.raw_view.get_buffer()
    assert buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False) == CONFIG
    buffer.set_text("Host web1\n    User typed\n")
    assert window.apply_raw_button.get_sensitive()
    window.apply_raw()
    assert _settled(window) and _effective(window, "user") == "typed"
    assert [b.patterns for b in window.draft.blocks] == ["web1"]


def test_save_writes_only_what_changed_and_clears_dirty(window):
    client = window._test_client
    web = window.draft.blocks[1]
    window._option_widgets[web.options[0].uid]["value"].set_text("root")
    assert _pump(lambda: window.dirty)
    window.save()
    assert _pump(lambda: client.saves == 1 and not window.dirty)
    assert client.path.read_text() == CONFIG.replace("User deploy", "User root")


def test_a_file_changed_on_disk_is_not_overwritten(window):
    client = window._test_client
    client.path.write_text(CONFIG + "# edited elsewhere\n")
    web = window.draft.blocks[1]
    window._option_widgets[web.options[0].uid]["value"].set_text("root")
    assert _pump(lambda: window.dirty)
    window.save()
    assert _pump(lambda: window.banner.get_revealed() and bool(window.banner.get_button_label()))
    assert client.saves == 0 and "edited elsewhere" in client.path.read_text()


def test_the_target_dropdown_lists_hosts_and_sets_the_target(window):
    rows = window._children(window._host_list)
    window._host_list.emit("row-activated", rows[2])
    assert window.target_entry.get_text() == "db1"
    assert _pump(lambda: _effective(window, "user") == "fallback")
    # A host named by a new block is offered too.
    window.add_block("host")
    block = window.draft.blocks[-1]
    window._block_widgets[block.uid]["patterns"].set_text("cache1 *.internal")
    assert [row._host for row in window._children(window._host_list)] == [
        "web1", "web2", "cache1", "db1",
    ]


def test_agreeing_to_a_command_does_not_cover_an_edited_one(window, tmp_path):
    """Review P1: consent is for the command shown, and for this target."""
    first, second = tmp_path / "first", tmp_path / "second"
    window.add_block("match")
    block = window.draft.blocks[-1]
    entry = window._block_widgets[block.uid]["patterns"]
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
    window.raw_view.get_buffer().set_text("Host web1\n    User typed\n")
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
    window._option_widgets[web.options[0].uid]["value"].set_text("root")
    assert _pump(lambda: window.dirty)
    newer = CONFIG.replace("User deploy", "User newest")
    window.raw_view.get_buffer().set_text(newer)
    window.save()
    assert _pump(lambda: client.saves == 1)
    assert client.path.read_text() == newer
