"""resolve_topmost_prompt_parent: a routed askpass prompt must stack on the
modal secondary window (e.g. the SCP browse dialog), even when GTK reports the
main window as active (Wayland modal-transient quirk)."""
from sshpilot.window_dialogs import (
    associate_window_with_parent_application,
    resolve_topmost_prompt_parent,
)


class FakeWin:
    def __init__(self, visible=True, modal=False):
        self._visible = visible
        self._modal = modal

    def get_visible(self):
        return self._visible

    def get_modal(self):
        return self._modal


def test_modal_secondary_wins_over_active_main():
    main = FakeWin()
    browse = FakeWin(modal=True)  # SCP browse Adw.Window
    # GTK reports the MAIN window active (the bug this guards against).
    parent = resolve_topmost_prompt_parent([main, browse], main, main)
    assert parent is browse


def test_active_modal_preferred_among_several():
    main = FakeWin()
    d1 = FakeWin(modal=True)
    d2 = FakeWin(modal=True)
    parent = resolve_topmost_prompt_parent([main, d1, d2], d1, main)
    assert parent is d1


def test_most_recently_focused_modal_used_when_none_is_active():
    """Gtk.Application.get_windows() is ordered most-recently-focused-first,
    so with several modal secondaries and no active-window match, the first
    one in the list — not the last — is the right (most recent) pick."""
    main = FakeWin()
    most_recent = FakeWin(modal=True)
    older = FakeWin(modal=True)
    # `active_window` is main here (the Wayland quirk this helper guards
    # against), so neither modal secondary matches it directly.
    parent = resolve_topmost_prompt_parent([main, most_recent, older], main, main)
    assert parent is most_recent


def test_hidden_modal_ignored():
    main = FakeWin()
    stale = FakeWin(visible=False, modal=True)
    parent = resolve_topmost_prompt_parent([main, stale], main, main)
    assert parent is main


def test_non_modal_active_secondary_used():
    # Non-modal secondary (e.g. the file manager window) that is active.
    main = FakeWin()
    fm = FakeWin(modal=False)
    parent = resolve_topmost_prompt_parent([main, fm], fm, main)
    assert parent is fm


def test_falls_back_to_main_when_nothing_else():
    main = FakeWin()
    parent = resolve_topmost_prompt_parent([main], main, main)
    assert parent is main
    # Empty window list / no active window is also safe.
    assert resolve_topmost_prompt_parent([], None, main) is main


# ---------------------------------------------------------------------------
# associate_window_with_parent_application: a bare Adw.Window/Gtk.Window is
# absent from Gtk.Application.get_windows() unless explicitly registered —
# set_transient_for() alone does not add it. Anything that presents itself as
# a blocking modal secondary (the connection editor, the SCP browse dialog,
# …) must call this so resolve_topmost_prompt_parent can find it.
# ---------------------------------------------------------------------------


class _RegisteringFakeWin(FakeWin):
    def __init__(self, *, application=None, **kwargs):
        super().__init__(**kwargs)
        self._application = None
        self._get_application_result = application

    def get_application(self):
        return self._get_application_result

    def set_application(self, app):
        self._application = app


def test_associate_registers_window_with_parents_application():
    app = object()
    parent = _RegisteringFakeWin(application=app)
    window = _RegisteringFakeWin()

    associate_window_with_parent_application(window, parent)

    assert window._application is app


def test_associate_is_a_noop_when_parent_has_no_application():
    parent = _RegisteringFakeWin(application=None)
    window = _RegisteringFakeWin()

    associate_window_with_parent_application(window, parent)

    assert window._application is None


def test_associate_swallows_missing_get_application():
    """A parent double without get_application() (common in tests) must not
    raise — the same tolerance the old inline try/except had."""
    window = _RegisteringFakeWin()

    associate_window_with_parent_application(window, parent=object())

    assert window._application is None


def test_associate_handles_none_parent():
    window = _RegisteringFakeWin()
    associate_window_with_parent_application(window, parent=None)
    assert window._application is None


# --- install_toast_overlay ---------------------------------------------------
#
# A secondary window that can start a launch needs somewhere to show a message
# the daemon sends while that launch runs. Without one, an alert routed to it
# falls back to the main window -- which is behind it, where nobody looks.


class FakeOverlay:
    def __init__(self):
        self.child = None

    def set_child(self, child):
        self.child = child


class FakeContentWindow:
    def __init__(self, content="content"):
        self._content = content
        self.toast_overlay = None

    def get_content(self):
        return self._content

    def set_content(self, content):
        self._content = content


def test_the_window_gets_an_overlay_wrapping_its_existing_content(monkeypatch):
    from sshpilot import window_dialogs

    overlay = FakeOverlay()
    monkeypatch.setattr(window_dialogs.Adw, "ToastOverlay", lambda: overlay)
    window = FakeContentWindow("original")

    window_dialogs.install_toast_overlay(window)

    assert window.toast_overlay is overlay
    assert overlay.child == "original", "the window's content must be kept"
    assert window._content is overlay


def test_installing_twice_keeps_the_first_overlay(monkeypatch):
    """Re-running must not nest overlays or orphan the window's content."""

    from sshpilot import window_dialogs

    first = FakeOverlay()
    monkeypatch.setattr(window_dialogs.Adw, "ToastOverlay", lambda: first)
    window = FakeContentWindow("original")
    window_dialogs.install_toast_overlay(window)

    second = FakeOverlay()
    monkeypatch.setattr(window_dialogs.Adw, "ToastOverlay", lambda: second)
    window_dialogs.install_toast_overlay(window)

    assert window.toast_overlay is first
    assert second.child is None


def test_a_window_with_no_content_is_left_alone():
    from sshpilot import window_dialogs

    window = FakeContentWindow(None)

    window_dialogs.install_toast_overlay(window)

    assert window.toast_overlay is None


def test_a_failure_leaves_the_window_openable(monkeypatch):
    """A missing toast surface must never stop a window from opening."""

    from sshpilot import window_dialogs

    def _explode():
        raise RuntimeError("no display")

    monkeypatch.setattr(window_dialogs.Adw, "ToastOverlay", _explode)
    window = FakeContentWindow("original")

    window_dialogs.install_toast_overlay(window)

    assert window.toast_overlay is None
    assert window._content == "original"


# --- bind_pre_command_status -------------------------------------------------
#
# Three surfaces claim a launch scope this way -- a terminal tab, the SCP
# transfer dialog and the copy-key window. The binding is best effort by
# design: it drives a progress line only, and the failure alert is raised from
# the application regardless, so nothing here may raise into a surface's
# start-up path.


class FakeApp:
    def __init__(self):
        self.registered = {}
        self.unregistered = []

    def register_pre_command_status(self, scope_id, setter):
        self.registered[scope_id] = setter

    def unregister_pre_command_status(self, scope_id):
        self.unregistered.append(scope_id)


class BareApp:
    """An application without the registry -- a plugin host, or a test."""


def _with_app(monkeypatch, app):
    from sshpilot import window_dialogs

    monkeypatch.setattr(
        window_dialogs.Gtk.Application, "get_default", staticmethod(lambda: app)
    )
    return window_dialogs


def test_binding_registers_the_setter_and_unbinding_releases_it(monkeypatch):
    app = FakeApp()
    window_dialogs = _with_app(monkeypatch, app)
    setter = lambda _text: None

    unbind = window_dialogs.bind_pre_command_status("scope-1", setter)

    assert app.registered == {"scope-1": setter}
    unbind()
    assert app.unregistered == ["scope-1"]


def test_a_scope_id_is_always_bound_as_text(monkeypatch):
    """Scope ids arrive as SessionId/TransferId/OperationId, not str."""

    class _Id(str):
        pass

    app = FakeApp()
    window_dialogs = _with_app(monkeypatch, app)

    window_dialogs.bind_pre_command_status(_Id("scope-2"), lambda _t: None)

    assert list(app.registered) == ["scope-2"]
    assert type(next(iter(app.registered))) is str


def test_an_empty_scope_or_setter_binds_nothing(monkeypatch):
    app = FakeApp()
    window_dialogs = _with_app(monkeypatch, app)

    window_dialogs.bind_pre_command_status("", lambda _t: None)()
    window_dialogs.bind_pre_command_status("scope-3", None)()

    assert app.registered == {}
    assert app.unregistered == []


def test_an_application_that_cannot_register_is_tolerated(monkeypatch):
    """Plugin hosts and tests run without the full application."""

    app = BareApp()
    window_dialogs = _with_app(monkeypatch, app)

    unbind = window_dialogs.bind_pre_command_status("scope-4", lambda _t: None)

    unbind()  # must not raise


def test_unbinding_twice_is_safe(monkeypatch):
    app = FakeApp()
    window_dialogs = _with_app(monkeypatch, app)

    unbind = window_dialogs.bind_pre_command_status("scope-5", lambda _t: None)
    unbind()
    unbind()

    assert app.unregistered == ["scope-5", "scope-5"]
