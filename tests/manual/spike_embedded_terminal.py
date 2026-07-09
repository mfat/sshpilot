#!/usr/bin/env python3
"""Manual spike: embedded (Cursor-model) xterm.js terminal, no server.

Run on a machine WITH A DISPLAY (this cannot run headless — it needs WebKitGTK):

    python3 tests/manual/spike_embedded_terminal.py            # runs /bin/bash
    python3 tests/manual/spike_embedded_terminal.py ssh host   # runs any argv

What it proves end-to-end, in one process:
  * xterm.js loaded via WebView.load_html with the bundled assets inlined (no CDN,
    no resource:// — WebKit doesn't resolve GIO resource URIs).
  * JS -> Python over a WebKit UserContentManager script-message handler
    (window.webkit.messageHandlers.sshpilotPty.postMessage).
  * Python -> JS via batched evaluate_javascript("term.write(...)").
  * A real PTY child (via sshpilot.xterm_pty_bridge.XtermPtyBridge) — the SAME
    engine unit-tested headlessly in tests/test_xterm_pty_bridge.py.

Success = you can type in a real shell inside the window, resize reflows, and
`ss -tlnp` shows NO new 127.0.0.1 listener.
"""
import json
import os
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("WebKit", "6.0")
gi.require_version("Vte", "3.91")
from gi.repository import Gtk, WebKit, GLib  # noqa: E402

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from sshpilot.xterm_pty_bridge import XtermPtyBridge  # noqa: E402

ASSETS = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..",
                 "sshpilot", "vendor", "pyxtermjs", "xterm")
)


def _read(name):
    with open(os.path.join(ASSETS, name), "r", encoding="utf-8") as f:
        return f.read()


def build_html():
    """One self-contained HTML doc with xterm.js + addons inlined."""
    xterm_js = _read("xterm.js")
    xterm_css = _read("xterm.css")
    fit_js = _read(os.path.join("addons", "fit", "xterm-addon-fit.js"))
    return f"""<!doctype html><html><head><meta charset="utf-8"/>
<style>{xterm_css}
  html,body{{margin:0;height:100%;background:#000}} #t{{height:100vh}}</style>
<script>{xterm_js}</script>
<script>{fit_js}</script>
</head><body><div id="t"></div><script>
  const term = new Terminal({{cursorBlink:true, fontSize:14,
     theme:{{background:'#000000', foreground:'#e0e0e0'}}}});
  const fit = new FitAddon.FitAddon(); term.loadAddon(fit);
  window.term = term; window.fit = fit;
  term.open(document.getElementById('t'));
  function send(o){{ window.webkit.messageHandlers.sshpilotPty.postMessage(JSON.stringify(o)); }}
  function fitToScreen(){{ fit.fit(); send({{type:'resize', rows:term.rows, cols:term.cols}}); }}
  term.onData(d => send({{type:'input', data:d}}));
  window.onresize = () => fitToScreen();
  requestAnimationFrame(() => {{ fitToScreen(); send({{type:'ready'}}); }});
</script></body></html>"""


class Spike:
    def __init__(self, argv):
        self.argv = argv
        self.bridge = XtermPtyBridge(on_output=self._write_to_term, flush_ms=12)
        self._webview = None
        self._ready = False

    def _write_to_term(self, chunk):
        # Python -> JS: batched write. json.dumps handles all escaping.
        script = "window.term && window.term.write(%s);" % json.dumps(chunk)
        try:
            self._webview.evaluate_javascript(script, len(script), None, None, None, None, None)
        except Exception as e:
            print("evaluate_javascript failed:", e)

    def _on_message(self, ucm, js_value):
        # WebKit6 delivers a JavaScriptCore.Value; older returns via .get_js_value()
        try:
            raw = js_value.to_json(0) if hasattr(js_value, "to_json") else js_value.get_js_value().to_json(0)
            payload = json.loads(raw)
            if isinstance(payload, str):
                payload = json.loads(payload)
        except Exception as e:
            print("bad message:", e, js_value)
            return
        t = payload.get("type")
        if t == "ready" and not self._ready:
            self._ready = True
            self.bridge.spawn(
                self.argv,
                env=[f"{k}={v}" for k, v in os.environ.items()
                     if k in ("PATH", "HOME", "USER", "LANG", "TERM")] or None,
                rows=payload.get("rows", 24), cols=payload.get("cols", 80),
                on_spawned=lambda pid, err: print("spawned pid=%s err=%s" % (pid, err)),
            )
        elif t == "input":
            self.bridge.write(payload.get("data", ""))
        elif t == "resize":
            self.bridge.resize(payload.get("rows", 24), payload.get("cols", 80))

    def run(self):
        app = Gtk.Application(application_id="io.github.mfat.sshpilot.spike")

        def on_activate(a):
            ucm = WebKit.UserContentManager()
            # WebKit 6 signature varies across versions; try both.
            try:
                ucm.register_script_message_handler("sshpilotPty", None)
            except TypeError:
                ucm.register_script_message_handler("sshpilotPty")
            ucm.connect("script-message-received::sshpilotPty", self._on_message)
            self._webview = WebKit.WebView(user_content_manager=ucm)
            self._webview.get_settings().set_property("enable-javascript", True)
            # localhost base URI => secure context => navigator.clipboard available
            self._webview.load_html(build_html(), "http://localhost/")
            win = Gtk.ApplicationWindow(application=a, title="Embedded terminal spike")
            win.set_default_size(900, 560)
            win.set_child(self._webview)
            win.present()

        app.connect("activate", on_activate)
        app.run(None)
        self.bridge.close()


if __name__ == "__main__":
    argv = sys.argv[1:] or ["/bin/bash", "--norc", "-i"]
    print("Spike argv:", argv)
    Spike(argv).run()
