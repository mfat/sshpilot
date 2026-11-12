"""The shared SSH option list, and the connection dialog's Advanced tab on it."""
from __future__ import annotations

import shutil

import pytest

pytestmark = pytest.mark.gui


def _list(gui, **kwargs):
    from sshpilot.ssh_option_list import SshOptionList

    refused = kwargs.pop("refused", {})
    kwargs.setdefault(
        "validate", lambda keyword, value: refused.get(keyword.lower())
    )
    kwargs.setdefault("keywords", ("Compression", "IPQoS", "SetEnv"))
    return SshOptionList(**kwargs)


def _finish(options, row, keyword, value):
    row.keyword_entry.set_text(keyword)
    row.value_entry.set_text(value)
    options._finish(row)


def test_rows_report_only_finished_valid_options(gui):
    options = _list(gui, refused={"badone": "nope"})
    seen = []
    options.connect("changed", lambda _o: seen.append(list(options.get_options())))

    row = options.add_empty_row()
    row.keyword_entry.set_text("IPQoS")
    options._finish(row)                       # half an option: flagged, reports nothing
    assert options.get_options() == [] and row.error and seen == []

    _finish(options, row, "IPQoS", "none")
    assert seen == [[("IPQoS", "none")]]

    bad = options.add_empty_row()
    _finish(options, bad, "BadOne", "x")
    assert bad.error == "nope" and bad.error_label.get_visible()
    assert options.has_errors()
    assert options.get_options() == [("IPQoS", "none")]
    assert len(seen) == 1                      # nothing usable changed

    _finish(options, bad, "SetEnv", "A=b")     # fixed
    assert bad.error is None and not bad.error_label.get_visible()
    assert seen[-1] == [("IPQoS", "none"), ("SetEnv", "A=b")]

    options.remove(row)
    assert seen[-1] == [("SetEnv", "A=b")]


def test_any_keyword_can_be_typed_and_known_ones_get_their_spelling(gui):
    options = _list(gui)
    row = options.add_empty_row()
    _finish(options, row, "compression", "yes")
    assert row.keyword_entry.get_text() == "Compression"
    other = options.add_empty_row()
    _finish(options, other, "SomethingNewer", "1")
    assert options.get_options() == [("Compression", "yes"), ("SomethingNewer", "1")]


def test_loaded_rows_are_trusted_until_edited(gui):
    """What is already saved must never be dropped because a check dislikes it."""
    options = _list(gui, refused={"oldthing": "refused"})
    seen = []
    options.connect("changed", lambda _o: seen.append(1))
    options.set_options([("OldThing", "yes"), ("IPQoS", "none")])
    assert seen == []
    assert options.commit_all() is True
    assert options.get_options() == [("OldThing", "yes"), ("IPQoS", "none")]

    row = options.rows()[0]
    row.value_entry.set_text("no")             # now the user touched it
    assert options.commit_all() is False
    assert row.error == "refused"
    # The refused edit changes nothing: the row still stands for what was saved.
    assert options.get_options() == [("OldThing", "yes"), ("IPQoS", "none")]
    assert seen == []


def test_edited_fires_on_every_keystroke(gui):
    options = _list(gui)
    edits = []
    options.connect("edited", lambda _o: edits.append(1))
    row = options.add_empty_row()
    row.keyword_entry.set_text("S")
    row.keyword_entry.set_text("Se")
    assert len(edits) >= 2
    # Uncommitted rows are still readable live.
    row.value_entry.set_text("x")
    assert options.get_options() == [("Se", "x")]


def test_check_ssh_option_words_each_refusal():
    from sshpilot.ssh_option_list import check_ssh_option

    kwargs = dict(reserved={"connecttimeout"}, reserved_message="{keyword} has a row")
    ok = lambda text: None
    assert check_ssh_option("IPQoS", "none", parse=ok, **kwargs) is None
    assert check_ssh_option("ConnectTimeout", "5", parse=ok, **kwargs) == "ConnectTimeout has a row"
    assert "Match" in check_ssh_option("Match", "all", parse=ok, **kwargs)
    assert check_ssh_option("-F", "/etc/passwd", parse=ok, **kwargs)
    assert check_ssh_option("Bad-Key", "x", parse=ok, **kwargs)
    seen = []
    rejected = check_ssh_option(
        "IPQoS", "none", parse=lambda text: seen.append(text) or "line 2: Bad", **kwargs
    )
    assert "line 2: Bad" in rejected
    assert seen == ["Host *\n    IPQoS none\n"]


@pytest.mark.skipif(shutil.which("ssh") is None, reason="needs OpenSSH")
def test_check_ssh_option_asks_the_real_openssh():
    from sshpilot.ssh_option_list import check_ssh_option

    kwargs = dict(reserved=(), reserved_message="{keyword}")
    assert check_ssh_option("IPQoS", "none", **kwargs) is None
    assert check_ssh_option("ProxyCommand", "nc %h %p", **kwargs) is None
    assert check_ssh_option("NoSuchOptionAtAll", "yes", **kwargs)
    assert check_ssh_option("Compression", "maybe", **kwargs)


def test_keyword_menu_list_is_options_only():
    from sshpilot.ssh_option_list import OPENSSH_KEYWORDS

    lowered = {name.lower() for name in OPENSSH_KEYWORDS}
    assert not lowered & {"host", "match", "include"}
    assert {"channeltimeout", "obscurekeystroketiming", "proxyjump"} <= lowered
    assert len(lowered) == len(OPENSSH_KEYWORDS)


# -- the connection dialog's Advanced tab -----------------------------------


def _tab(gui):
    from sshpilot.connection_dialog import SSHConfigAdvancedTab

    return SSHConfigAdvancedTab(connection_manager=None, parent_dialog=None)


def test_dialog_keeps_options_its_keyword_menu_does_not_list(gui):
    """The old fixed dropdown dropped any unlisted keyword on load, so saving
    the connection deleted that line from the Host block."""
    tab = _tab(gui)
    tab.set_extra_ssh_config(
        "ObscureKeystrokeTiming no\nSomeFutureOption=abc\nCompression yes\n# note\nTCPKeepAlive"
    )
    assert tab.get_config_entries() == [
        ("ObscureKeystrokeTiming", "no"),
        ("SomeFutureOption", "abc"),
        ("Compression", "yes"),
        ("TCPKeepAlive", "yes"),
    ]
    assert tab.get_extra_ssh_config() == (
        "ObscureKeystrokeTiming no\nSomeFutureOption abc\nCompression yes\nTCPKeepAlive yes"
    )
    # Loaded lines are never refused, whatever OpenSSH thinks of them.
    assert tab.has_invalid_options() is False


def test_dialog_does_not_offer_structure_or_fields_it_already_has(gui):
    from sshpilot.connection_dialog import ADVANCED_TAB_SSH_OPTIONS
    from sshpilot.ssh_config_formatter import MANAGED_HOST_OPTIONS

    lowered = {name.lower() for name in ADVANCED_TAB_SSH_OPTIONS}
    assert not lowered & {"host", "match", "include"}
    assert not lowered & MANAGED_HOST_OPTIONS
    assert "ciphers" in lowered


@pytest.mark.skipif(shutil.which("ssh") is None, reason="needs OpenSSH")
def test_dialog_refuses_bad_rows_and_blocks_the_save(gui):
    tab = _tab(gui)
    tab.set_extra_ssh_config("Compression yes")
    options = tab.option_list

    row = options.add_empty_row()
    _finish(options, row, "ProxyJump", "bastion")        # has its own field
    assert "ProxyJump" in row.error
    assert tab.has_invalid_options() is True
    assert tab.get_extra_ssh_config() == "Compression yes"   # not written

    _finish(options, row, "Compression", "sometimes")     # OpenSSH rejects the value
    assert row.error and tab.has_invalid_options() is True

    _finish(options, row, "Match", "all")                 # not an option
    assert "Match" in row.error

    _finish(options, row, "ServerAliveInterval", "30")
    assert row.error is None and tab.has_invalid_options() is False
    assert tab.get_extra_ssh_config() == "Compression yes\nServerAliveInterval 30"


def test_dialog_option_helpers_still_work(gui):
    tab = _tab(gui)
    tab.set_option("SessionType", "none")
    tab.set_option("sessiontype", "default")
    assert tab.get_option("SESSIONTYPE") == "default"
    assert len(tab.get_config_entries()) == 1
    tab.set_option("ExitOnForwardFailure", "yes")
    tab.remove_option("ExitOnForwardFailure", "no")       # value does not match
    assert tab.get_option("ExitOnForwardFailure") == "yes"
    tab.remove_option("ExitOnForwardFailure", "yes")
    assert tab.get_option("ExitOnForwardFailure") is None
    assert tab.generate_ssh_config("web") == "Host web\n    sessiontype default"


# -- keyword suggestions while typing ---------------------------------------


def test_keyword_matches_prefix_first_then_anywhere(gui):
    options = _list(gui, keywords=("Compression", "ConnectTimeout", "IPQoS", "SessionType"))
    assert options.keyword_matches("") == []
    assert options.keyword_matches("co") == ["Compression", "ConnectTimeout"]
    assert options.keyword_matches("TIME") == ["ConnectTimeout"]
    assert options.keyword_matches("s") == ["SessionType", "Compression", "IPQoS"]
    assert options.keyword_matches("zzz") == []
    # Typed in full, with nothing else to offer: no list.
    assert options.keyword_matches("ipqos") == []


def test_typing_a_keyword_suggests_and_arrow_keys_pick(gui):
    from gi.repository import Gdk, Gtk

    options = _list(gui, keywords=("Compression", "ConnectTimeout", "IPQoS"))
    window = Gtk.Window(child=options)
    window.set_default_size(520, 300)
    window.present()
    try:
        gui.pump(300)
        row = options.add_empty_row()
        gui.pump(200)

        row.keyword_entry.set_text("co")          # typed with the field focused
        gui.pump(200)
        assert options.shown_suggestions(row) == ["Compression", "ConnectTimeout"]
        # The list never takes the focus away from the field.
        assert row.keyword_entry.get_delegate().has_focus()

        row.keyword_entry.set_text("con")
        gui.pump(100)
        assert options.shown_suggestions(row) == ["ConnectTimeout"]

        row.keyword_entry.set_text("co")
        gui.pump(100)
        key = lambda value: options._on_keyword_key(None, value, 0, 0, row)
        assert key(Gdk.KEY_Down) is True and key(Gdk.KEY_Down) is True
        assert key(Gdk.KEY_Return) is True
        gui.pump(200)
        assert row.keyword_entry.get_text() == "ConnectTimeout"
        assert options.suggestions_visible(row) is False
        assert row.value_entry.get_delegate().has_focus()

        # Enter with nothing selected keeps what was typed.
        row.keyword_entry.grab_focus()
        row.keyword_entry.set_text("i")
        gui.pump(100)
        assert options.shown_suggestions(row) == ["IPQoS", "Compression", "ConnectTimeout"]
        assert key(Gdk.KEY_Return) is False
        assert row.keyword_entry.get_text() == "i"

        # Escape closes the list; an unknown keyword offers nothing.
        row.keyword_entry.set_text("ip")
        gui.pump(100)
        assert key(Gdk.KEY_Escape) is True
        assert options.suggestions_visible(row) is False
        row.keyword_entry.set_text("zzz")
        gui.pump(100)
        assert options.suggestions_visible(row) is False
    finally:
        window.destroy()


def test_loading_options_never_opens_suggestions(gui):
    from gi.repository import Gtk

    options = _list(gui, keywords=("Compression", "ConnectTimeout"))
    window = Gtk.Window(child=options)
    window.present()
    try:
        gui.pump(200)
        options.set_options([("Co", "x")])
        gui.pump(200)
        assert options.suggestions_visible(options.rows()[0]) is False
    finally:
        window.destroy()


# -- a refused edit must never change what is saved -------------------------


def test_refused_edit_of_a_saved_option_keeps_the_saved_value(gui):
    """Saving on ``changed`` must not drop an option because of a typo in it."""
    options = _list(gui, refused={"ipqos": None})
    options._validate = lambda keyword, value: "bad value" if value == "typo" else None
    saves = []
    options.connect("changed", lambda _o: saves.append(list(options.get_options())))
    options.set_options([("IPQoS", "none"), ("SetEnv", "A=b")])
    row = options.rows()[0]

    _finish(options, row, "IPQoS", "typo")
    assert row.error == "bad value"
    assert options.get_options() == [("IPQoS", "none"), ("SetEnv", "A=b")]
    assert saves == []                               # nothing was persisted

    row.value_entry.set_text("")                     # value cleared: half an option
    options._finish(row)
    assert row.error and saves == []
    assert options.get_options() == [("IPQoS", "none"), ("SetEnv", "A=b")]

    _finish(options, row, "IPQoS", "lowdelay")       # a good edit saves the new value
    assert row.error is None
    assert saves == [[("IPQoS", "lowdelay"), ("SetEnv", "A=b")]]

    _finish(options, row, "IPQoS", "typo")           # ...and is what a later typo keeps
    assert options.get_options()[0] == ("IPQoS", "lowdelay")
    assert len(saves) == 1

    options.remove(row)                              # only removing takes it away
    assert saves[-1] == [("SetEnv", "A=b")]


def test_emptying_both_fields_removes_the_option(gui):
    options = _list(gui)
    saves = []
    options.connect("changed", lambda _o: saves.append(list(options.get_options())))
    options.set_options([("IPQoS", "none")])
    row = options.rows()[0]
    row.keyword_entry.set_text("")
    row.value_entry.set_text("")
    options._finish(row)
    assert row.error is None and saves == [[]]


def test_half_filled_row_blocks_the_dialog_save(gui):
    tab = _tab(gui)
    tab.set_extra_ssh_config("Compression yes")
    assert tab.has_invalid_options() is False

    blank = tab.option_list.add_empty_row()          # an untouched new row is fine
    assert tab.has_invalid_options() is False

    blank.keyword_entry.set_text("TCPKeepAlive")     # keyword but no value
    assert tab.has_invalid_options() is True
    assert blank.error and blank.error_label.get_visible()
    assert tab.get_extra_ssh_config() == "Compression yes"

    blank.keyword_entry.set_text("")
    blank.value_entry.set_text("no")                 # value but no keyword
    assert tab.has_invalid_options() is True

    blank.keyword_entry.set_text("TCPKeepAlive")
    assert tab.has_invalid_options() is False
    assert tab.get_extra_ssh_config() == "Compression yes\nTCPKeepAlive no"


def test_note_remarks_on_an_accepted_row_without_refusing_it(gui):
    note = lambda keyword, _value: "added" if keyword.lower() == "identityfile" else None
    options = _list(gui, note=note, refused={"badone": "nope"})
    options.set_options([("IdentityFile", "~/.ssh/a"), ("IPQoS", "none")])
    loaded, plain = options.rows()
    assert loaded.note_label.get_visible() and loaded.note_label.get_label() == "added"
    assert not plain.note_label.get_visible()

    row = options.add_empty_row()
    _finish(options, row, "IdentityFile", "~/.ssh/b")
    assert row.error is None and row.note_label.get_visible()
    assert ("IdentityFile", "~/.ssh/b") in options.get_options()

    _finish(options, row, "BadOne", "x")        # an error replaces the note
    assert row.error == "nope" and not row.note_label.get_visible()
