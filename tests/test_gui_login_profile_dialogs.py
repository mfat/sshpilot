"""The login profile dialogs, in real windows over a recording controller.

What a profile does to connections is decided in the daemon and tested
there. These are the questions the dialogs ask on the way: which profile,
what it will replace (one summary, before anything is written), who inherits
a group's profile, what the users of a deleted profile get instead -- and
that Cancel writes nothing.
"""
from __future__ import annotations

import time
import types

import pytest

from sshpilot.api.models.login_profiles import (
    ConnectionProfileLink,
    DeleteLoginProfileResult,
    GroupProfileLink,
    LoginProfileAssignmentPreview,
    LoginProfileField,
    LoginProfileFieldChange,
    LoginProfileLinkMode,
    LoginProfileSettings,
    LoginProfileSnapshot,
    LoginProfileSummary,
    LoginProfileUpdatePreview,
)
from tests.test_gui_auth_section_widgets import gtk_fixture  # noqa: F401 - fixture

pytestmark = pytest.mark.gui

DEPLOY = "lp-aaaaaaaaaaaaaaaa"
OPS = "lp-bbbbbbbbbbbbbbbb"

SNAPSHOT = LoginProfileSnapshot(
    available=True,
    profiles=(
        LoginProfileSummary(id=DEPLOY, revision=3, settings=LoginProfileSettings(
            name="Deploy", username="deploy",
        )),
        LoginProfileSummary(id=OPS, revision=1, settings=LoginProfileSettings(
            name="Ops", username="ops",
        )),
    ),
    group_links=(GroupProfileLink(group_id="g-prod", profile_id=DEPLOY),),
    connection_links=(
        ConnectionProfileLink(
            connection_id="web1", mode=LoginProfileLinkMode.EXPLICIT,
            profile_id=DEPLOY,
        ),
        ConnectionProfileLink(
            connection_id="db1", mode=LoginProfileLinkMode.EXPLICIT,
            profile_id=OPS,
        ),
    ),
)


def _change(connection, before, after, field=LoginProfileField.USERNAME):
    return LoginProfileAssignmentPreview(
        connection_id=connection, profile_name="Deploy",
        changes=(LoginProfileFieldChange(field=field, before=before, after=after),),
    )


class Controller:
    """Records what the dialogs ask the daemon for; answers from a script."""

    def __init__(self, *, previews=(), update_preview=None, fail=None):
        self.snapshot = SNAPSHOT
        self.calls = []
        self.previews = tuple(previews)
        self.update_preview = update_preview or LoginProfileUpdatePreview()
        self.fail = fail          # name of the call that raises

    def _call(self, name, *args):
        self.calls.append((name,) + args)
        if self.fail == name:
            raise RuntimeError(f"{name} failed")

    def named(self, name):
        return [call for call in self.calls if call[0] == name]

    def refresh(self):
        self._call("refresh")
        return self.snapshot

    def preview(self, connection_ids, mode, profile_id):
        self._call("preview", tuple(connection_ids), mode, profile_id)
        return self.previews

    def assign(self, connection_ids, mode, profile_id):
        self._call("assign", tuple(connection_ids), mode, profile_id)

    def set_group_profile(self, group_id, profile_id, link_members):
        self._call("set_group_profile", group_id, profile_id, tuple(link_members))

    def delete(self, profile_id, targets):
        self._call("delete", profile_id, dict(targets))
        return DeleteLoginProfileResult()

    def preview_update(self, profile_id, settings):
        self._call("preview_update", profile_id, settings)
        return self.update_preview

    def update(self, profile_id, settings, *, expected_revision=None,
               password=None, sudo_password=None):
        self._call("update", profile_id, settings, expected_revision)
        return LoginProfileSummary(id=profile_id, revision=4, settings=settings)

    def create(self, settings, *, password=None, sudo_password=None):
        self._call("create", settings)
        return LoginProfileSummary(id="lp-cccccccccccccccc", revision=1, settings=settings)


def _pump(until=lambda: False, seconds=3.0):
    from gi.repository import GLib

    context = GLib.MainContext.default()
    deadline = time.time() + seconds
    while time.time() < deadline:
        while context.pending():
            context.iteration(False)
        if until():
            return True
        time.sleep(0.01)
    return bool(until())


@pytest.fixture
def stage(gtk, monkeypatch):
    """A parent window, and every dialog and alert the module opens."""
    from gi.repository import Adw

    import sshpilot.login_profile_dialogs as dialogs

    shown = types.SimpleNamespace(frames=[], alerts=[], dialogs=dialogs)

    make_frame = dialogs._dialog_frame

    def _frame(title, confirm_label):
        frame = make_frame(title, confirm_label)
        shown.frames.append(types.SimpleNamespace(
            dialog=frame[0], page=frame[1], confirm=frame[2], banner=frame[3],
        ))
        return frame

    class _Adw:
        """libadwaita, with every alert the module builds noted down."""

        def __getattr__(self, name):
            return getattr(Adw, name)

        @staticmethod
        def AlertDialog(**kwargs):
            alert = Adw.AlertDialog(**kwargs)
            shown.alerts.append(alert)
            return alert

    monkeypatch.setattr(dialogs, "_dialog_frame", _frame)
    monkeypatch.setattr(dialogs, "Adw", _Adw())
    shown.parent = gtk.Window(default_width=700, default_height=700)
    shown.parent.present()
    _pump(seconds=0.1)
    yield shown
    for dialog in [frame.dialog for frame in shown.frames] + shown.alerts:
        _close(dialog)
    _pump(seconds=0.1)
    shown.parent.destroy()


def _close(dialog):
    """Close a dialog that is still up. One that already closed (or never
    opened) has no window any more, and libadwaita crashes closing it."""
    if dialog.get_mapped():
        dialog.force_close()


def _rows(widget, kind):
    """Every *kind* under *widget*, in order."""
    found = []
    child = widget.get_first_child()
    while child is not None:
        if isinstance(child, kind):
            found.append(child)
        found.extend(_rows(child, kind))
        child = child.get_next_sibling()
    return found


def _labels(combo):
    model = combo.get_model()
    return [model.get_string(i) for i in range(model.get_n_items())]


def _answer(alert, response):
    """Answer an alert once it is up (see the contract suite's helper)."""
    _pump(seconds=0.25)
    alert.emit("response", response)
    _close(alert)
    _pump(seconds=0.1)


# -- assigning a profile to connections ---------------------------------------------

def test_assigning_asks_once_with_what_it_replaces_and_cancel_writes_nothing(stage):
    from gi.repository import Adw

    controller = Controller(previews=(_change("db1", "alice", "deploy"),))
    done = []
    stage.dialogs.show_assign_dialog(
        stage.parent, controller, ["db1"], on_done=lambda: done.append(1)
    )
    frame = stage.frames[0]
    combo = _rows(frame.page, Adw.ComboRow)[0]
    assert _labels(combo) == ["Don't use a profile", "Inherit from group", "Deploy", "Ops"]
    assert combo.get_selected() == 3                  # db1 uses Ops now

    combo.set_selected(2)
    frame.confirm.emit("clicked")
    assert _pump(lambda: bool(stage.alerts))
    assert controller.named("preview") == [
        ("preview", ("db1",), LoginProfileLinkMode.EXPLICIT, DEPLOY)
    ]
    alert = stage.alerts[0]
    assert alert.get_heading() == "Replace connection settings?"
    assert alert.get_body() == (
        "The login profile will replace these settings:\n\n"
        "db1:\n  Username: alice → deploy"
    )
    assert alert.get_close_response() == "cancel"
    _answer(alert, "cancel")
    assert controller.named("assign") == [] and done == []
    assert frame.confirm.get_sensitive()             # the choice can be changed

    # Asked again, and confirmed this time: one write.
    frame.confirm.emit("clicked")
    assert _pump(lambda: len(stage.alerts) == 2)
    _answer(stage.alerts[1], "apply")
    assert _pump(lambda: bool(done))
    assert controller.named("assign") == [
        ("assign", ("db1",), LoginProfileLinkMode.EXPLICIT, DEPLOY)
    ]


def test_an_assignment_that_replaces_nothing_does_not_ask(stage):
    from gi.repository import Adw

    controller = Controller(previews=(
        LoginProfileAssignmentPreview(connection_id="db1", profile_name="Deploy"),
    ))
    done = []
    stage.dialogs.show_assign_dialog(
        stage.parent, controller, ["db1"], on_done=lambda: done.append(1)
    )
    frame = stage.frames[0]
    _rows(frame.page, Adw.ComboRow)[0].set_selected(2)
    frame.confirm.emit("clicked")
    assert _pump(lambda: bool(done))
    assert stage.alerts == []
    assert len(controller.named("assign")) == 1


def test_dont_use_a_profile_detaches_without_a_preview(stage):
    from gi.repository import Adw

    controller = Controller()
    done = []
    stage.dialogs.show_assign_dialog(
        stage.parent, controller, ["web1"], on_done=lambda: done.append(1)
    )
    frame = stage.frames[0]
    combo = _rows(frame.page, Adw.ComboRow)[0]
    assert combo.get_selected() == 2                  # web1 uses Deploy now
    combo.set_selected(0)
    frame.confirm.emit("clicked")
    assert _pump(lambda: bool(done))
    assert controller.named("preview") == [] and stage.alerts == []
    assert controller.named("assign") == [("assign", ("web1",), None, None)]


def test_inherit_from_group_is_offered_and_sent_as_such(stage):
    from gi.repository import Adw

    controller = Controller()
    done = []
    stage.dialogs.show_assign_dialog(
        stage.parent, controller, ["web1", "db1", "cache1"],
        on_done=lambda: done.append(1),
    )
    frame = stage.frames[0]
    # Several connections: no single current choice, and the count is said.
    group = _rows(frame.page, Adw.PreferencesGroup)[0]
    assert group.get_description() == "Applies to 3 connections."
    combo = _rows(frame.page, Adw.ComboRow)[0]
    assert combo.get_selected() == 0
    combo.set_selected(1)
    frame.confirm.emit("clicked")
    assert _pump(lambda: bool(done))
    assert controller.named("assign") == [
        ("assign", ("web1", "db1", "cache1"), LoginProfileLinkMode.INHERIT, None)
    ]


def test_a_failed_assignment_says_so_and_can_be_retried(stage):
    from gi.repository import Adw

    controller = Controller(fail="assign")
    done = []
    stage.dialogs.show_assign_dialog(
        stage.parent, controller, ["db1"], on_done=lambda: done.append(1)
    )
    frame = stage.frames[0]
    _rows(frame.page, Adw.ComboRow)[0].set_selected(0)
    frame.confirm.emit("clicked")
    assert _pump(lambda: frame.banner.get_revealed())
    assert frame.banner.get_title()
    assert frame.confirm.get_sensitive() and done == []


def test_the_profiles_are_fetched_first_when_none_are_loaded(stage):
    controller = Controller()
    controller.snapshot = None

    def refresh():
        controller.calls.append(("refresh",))
        controller.snapshot = SNAPSHOT
        return SNAPSHOT

    controller.refresh = refresh
    stage.dialogs.show_assign_dialog(stage.parent, controller, ["db1"])
    assert stage.frames == []
    assert _pump(lambda: bool(stage.frames))
    assert controller.named("refresh") == [("refresh",)]


# -- a group's profile ----------------------------------------------------------------

def test_the_group_dialog_preselects_who_inherits_and_sends_the_checked_ones(stage):
    from gi.repository import Adw, Gtk

    controller = Controller(previews=(
        _change("cache1", "", "ops"), _change("web1", "deploy", "ops"),
    ))
    done = []
    stage.dialogs.show_group_profile_dialog(
        stage.parent, controller, "g-prod", "Production",
        ["web1", "cache1"], on_done=lambda: done.append(1),
    )
    frame = stage.frames[0]
    combo = _rows(frame.page, Adw.ComboRow)[0]
    assert _labels(combo) == ["None", "Deploy", "Ops"]
    assert combo.get_selected() == 1                  # the group uses Deploy

    members = {
        row.get_title(): row for row in _rows(frame.page, Adw.ActionRow)
        if row.get_title() in ("web1", "cache1")
    }
    checks = {name: _rows(row, Gtk.CheckButton)[0] for name, row in members.items()}
    # web1 has a profile of its own: left alone unless the user says so.
    assert not checks["web1"].get_active()
    assert members["web1"].get_subtitle() == "Uses profile “Deploy”"
    assert checks["cache1"].get_active()

    # Another profile: each member says what it would change -- except the
    # one that keeps its own profile.
    combo.set_selected(2)
    assert _pump(lambda: bool(members["cache1"].get_subtitle()))
    assert members["cache1"].get_subtitle() == "Username: — → ops"
    assert members["web1"].get_subtitle() == "Uses profile “Deploy”"

    frame.confirm.emit("clicked")
    assert _pump(lambda: bool(done))
    assert controller.named("set_group_profile") == [
        ("set_group_profile", "g-prod", OPS, ("cache1",))
    ]


def test_clearing_a_groups_profile_links_no_member(stage):
    from gi.repository import Adw

    controller = Controller()
    done = []
    stage.dialogs.show_group_profile_dialog(
        stage.parent, controller, "g-prod", "Production",
        ["web1", "cache1"], on_done=lambda: done.append(1),
    )
    frame = stage.frames[0]
    _rows(frame.page, Adw.ComboRow)[0].set_selected(0)
    frame.confirm.emit("clicked")
    assert _pump(lambda: bool(done))
    assert controller.named("set_group_profile") == [
        ("set_group_profile", "g-prod", None, ())
    ]


def test_a_failed_group_change_says_so(stage):
    controller = Controller(fail="set_group_profile")
    stage.dialogs.show_group_profile_dialog(
        stage.parent, controller, "g-prod", "Production", ["cache1"],
    )
    frame = stage.frames[0]
    frame.confirm.emit("clicked")
    assert _pump(lambda: frame.banner.get_revealed())
    assert frame.confirm.get_sensitive()


# -- deleting a profile ---------------------------------------------------------------

def test_deleting_an_unused_profile_is_one_question(stage):
    controller = Controller()
    unused = LoginProfileSummary(
        id="lp-dddddddddddddddd", revision=1,
        settings=LoginProfileSettings(name="Old"),
    )
    done = []
    stage.dialogs.show_delete_dialog(
        stage.parent, controller, unused, {}, on_done=done.append
    )
    assert stage.frames == []
    alert = stage.alerts[0]
    assert alert.get_heading() == "Delete “Old”?"
    assert "No connection or group uses this profile" in alert.get_body()
    _answer(alert, "cancel")
    assert controller.named("delete") == []

    stage.dialogs.show_delete_dialog(
        stage.parent, controller, unused, {}, on_done=done.append
    )
    _answer(stage.alerts[1], "delete")
    assert _pump(lambda: bool(done))
    assert controller.named("delete") == [("delete", unused.id, {})]


def test_deleting_a_used_profile_asks_what_each_user_gets_instead(stage):
    from gi.repository import Adw

    controller = Controller()
    done = []
    stage.dialogs.show_delete_dialog(
        stage.parent, controller, SNAPSHOT.profile(DEPLOY),
        {"g-prod": "Production"}, on_done=done.append,
    )
    assert stage.alerts == []
    frame = stage.frames[0]
    combos = {row.get_title(): row for row in _rows(frame.page, Adw.ComboRow)}
    assert set(combos) == {"Apply to all", "Production", "web1"}
    assert combos["Production"].get_subtitle() == "Group"
    assert combos["web1"].get_subtitle() == "Connection"
    # The profile being deleted is not offered as its own replacement.
    targets = ["Detach (keep current values)", "Ops"]
    for combo in combos.values():
        assert _labels(combo) == targets
        assert combo.get_selected() == 0

    # "Apply to all" sets every row; one row can then differ.
    combos["Apply to all"].set_selected(1)
    assert combos["Production"].get_selected() == 1
    assert combos["web1"].get_selected() == 1
    combos["web1"].set_selected(0)

    frame.confirm.emit("clicked")
    assert _pump(lambda: bool(done))
    assert controller.named("delete") == [
        ("delete", DEPLOY, {"group:g-prod": OPS, "web1": None})
    ]


def test_a_failed_delete_keeps_the_dialog_and_says_so(stage):
    controller = Controller(fail="delete")
    done = []
    stage.dialogs.show_delete_dialog(
        stage.parent, controller, SNAPSHOT.profile(DEPLOY), {}, on_done=done.append,
    )
    frame = stage.frames[0]
    frame.confirm.emit("clicked")
    assert _pump(lambda: frame.banner.get_revealed())
    assert frame.confirm.get_sensitive() and done == []


# -- editing a profile that connections use -------------------------------------------

def _edit(stage, controller, saved):
    editor = stage.dialogs.open_profile_editor(
        stage.parent, controller, profile=SNAPSHOT.profile(DEPLOY),
        on_saved=saved.append, parent_window=stage.parent,
    )
    _pump(seconds=0.2)
    return editor


def test_an_edit_that_replaces_users_values_asks_once_in_one_summary(stage):
    from gi.repository import Gtk

    controller = Controller(update_preview=LoginProfileUpdatePreview(
        linked_count=3,
        replaced=(
            _change("web1", "jump.example.com", "bastion",
                    LoginProfileField.PROXY_COMMAND),
            _change("db1", "alice", "deploy"),
        ),
    ))
    saved = []
    editor = _edit(stage, controller, saved)
    try:
        editor.username_row.set_text("release")
        editor._save.emit("clicked")
        assert _pump(lambda: bool(stage.alerts))
        assert controller.named("update") == []       # nothing written yet
        asked = controller.named("preview_update")[0]
        assert asked[1] == DEPLOY and asked[2].username == "release"

        alert = stage.alerts[0]
        assert alert.get_heading() == "Replace user-set values?"
        body = alert.get_body()
        assert "2 of 3 linked connections" in body
        assert "Username" in body
        # Which connections, and what they lose, is one click away.
        expander = alert.get_extra_child()
        assert isinstance(expander, Gtk.Expander)
        details = expander.get_child().get_label()
        assert "web1:" in details and "db1:" in details
        assert "alice → deploy" in details
        assert alert.get_default_response() == "cancel"

        _answer(alert, "cancel")
        assert controller.named("update") == [] and saved == []
        assert editor._save.get_sensitive()           # still open, can be changed

        editor._save.emit("clicked")
        assert _pump(lambda: len(stage.alerts) == 2)
        _answer(stage.alerts[1], "apply")
        assert _pump(lambda: bool(saved))
        update = controller.named("update")[0]
        assert update[1] == DEPLOY and update[2].username == "release"
        assert update[3] == 3                          # the revision it was opened at
    finally:
        editor.dialog.destroy()


def test_an_edit_that_replaces_nothing_saves_without_asking(stage):
    controller = Controller(update_preview=LoginProfileUpdatePreview(linked_count=2))
    saved = []
    editor = _edit(stage, controller, saved)
    try:
        editor.name_row.set_text("Deploy (new)")
        editor._save.emit("clicked")
        assert _pump(lambda: bool(saved))
        assert stage.alerts == []
        assert len(controller.named("preview_update")) == 1
        assert controller.named("update")[0][2].name == "Deploy (new)"
    finally:
        editor.dialog.destroy()


def test_a_new_profile_is_created_without_a_preview(stage):
    controller = Controller()
    saved = []
    editor = stage.dialogs.open_profile_editor(
        stage.parent, controller, on_saved=saved.append, parent_window=stage.parent,
    )
    _pump(seconds=0.2)
    try:
        editor.name_row.set_text("Fresh")
        editor._save.emit("clicked")
        assert _pump(lambda: bool(saved))
        assert controller.named("preview_update") == [] and stage.alerts == []
        assert controller.named("create")[0][1].name == "Fresh"
    finally:
        editor.dialog.destroy()


def test_a_profile_needs_a_name(stage):
    controller = Controller()
    editor = _edit(stage, controller, [])
    try:
        editor.name_row.set_text("   ")
        editor._save.emit("clicked")
        _pump(seconds=0.2)
        assert editor._banner.get_revealed()
        assert editor._banner.get_title() == "Enter a name for the login profile."
        assert controller.calls == []
    finally:
        editor.dialog.destroy()


def test_a_failed_save_stays_open_and_says_so(stage):
    controller = Controller(fail="update")
    saved = []
    editor = _edit(stage, controller, saved)
    try:
        editor._save.emit("clicked")
        assert _pump(lambda: editor._banner.get_revealed())
        assert editor._save.get_sensitive() and saved == []
    finally:
        editor.dialog.destroy()


# -- key passphrases saved from the profile editor ------------------------------------

class _KeyManager:
    def __init__(self, ok=True):
        self.ok, self.stored = ok, []

    def store_key_passphrase(self, path, secret):
        self.stored.append((path, bytes(secret).decode()))
        return self.ok


def _window(key_manager=None, deleted=None, delete_ok=True):
    def delete_key_passphrase(request):
        deleted.append(request.key_path)
        return delete_ok

    return types.SimpleNamespace(
        key_manager=key_manager,
        client=(
            types.SimpleNamespace(delete_key_passphrase=delete_key_passphrase)
            if deleted is not None else None
        ),
    )


def test_passphrases_are_stored_and_removed_by_key_path(gtk):
    from sshpilot.login_profile_dialogs import persist_key_passphrases

    keys, deleted = _KeyManager(), []
    persist_key_passphrases(_window(keys, deleted), [
        ("store", "/home/me/.ssh/id_a", "s3cret"),
        ("delete", "/home/me/.ssh/id_b", ""),
    ])
    assert keys.stored == [("/home/me/.ssh/id_a", "s3cret")]
    assert deleted == ["/home/me/.ssh/id_b"]
    # Nothing to do needs no storage at all.
    persist_key_passphrases(_window(), [])


@pytest.mark.parametrize("window, operation", [
    (_window(None, []), ("store", "/k", "x")),                  # no key storage
    (_window(_KeyManager(), None), ("delete", "/k", "")),       # no daemon
    (_window(_KeyManager(ok=False), []), ("store", "/k", "x")),  # refused
    (_window(_KeyManager(), [], delete_ok=False), ("delete", "/k", "")),
])
def test_a_passphrase_that_cannot_be_saved_is_an_error_not_a_silence(
    gtk, window, operation
):
    from sshpilot.gtk.login_profile_controller import KeyPassphraseStorageError
    from sshpilot.login_profile_dialogs import persist_key_passphrases

    with pytest.raises(KeyPassphraseStorageError):
        persist_key_passphrases(window, [operation])


def test_a_passphrase_failure_after_the_save_reports_the_saved_profile(stage):
    controller = Controller()
    saved = []
    editor = _edit(stage, controller, saved)
    try:
        editor.key_editor.pending_passphrase_operations = lambda: [
            ("store", "/home/me/.ssh/id_a", "s3cret")
        ]
        stage.parent.key_manager = _KeyManager(ok=False)
        editor._save.emit("clicked")
        assert _pump(lambda: editor._banner.get_revealed())
        # The profile itself was saved: the list is refreshed, the form
        # stays open with the reason, ready to try the passphrase again.
        assert len(controller.named("update")) == 1
        assert [summary.id for summary in saved] == [DEPLOY]
        assert editor._save.get_sensitive()
    finally:
        editor.dialog.destroy()
