"""Headless tests for the embedded-terminal HTML shell builder.

Pure string building (no gi/WebKit), so runs under the normal harness.
"""
import json
import os
import shutil
import subprocess

import pytest

from sshpilot.xterm_shell import (
    SELECTION_MOUSE_REPORT_GUARD_JS,
    asset_dir,
    build_shell_html,
)


def _read_asset(name: str) -> str:
    with open(os.path.join(asset_dir(), name), encoding="utf-8") as handle:
        return handle.read()


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
    assert "term.onBinary" in html
    assert 'encoding: "binary"' in html
    assert "btoa(d)" in html
    assert 'send({ type: "ready"' in html or 'type: "ready"' in html
    # Ready is synchronous after fit; one rAF refines size/focus (not double-rAF).
    assert "requestAnimationFrame" in html
    # Bulk preready flush helper (base64 → term.write once).
    assert "window.termWriteB64" in html
    # Flow control write + ack (xterm.js flowcontrol guide).
    assert "window.termWrite" in html
    assert 'type: "write-ack"' in html
    # SearchAddon helper + result events.
    assert "window.sshpilotSearch" in html
    assert 'type: "search-result"' in html
    assert 'type: "search-results"' in html
    # Decorations need proposed API (SearchAddon match highlights).
    assert '"allowProposedApi": true' in html
    # Sticky scroll: only scrollToBottom when already at bottom.
    assert "_isAtBottom" in html
    assert "scrollToBottom" in html
    # WebLinks must bridge to Python — default window.open() is blocked in WebKitGTK.
    assert 'type: "open-url"' in html or "type: \"open-url\"" in html
    # Hover/leave feed TerminalWidget._hovered_hyperlink_uri for Open/Copy Link.
    assert 'type: "link-hover"' in html
    assert 'type: "link-leave"' in html


def test_clipboard_shortcuts_are_owned_once_and_respect_passthrough():
    html = build_shell_html()
    assert "window.sshpilotShortcutPassthrough" in html
    assert "const isMac = /Mac|iPhone|iPad/" in html
    assert "isMac ? e.metaKey : (e.ctrlKey && e.shiftKey)" in html
    assert 'if (k === "c")' in html
    assert 'k === "c" || k === "x"' not in html
    assert 'type: "paste"' in html
    assert 'type: "copy"' in html
    assert 'type: "selection-changed"' in html
    assert 'type: "clipboard-passthrough"' in html
    assert "hasSelection: term.hasSelection()" in html
    assert "selectionLength: selection.length" in html


def test_link_handler_matches_xterm_docs_pattern():
    """Shared ILinkHandler for WebLinks + OSC 8 (xtermjs.org link-handling guide)."""
    html = build_shell_html()
    assert "const linkHandler" in html
    assert "term.options.linkHandler = linkHandler" in html
    assert "new WebLinksAddon.WebLinksAddon(activateLink, linkHandler)" in html
    assert "allowNonHttpProtocols: false" in html
    # Modifier required to open (Ctrl / Cmd).
    assert "event.metaKey" in html
    assert "event.ctrlKey" in html
    assert "function activateLink" in html


def test_theme_and_font_seeded():
    html = build_shell_html(theme={"background": "#112233"}, font_family="Fira", font_size=15)
    assert "#112233" in html
    assert "Fira" in html
    assert "15" in html


def test_autocomplete_popup_present():
    html = build_shell_html()
    assert 'id="ac"' in html
    assert "window.sshpilotAC" in html
    assert "sshpilotAC.visible()" in html  # key handler consults the popup
    assert "e.preventDefault()" in html
    assert "sshpilotAC.key(e)" in html
    # Suggestion-only: no auto-highlight; Tab passes through; Enter needs selection.
    assert "sel = -1" in html
    assert 'e.key === "Tab" || e.key === "ArrowRight") return true' in html
    assert "hasSelection()" in html
    # Bottom-of-terminal: flip above the real cursor box (helper textarea).
    assert "placeAbove" in html
    assert "xterm-helper-textarea" in html
    assert "cursorTop - gap - h" in html
    # Bold + distinct panel (lifted bg, accent border, solid selection).
    assert "font-weight:700" in html or "font-weight: 700" in html
    assert "--ac-accent" in html
    assert "--ac-sel-fg" in html
    assert "liftHex" in html


def test_asset_dir_exists():
    # Either the system libjs-xterm or the bundled copy must resolve to real files.
    import os
    d = asset_dir()
    assert os.path.isfile(os.path.join(d, "xterm.js"))


# --- Shift+drag selection vs. mouse reporting (xterm.js) ---------------------
#
# xterm.js skips the *press* when SelectionService.shouldForceSelection says
# the user is forcing a local selection, but the mouseup listener it installs
# for the UP protocol never re-checks the modifier. The release therefore
# still reaches the remote app, and because reports go out through
# CoreService.triggerDataEvent -- which SelectionService treats as user input
# -- the selection the user just made is cleared the instant they let go.
# Verified against a live WebKit page before and after the guard.

_GUARD_HARNESS = """
%s

function makeTerm(forceSelection) {
  const listeners = { mousedown: [], mouseup: [] };
  const reported = [];
  const doc = {
    addEventListener: function (name, handler, capture) {
      listeners[name].push({ handler: handler, capture: !!capture });
    }
  };
  const term = { _core: {
    _selectionService: { shouldForceSelection: function (ev) { return !!ev.shift; } },
    coreMouseService: {
      triggerMouseEvent: function (ev) { reported.push(ev); return true; }
    }
  } };
  const installed = installSelectionMouseReportGuard(term, doc);
  return {
    installed: installed,
    reported: reported,
    press: function (shift) {
      listeners.mousedown.forEach(function (l) { l.handler({ shift: shift }); });
    },
    release: function () {
      listeners.mouseup.forEach(function (l) { l.handler({}); });
    },
    report: function (action) {
      return term._core.coreMouseService.triggerMouseEvent({ action: action });
    },
    capturesPress: listeners.mousedown.every(function (l) { return l.capture; }),
    bubblesRelease: listeners.mouseup.every(function (l) { return !l.capture; })
  };
}

const out = {};

// A Shift+drag is local selection only: nothing goes on the wire, so nothing
// counts as user input and the selection survives the release.
const forced = makeTerm();
out.installed = forced.installed;
out.capturesPress = forced.capturesPress;
out.bubblesRelease = forced.bubblesRelease;
forced.press(true);
forced.report("move");
forced.report("up");
out.reportedDuringShiftDrag = forced.reported.length;

// The remote app keeps every ordinary click and drag.
const plain = makeTerm();
plain.press(false);
plain.report("move");
plain.report("up");
out.reportedDuringPlainDrag = plain.reported.length;

// The suppression lasts exactly one gesture.
const after = makeTerm();
after.press(true);
after.report("up");
after.release();
after.press(false);
after.report("down");
after.report("up");
out.reportedAfterShiftDrag = after.reported.length;

console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_selection_guard_drops_only_the_forced_selection_gesture(tmp_path):
    script = tmp_path / "guard.js"
    script.write_text(_GUARD_HARNESS % SELECTION_MOUSE_REPORT_GUARD_JS)
    result = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, check=True
    )
    out = json.loads(result.stdout)

    assert out["installed"] is True
    assert out["capturesPress"] is True, "the press flag must be set before xterm.js reacts"
    assert out["bubblesRelease"] is True, "the reset must outlive xterm.js's own handlers"
    # The whole point: a Shift+drag puts nothing on the wire, so nothing
    # reaches CoreService.triggerDataEvent to clear the selection.
    assert out["reportedDuringShiftDrag"] == 0
    # ...and htop still gets every ordinary click and drag.
    assert out["reportedDuringPlainDrag"] == 2
    assert out["reportedAfterShiftDrag"] == 2


def test_selection_guard_is_installed_in_the_page():
    html = build_shell_html()
    assert "installSelectionMouseReportGuard(term, document);" in html
    assert "shouldForceSelection" in html
    # Installed after term.open(): _core._selectionService does not exist before.
    assert html.index('term.open(document.getElementById("terminal"));') < html.index(
        "installSelectionMouseReportGuard(term, document);"
    )


def test_vendored_xterm_still_exposes_what_the_guard_hooks():
    """The guard reaches into xterm.js internals and fails open if they move.

    Without this, an xterm.js bump that renames one of them would silently
    disable the guard and bring the lost selection back.
    """
    core = _read_asset("xterm.js")
    for symbol in (
        "shouldForceSelection",
        "triggerMouseEvent",
        "_selectionService",
        "coreMouseService",
    ):
        assert symbol in core, f"vendored xterm.js no longer defines {symbol}"
