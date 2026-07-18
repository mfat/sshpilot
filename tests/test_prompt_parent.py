"""resolve_topmost_prompt_parent: a routed askpass prompt must stack on the
modal secondary window (e.g. the SCP browse dialog), even when GTK reports the
main window as active (Wayland modal-transient quirk)."""
from sshpilot.window_dialogs import resolve_topmost_prompt_parent


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
