"""PyXterm theme application fills the page background (no bottom strip)."""
from types import SimpleNamespace
from unittest.mock import MagicMock

from sshpilot.terminal_backends import PyXtermTerminalBackend


def test_apply_theme_syncs_page_background():
    b = object.__new__(PyXtermTerminalBackend)
    b.available = True
    b.owner = SimpleNamespace(
        config=SimpleNamespace(
            get_setting=lambda key, default=None: "default",
            get_terminal_profile=lambda _name: {
                "background": "#112233",
                "foreground": "#abcdef",
                "cursor_color": "#ffffff",
                "highlight_background": "#4A90E2",
                "highlight_foreground": "#ffffff",
                "palette": [],
            },
        )
    )
    scripts = []
    b._run_javascript = scripts.append

    b.apply_theme("default")

    assert len(scripts) == 1
    js = scripts[0]
    assert '"#112233"' in js
    assert "document.body.style.background" in js
    assert "document.documentElement.style.background" in js
    assert "terminalEl.style.background" in js


def test_shell_avoids_100vh_gap():
    from sshpilot.xterm_shell import _build_default_shell_html

    _build_default_shell_html.cache_clear()
    from sshpilot.xterm_shell import build_shell_html

    html = build_shell_html()
    assert "height:100vh" not in html
    assert "background:inherit" in html
