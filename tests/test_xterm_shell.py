"""Headless tests for the embedded-terminal HTML shell builder.

Pure string building (no gi/WebKit), so runs under the normal harness.
"""
from sshpilot.xterm_shell import build_shell_html, asset_dir


def test_self_contained_no_cdn():
    html = build_shell_html()
    assert "cdn.jsdelivr" not in html and "cdnjs" not in html
    assert "socket.io" not in html


def test_inlines_core_and_addons():
    html = build_shell_html()
    # xterm core global + all three addon globals must be present inline
    assert "Terminal" in html
    assert "FitAddon" in html
    assert "WebLinksAddon" in html
    assert "SearchAddon" in html
    # sizable (core alone is ~280 KB)
    assert len(html) > 200_000


def test_bridge_wiring_present():
    html = build_shell_html()
    assert "window.webkit.messageHandlers.sshpilotPty.postMessage" in html
    assert '"type": "input"' in html or "type: \"input\"" in html or "type:\"input\"" in html
    assert 'send({ type: "ready"' in html or 'type: "ready"' in html


def test_theme_and_font_seeded():
    html = build_shell_html(theme={"background": "#112233"}, font_family="Fira", font_size=15)
    assert "#112233" in html
    assert "Fira" in html
    assert "15" in html


def test_asset_dir_exists():
    # Either the system libjs-xterm or the bundled copy must resolve to real files.
    import os
    d = asset_dir()
    assert os.path.isfile(os.path.join(d, "xterm.js"))
