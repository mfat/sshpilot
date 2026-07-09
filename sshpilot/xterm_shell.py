"""Builds the self-contained HTML shell for the embedded (Cursor-model) terminal.

The embedded PyXterm backend loads this once via ``WebView.load_html`` — xterm.js +
addons inlined (no CDN, no ``resource://`` which WebKit can't resolve, no server).
The page talks to Python over a WebKit ``UserContentManager`` script-message handler
(``window.webkit.messageHandlers.sshpilotPty``); Python writes output back via
``evaluate_javascript("window.term.write(…)")``.

Kept WebKit-free so it is unit-testable headlessly (``tests/test_xterm_shell.py``).
"""
from __future__ import annotations

import json
import os
from typing import Optional

# xterm.js asset layout mirrors Debian's libjs-xterm (see debian/rules + app.py).
_CORE = "xterm.js"
_CSS = "xterm.css"
_ADDONS = (
    os.path.join("addons", "fit", "xterm-addon-fit.js"),
    os.path.join("addons", "webLinks", "xterm-addon-web-links.js"),
    os.path.join("addons", "search", "xterm-addon-search.js"),
)


def asset_dir() -> str:
    """Resolve the xterm.js asset dir: env override → system libjs-xterm → bundled."""
    env = os.environ.get("PYXTERMJS_ASSETS_DIR")
    if env and os.path.isdir(env):
        return env
    system = "/usr/share/javascript/xterm"
    if os.path.isdir(system):
        return system
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "vendor", "pyxtermjs", "xterm")


def _read(rel: str) -> str:
    with open(os.path.join(asset_dir(), rel), "r", encoding="utf-8") as f:
        return f.read()


def build_shell_html(
    theme: Optional[dict] = None,
    font_family: Optional[str] = None,
    font_size: Optional[float] = None,
    background: str = "#000000",
) -> str:
    """Return one self-contained HTML document for the embedded terminal.

    ``theme`` is an xterm.js theme dict (or None). ``font_family``/``font_size``
    seed the initial Terminal options; runtime changes still go through the
    backend's ``apply_theme``/``set_font`` JS injection.
    """
    core = _read(_CORE)
    css = _read(_CSS)
    addons = "\n".join(f"<script>{_read(a)}</script>" for a in _ADDONS)

    opts = {"cursorBlink": True, "scrollback": 1000, "macOptionIsMeta": True}
    if theme:
        opts["theme"] = theme
    if font_family:
        opts["fontFamily"] = font_family
    if font_size:
        opts["fontSize"] = font_size
    opts_json = json.dumps(opts)

    # NOTE: braces in the JS are literal; this is a plain f-string only for the
    # few interpolated values, all JSON-encoded, so no injection surface.
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"/><title>sshPilot Terminal</title>
<style>{css}
  html, body {{ margin:0; padding:0; height:100%; background:{json.dumps(background)[1:-1]}; }}
  #terminal {{ width:100%; height:100vh; }}
  .xterm-viewport, .xterm-screen {{ height:100% !important; }}
</style>
<script>{core}</script>
{addons}
</head><body>
<div id="terminal"></div>
<script>
  const term = new Terminal({opts_json});
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.loadAddon(new WebLinksAddon.WebLinksAddon());
  const searchAddon = new SearchAddon.SearchAddon();
  term.loadAddon(searchAddon);
  term.searchAddon = searchAddon;
  window.term = term; window.fit = fit;

  function send(o) {{
    try {{ window.webkit.messageHandlers.sshpilotPty.postMessage(JSON.stringify(o)); }}
    catch (e) {{ /* handler not registered yet */ }}
  }}
  // Programmatic input path (feed_child/broadcast can also go straight to the PTY).
  window.ptySend = function (o) {{ send(o); return true; }};

  term.open(document.getElementById("terminal"));
  term.onData(d => send({{ type: "input", data: d }}));

  function fitToScreen() {{
    fit.fit();
    send({{ type: "resize", rows: term.rows, cols: term.cols }});
  }}
  function debounce(fn, ms) {{ let t; return function () {{ clearTimeout(t); t = setTimeout(fn, ms); }}; }}
  window.onresize = debounce(fitToScreen, 50);

  requestAnimationFrame(() => {{
    fitToScreen();
    setTimeout(() => term.focus(), 50);
    send({{ type: "ready", rows: term.rows, cols: term.cols }});
  }});

  // Copy/paste keyboard shortcuts (parity with the old shell).
  term.attachCustomKeyEventHandler(function (e) {{
    if (e.type !== "keydown" || !(e.ctrlKey && e.shiftKey)) return true;
    const k = e.key.toLowerCase();
    if (k === "v") {{ navigator.clipboard.readText().then(t => term.paste(t)); return false; }}
    if (k === "c" || k === "x") {{ navigator.clipboard.writeText(term.getSelection()); term.focus(); return false; }}
    return true;
  }});
</script></body></html>"""
