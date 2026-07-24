from types import SimpleNamespace

import pytest

try:
    import sshpilot.window as window_module
    from sshpilot.window import MainWindow
except Exception:  # pragma: no cover - depends on GTK test stub state
    window_module = None
    MainWindow = None

pytestmark = pytest.mark.skipif(
    MainWindow is None,
    reason="GTK stubs unavailable or polluted by sibling tests",
)


class _Config:
    def __init__(self):
        self.pinned = set()

    def is_pinned(self, nickname):
        return nickname in self.pinned

    def pin_connection(self, nickname):
        self.pinned.add(nickname)

    def unpin_connection(self, nickname):
        self.pinned.discard(nickname)


class _ToastOverlay:
    def __init__(self):
        self.toasts = []

    def add_toast(self, toast):
        self.toasts.append(toast)


class _Toast:
    def __init__(self, title):
        self.title = title
        self.timeout = 0
        self._dismissed = []

    def set_timeout(self, timeout):
        self.timeout = timeout

    def get_timeout(self):
        return self.timeout

    def connect(self, signal, callback):
        assert signal == 'dismissed'
        self._dismissed.append(callback)

    def dismiss(self):
        for callback in list(self._dismissed):
            callback(self)


class _ToastFactory:
    @staticmethod
    def new(title):
        return _Toast(title)


def test_pin_status_toasts_expire_and_replace_previous_toast(monkeypatch):
    monkeypatch.setattr(window_module.Adw, 'Toast', _ToastFactory)
    overlay = _ToastOverlay()
    window = SimpleNamespace(
        config=_Config(),
        toast_overlay=overlay,
        welcome_view=None,
    )
    connection = SimpleNamespace(nickname='demo')

    MainWindow._toggle_pin_connections(window, [connection])
    first = overlay.toasts[-1]
    first_dismissed = []
    first.connect('dismissed', lambda *_args: first_dismissed.append(True))

    assert first.get_timeout() == 3

    MainWindow._toggle_pin_connections(window, [connection])
    second = overlay.toasts[-1]

    assert first_dismissed == [True]
    assert second is not first
    assert second.get_timeout() == 3
    assert window._pin_status_toast is second
