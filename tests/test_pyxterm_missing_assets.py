"""PyXterm without its xterm.js assets falls back instead of a blank page (#1382).

The .deb drops the bundled xterm.js in favour of Debian's libjs-xterm. The AUR
sshpilot-bin package unpacks that .deb on Arch, where no libjs-xterm exists, so
a saved "xterm.js" terminal choice used to open every tab as an empty WebView.
"""
import os

from sshpilot import xterm_shell
from sshpilot.terminal_backends import PyXtermBridgeBackend

_BUNDLED = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src", "sshpilot", "vendor", "pyxtermjs", "xterm",
)


def test_assets_available_follows_the_asset_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("PYXTERMJS_ASSETS_DIR", str(tmp_path))
    assert xterm_shell.assets_available() is False

    monkeypatch.setenv("PYXTERMJS_ASSETS_DIR", _BUNDLED)
    assert xterm_shell.assets_available() is True


def test_backend_is_unavailable_without_xterm_js(monkeypatch, tmp_path):
    monkeypatch.setenv("PYXTERMJS_ASSETS_DIR", str(tmp_path))

    def no_webview(self, _webkit):
        raise AssertionError("a WebView was built without xterm.js to load")

    monkeypatch.setattr(PyXtermBridgeBackend, "_make_webview_webkit6", no_webview)

    backend = PyXtermBridgeBackend(object())

    assert backend.available is False
    assert isinstance(backend.import_error, FileNotFoundError)
