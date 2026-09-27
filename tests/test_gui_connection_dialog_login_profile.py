"""The Username row's login profile picker, against real GTK widgets."""

import pytest

from sshpilot.api.models.login_profiles import (
    LoginProfileSettings,
    LoginProfileSnapshot,
    LoginProfileSummary,
)

pytestmark = pytest.mark.gui

A = "lp-aaaaaaaaaaaaaaaa"
B = "lp-bbbbbbbbbbbbbbbb"

SNAPSHOT = LoginProfileSnapshot(
    available=True,
    profiles=(
        LoginProfileSummary(id=A, settings=LoginProfileSettings(name="Deploy", username="deploy"), revision=1),
        LoginProfileSummary(id=B, settings=LoginProfileSettings(name="Anon"), revision=1),
    ),
)


@pytest.fixture
def gtk():
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gtk
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"GTK unavailable: {exc}")
    if not isinstance(getattr(Gtk, "MAJOR_VERSION", None), int):
        pytest.skip("gi is stubbed in this environment (no real GTK)")
    if not Gtk.init_check():
        pytest.skip("GTK cannot initialise (no display)")
    Adw.init()
    return Adw, Gtk


def _dialog(gtk):
    Adw, Gtk = gtk
    from sshpilot.connection_dialog_login_profile import ConnectionDialogLoginProfileMixin

    class Dialog(ConnectionDialogLoginProfileMixin):
        is_editing = False

    dialog = Dialog()
    dialog.username_row = Adw.EntryRow(title="Username")
    dialog.username_row.set_text("alice")
    dialog._add_username_profile_button(dialog.username_row)
    dialog._build_login_profile_group()
    dialog.auth_group = Adw.PreferencesGroup()
    dialog._register_login_profile_locked_widgets([dialog.auth_group])
    return dialog


def _pick(dialog, label):
    dialog._fill_username_profile_list()
    row = dialog._username_profile_list.get_first_child()
    while row is not None:
        if row.get_child().get_first_child().get_label() == label:
            dialog._on_username_profile_activated(dialog._username_profile_list, row)
            return
        row = row.get_next_sibling()
    raise AssertionError(f"no picker entry {label!r}")


def test_button_appears_with_profiles(gtk):
    dialog = _dialog(gtk)
    assert not dialog._username_profile_button.get_visible()
    dialog._on_login_profile_snapshot(SNAPSHOT)
    assert dialog._username_profile_button.get_visible()


def _dimmed(row):
    return row.get_delegate().get_parent().has_css_class("dim-label")


def test_picking_a_profile_links_it_and_greys_out_the_username(gtk):
    dialog = _dialog(gtk)
    dialog._on_login_profile_snapshot(SNAPSHOT)

    _pick(dialog, "Deploy")

    assert dialog.login_profile_row.get_selected() == 1
    assert dialog.username_row.get_text() == "deploy"
    assert not dialog.username_row.get_editable()
    assert dialog.username_row.get_title() == "Username (using a login profile)"
    # The text is greyed out; the picker button stays usable.
    assert _dimmed(dialog.username_row)
    assert dialog._username_profile_button.is_sensitive()
    assert not dialog._username_profile_button.has_css_class("dim-label")
    assert not dialog.auth_group.get_sensitive()
    assert dialog.collect_login_profile_change()[1] == A


def test_an_inherited_username_is_not_adopted_or_dimmed_twice(gtk):
    dialog = _dialog(gtk)
    dialog.username_row.add_css_class("dim-label")  # shown as inherited
    adopted = []
    dialog.username_row.connect(
        "changed",
        lambda _r: None if dialog._applying_inherited_value else adopted.append(1),
    )
    dialog._on_login_profile_snapshot(SNAPSHOT)

    _pick(dialog, "Deploy")
    assert not _dimmed(dialog.username_row)  # the row itself is already dim
    _pick(dialog, "Don't use a profile")

    assert dialog.username_row.get_text() == "alice"
    assert adopted == []


def test_switching_profiles_then_back_restores_the_own_username(gtk):
    dialog = _dialog(gtk)
    dialog._on_login_profile_snapshot(SNAPSHOT)

    _pick(dialog, "Deploy")
    _pick(dialog, "Anon")
    assert dialog.username_row.get_text() == ""
    _pick(dialog, "Don't use a profile")

    assert dialog.username_row.get_text() == "alice"
    assert dialog.username_row.get_editable()
    assert dialog.username_row.get_title() == "Username"
    assert not _dimmed(dialog.username_row)
    assert dialog.auth_group.get_sensitive()
    assert dialog.collect_login_profile_change() is None


def test_both_buttons_use_the_bundled_profile_icon(gtk):
    from gi.repository import Gio, Gtk

    dialog = _dialog(gtk)
    images = [
        dialog._username_profile_button.get_child(),
        dialog.login_profile_manage_button.get_child(),
    ]
    for image in images:
        assert isinstance(image, Gtk.Image)
        gicon = image.get_gicon()
        assert isinstance(gicon, Gio.FileIcon)
        assert gicon.get_file().get_uri().endswith("/actions/system-users-symbolic.svg")


def test_manage_opens_the_profiles_window_over_the_dialog_and_reloads_on_close(gtk):
    Adw, Gtk = gtk
    dialog = _dialog(gtk)
    opened = []
    profiles_window = Gtk.Window()
    profiles_window.present()

    class Parent:
        def show_login_profiles_window(self, transient_for=None):
            opened.append(transient_for)
            return profiles_window

    dialog.parent_window = Parent()
    reloads = []
    dialog.start_login_profile_load = lambda: reloads.append(1)
    assert dialog.login_profile_manage_button.get_tooltip_text() == "Manage Profiles…"

    dialog.login_profile_manage_button.emit("clicked")
    dialog.login_profile_manage_button.emit("clicked")  # already open
    assert opened == [dialog, dialog]

    profiles_window.close()
    assert reloads == [1]


def test_status_page_paintable_comes_from_the_bundled_icon(gtk):
    from sshpilot.icon_utils import new_paintable_from_icon_name

    paintable = new_paintable_from_icon_name("system-users-symbolic", 128)
    assert paintable.get_file().get_uri().endswith("/actions/system-users-symbolic.svg")


class _FakeController:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def refresh(self):
        return SNAPSHOT


def _pump_until(predicate, timeout=2.0):
    from gi.repository import GLib

    loop = GLib.MainLoop()

    def _check():
        if predicate():
            loop.quit()
            return False
        return True

    GLib.timeout_add(20, _check)
    GLib.timeout_add(int(timeout * 1000), lambda: (loop.quit(), False)[1])
    loop.run()
    return predicate()


def test_profiles_window_never_opens_on_the_empty_page(gtk):
    Adw, Gtk = gtk
    from sshpilot.login_profile_dialogs import LoginProfilesWindow

    # No cached profiles yet: blank until the refresh lands, then the list.
    window = LoginProfilesWindow(Gtk.Window(), _FakeController(None))
    assert window._stack.get_visible_child_name() == "loading"
    assert _pump_until(lambda: window._stack.get_visible_child_name() == "list")

    # Cached profiles: the list straight away.
    window = LoginProfilesWindow(Gtk.Window(), _FakeController(SNAPSHOT))
    assert window._stack.get_visible_child_name() == "list"
    _pump_until(lambda: False, timeout=0.3)  # let the background refresh land
