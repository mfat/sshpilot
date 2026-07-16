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
from functools import lru_cache
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
    with open(os.path.join(asset_dir(), rel), encoding="utf-8") as f:
        return f.read()


def build_shell_html(
    theme: Optional[dict] = None,
    font_family: Optional[str] = None,
    font_size: Optional[float] = None,
    background: str = "#000000",
) -> str:
    """Return one self-contained HTML document for the embedded terminal.

    ``theme`` is an xterm.js theme dict (or None). ``font_family``/``font_size``
    seed the initial Terminal options; ``font_size`` is CSS pixels (xterm.js
    units). Runtime changes still go through the backend's ``apply_theme``/
    ``set_font`` JS injection, which converts Pango points → CSS pixels.
    """
    if theme is None and font_family is None and font_size is None and background == "#000000":
        return _build_default_shell_html()
    return _build_shell_html_impl(theme, font_family, font_size, background)


@lru_cache(maxsize=1)
def _build_default_shell_html() -> str:
    return _build_shell_html_impl(None, None, None, "#000000")


def _build_shell_html_impl(
    theme: Optional[dict],
    font_family: Optional[str],
    font_size: Optional[float],
    background: str,
) -> str:
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
  /* Autocomplete popup (window.sshpilotAC); colors come from term.options.theme at show time. */
  #ac {{ position:absolute; display:none; z-index:10; max-height:16em; overflow:hidden;
        border:1px solid rgba(127,127,127,.4); border-radius:6px;
        box-shadow:0 2px 8px rgba(0,0,0,.4); white-space:pre; }}
  .ac-row {{ padding:1px 8px; cursor:pointer; overflow:hidden; text-overflow:ellipsis; max-width:60ch; }}
  .ac-sel {{ background:rgba(127,127,127,.35); }}
</style>
<script>{core}</script>
{addons}
</head><body>
<div id="terminal"></div>
<div id="ac"></div>
<script>
  const term = new Terminal({opts_json});
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
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

  // Default WebLinksAddon uses window.open(), which WebKitGTK blocks without a
  // create-web-view handler. Bridge to Python and open in the system browser.
  term.loadAddon(new WebLinksAddon.WebLinksAddon((event, uri) => {{
    send({{ type: "open-url", url: uri }});
  }}));

  term.open(document.getElementById("terminal"));
  term.onData(d => send({{ type: "input", data: d }}));
  // OSC 0/2 title changes (remote shell prompt) — used as connect evidence.
  term.onTitleChange(t => send({{ type: "title", title: t }}));

  function fitToScreen() {{
    fit.fit();
    send({{ type: "resize", rows: term.rows, cols: term.cols }});
  }}
  function debounce(fn, ms) {{ let t; return function () {{ clearTimeout(t); t = setTimeout(fn, ms); }}; }}
  window.onresize = debounce(fitToScreen, 50);

  // Size and signal readiness synchronously so Python can flush buffered PTY
  // output immediately. Defer focus/extra layout to the next frame.
  fit.fit();
  send({{ type: "ready", rows: term.rows, cols: term.cols }});
  requestAnimationFrame(() => {{
    fitToScreen();
    term.focus();
  }});

  // Autocomplete popup. Python drives it via sshpilotAC.update(payload) —
  // empty items hides it and clears Esc suppression (line reset).
  window.sshpilotAC = (function () {{
    const el = document.getElementById("ac");
    let items = [], sel = 0, suppressed = false;

    function hide() {{ el.style.display = "none"; items = []; }}
    function visible() {{ return el.style.display === "block"; }}

    function accept(i, run) {{
      const it = items[i];
      if (it) send({{ type: "input", data: it.suffix + (run ? "\\r" : "") }});
      hide();
      term.focus();
    }}

    function render() {{
      el.innerHTML = "";
      items.forEach(function (it, i) {{
        const row = document.createElement("div");
        row.className = "ac-row" + (i === sel ? " ac-sel" : "");
        row.textContent = it.text;
        row.addEventListener("mousedown", function (ev) {{ ev.preventDefault(); accept(i, false); }});
        el.appendChild(row);
      }});
    }}

    function position() {{
      const screen = document.querySelector(".xterm-screen");
      if (!screen) return;
      const r = screen.getBoundingClientRect();
      const cw = r.width / term.cols, ch = r.height / term.rows;
      const buf = term.buffer.active;
      el.style.display = "block";
      let x = r.left + buf.cursorX * cw, y = r.top + (buf.cursorY + 1) * ch;
      if (y + el.offsetHeight > window.innerHeight) y = r.top + buf.cursorY * ch - el.offsetHeight;
      x = Math.max(0, Math.min(x, window.innerWidth - el.offsetWidth));
      el.style.left = x + "px";
      el.style.top = Math.max(0, y) + "px";
    }}

    function update(p) {{
      if (!p || !p.items || !p.items.length) {{ hide(); suppressed = false; return; }}
      if (suppressed || term.buffer.active.type === "alternate") return;
      const t = term.options.theme || {{}};
      el.style.background = t.background || "#1e1e1e";
      el.style.color = t.foreground || "#ffffff";
      el.style.fontFamily = term.options.fontFamily || "monospace";
      el.style.fontSize = (term.options.fontSize || 14) + "px";
      items = p.items; sel = 0;
      render();
      position();
    }}

    // keydown while visible; returns false when the key was consumed.
    function key(e) {{
      if (e.key === "ArrowDown") {{ sel = (sel + 1) % items.length; render(); return false; }}
      if (e.key === "ArrowUp") {{ sel = (sel - 1 + items.length) % items.length; render(); return false; }}
      if (e.key === "Tab" || e.key === "ArrowRight") {{ accept(sel, false); return false; }}
      if (e.key === "Enter") {{ accept(sel, true); return false; }}
      if (e.key === "Escape") {{ suppressed = true; hide(); return false; }}
      return true;
    }}

    term.onScroll(hide);
    window.addEventListener("resize", hide);
    return {{ update: update, hide: hide, visible: visible, key: key }};
  }})();

  // Copy/paste keyboard shortcuts. Bridge to Python so GTK owns the system
  // clipboard — navigator.clipboard cannot reliably read text copied in other
  // apps under WebKitGTK.
  term.attachCustomKeyEventHandler(function (e) {{
    if (e.type === "keydown" && window.sshpilotAC.visible()
        && !e.ctrlKey && !e.altKey && !e.metaKey
        && !window.sshpilotAC.key(e)) return false;
    if (e.type !== "keydown" || !(e.ctrlKey && e.shiftKey)) return true;
    const k = e.key.toLowerCase();
    if (k === "v") {{ send({{ type: "paste" }}); return false; }}
    if (k === "c" || k === "x") {{
      send({{ type: "copy", text: term.getSelection() }});
      term.focus();
      return false;
    }}
    return true;
  }});
</script></body></html>"""
