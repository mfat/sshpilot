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
