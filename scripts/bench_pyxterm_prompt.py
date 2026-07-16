#!/usr/bin/env python3
"""Objectively measure PyXterm time-to-prompt for a local shell.

Uses the production shell HTML + PTY bridge (same path as the app). Timestamps:

  t0           start of load_html + spawn (parallel, matching the app)
  t_pty        first coalesced PTY output delivered to Python
  t_ready      xterm.js ``ready`` script-message
  t_paint_js   first ``term.write`` executed inside the WebView (closest to
               "prompt appeared" we can observe without a frame capture)

Cold trial = first WebView in the process (WebProcess spawn). Warm trials reuse
the already-running WebProcess.

Usage (needs a display + WebKitGTK 6):

    python3 scripts/bench_pyxterm_prompt.py --trials 8
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from typing import List, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("WebKit", "6.0")
gi.require_version("Vte", "3.91")
from gi.repository import Adw, GLib, Gtk, WebKit  # noqa: E402

# Repo root on sys.path
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from sshpilot.xterm_pty_bridge import XtermPtyBridge  # noqa: E402
from sshpilot.xterm_shell import build_shell_html  # noqa: E402


@dataclass
class Trial:
    cold: bool
    t_pty_ms: Optional[float] = None
    t_ready_ms: Optional[float] = None
    t_paint_ms: Optional[float] = None
    t_flush_ms: Optional[float] = None
    prompt_chars: int = 0
    error: str = ""


@dataclass
class Runner:
    argv: List[str]
    trials_left: int
    results: List[Trial] = field(default_factory=list)
    # When True, match post-fix order: flush preready before theme JS.
    # When False, match pre-fix: theme JS then flush (extra JS queue hop).
    flush_before_theme: bool = True
    # Prewarmed: load HTML once, then each trial only respawns the shell
    # (models XtermShellPool hot WebView — ready already true).
    prewarmed: bool = False
    _app: Optional[Adw.Application] = None
    _window: Optional[Gtk.Window] = None
    _bridge: Optional[XtermPtyBridge] = None
    _webview: Optional[WebKit.WebView] = None
    _t0: float = 0.0
    _trial: Optional[Trial] = None
    _preready: List[str] = field(default_factory=list)
    _js_ready: bool = False
    _shell_ready: bool = False  # WebView reached ready at least once
    _painted: bool = False
    _timeout_id: Optional[int] = None
    _prewarm_pending: bool = False

    def run(self) -> List[Trial]:
        app_id = f"io.github.mfat.sshpilot.benchprompt{os.getpid()}"
        self._app = Adw.Application(application_id=app_id)
        self._app.connect("activate", self._on_activate)
        self._app.run(None)
        return self.results

    def _on_activate(self, _app):
        # Keep the app alive across ApplicationWindow destroy between trials.
        self._app.hold()
        if self.prewarmed:
            self._create_webview()
            self._prewarm_pending = True
            html = build_shell_html()
            self._webview.load_html(html, "http://localhost/")
            # First ready (no shell yet) arms the trial loop.
        else:
            self._start_trial()

    def _create_webview(self):
        ucm = WebKit.UserContentManager()
        try:
            ucm.register_script_message_handler("sshpilotPty", None)
        except TypeError:
            ucm.register_script_message_handler("sshpilotPty")
        ucm.connect("script-message-received::sshpilotPty", self._on_message)

        webview = WebKit.WebView(user_content_manager=ucm)
        try:
            settings = webview.get_settings()
            if settings:
                settings.set_property("enable-javascript", True)
        except Exception:
            pass
        self._webview = webview
        win = Gtk.ApplicationWindow(application=self._app, title="bench-pyxterm-prompt")
        win.set_default_size(900, 500)
        win.set_child(webview)
        win.present()
        self._window = win

    def _start_trial(self):
        if self.trials_left <= 0:
            try:
                self._app.release()
            except Exception:
                pass
            self._app.quit()
            return
        self.trials_left -= 1
        cold = (not self.prewarmed) and len(self.results) == 0
        self._trial = Trial(cold=cold)
        self._preready = []
        self._painted = False
        self._bridge = None

        if not self.prewarmed:
            if self._window is not None:
                self._window.destroy()
                self._window = None
                self._webview = None
            self._js_ready = False
            self._create_webview()

        # Safety timeout per trial
        self._timeout_id = GLib.timeout_add(8000, self._on_timeout)

        # Match production: start the clock, load HTML and spawn in parallel
        # (prewarmed mode: WebView already ready — only spawn is on the clock).
        self._t0 = time.perf_counter()
        if self.prewarmed:
            self._js_ready = True
            # Clear leftover scrollback so first-paint is from this spawn.
            script = (
                "window.term && window.term.clear();"
                "window.term && window.term.reset();"
            )
            try:
                self._webview.evaluate_javascript(
                    script, len(script), None, None, None, None, None
                )
            except Exception:
                pass
            self._spawn_shell()
        else:
            self._js_ready = False
            html = build_shell_html()
            self._webview.load_html(html, "http://localhost/")
            self._spawn_shell()

    def _spawn_shell(self):
        flush_ms = 16
        # Detect older bridge default if present by inspecting signature defaults
        try:
            import inspect
            default = inspect.signature(XtermPtyBridge.__init__).parameters["flush_ms"].default
            if isinstance(default, int):
                flush_ms = default
        except Exception:
            pass

        self._bridge = XtermPtyBridge(
            on_output=self._on_pty_output,
            on_exit=lambda _s: None,
            flush_ms=flush_ms,
        )
        env = [
            f"PATH={os.environ.get('PATH', '')}",
            f"HOME={os.environ.get('HOME', '')}",
            f"USER={os.environ.get('USER', '')}",
            f"LANG={os.environ.get('LANG', 'C.UTF-8')}",
            "TERM=xterm-256color",
            "PS1=BENCH$ ",
        ]
        self._bridge.spawn(self.argv, env=env, cwd=os.environ.get("HOME"), rows=24, cols=80)

    def _on_pty_output(self, chunk: str):
        if self._trial is None:
            return
        if self._trial.t_pty_ms is None:
            self._trial.t_pty_ms = (time.perf_counter() - self._t0) * 1000.0
        if self._js_ready:
            self._write_to_term(chunk, bulk=False)
        else:
            self._preready.append(chunk)

    def _write_to_term(self, text: str, *, bulk: bool):
        if self._webview is None or not text:
            return
        if self._trial and self._trial.t_flush_ms is None:
            self._trial.t_flush_ms = (time.perf_counter() - self._t0) * 1000.0
            self._trial.prompt_chars = len(text)
        # Prefer production bulk helper when present; fall back to term.write.
        if bulk:
            import base64
            b64 = base64.b64encode(text.encode("utf-8", "replace")).decode("ascii")
            script = (
                "(() => {"
                "  const mark = () => {"
                "    try { window.webkit.messageHandlers.sshpilotPty.postMessage("
                "      JSON.stringify({type:'first-paint'})); } catch (e) {}"
                "  };"
                "  if (window.termWriteB64) { window.termWriteB64(%s); mark(); }"
                "  else if (window.term) { window.term.write(%s); mark(); }"
                "})();"
                % (json.dumps(b64), json.dumps(text))
            )
        else:
            script = (
                "(() => {"
                "  if (window.term) window.term.write(%s);"
                "  try { window.webkit.messageHandlers.sshpilotPty.postMessage("
                "    JSON.stringify({type:'first-paint'})); } catch (e) {}"
                "})();" % json.dumps(text)
            )
        try:
            self._webview.evaluate_javascript(
                script, len(script), None, None, None, None, None
            )
        except Exception as exc:
            if self._trial:
                self._trial.error = f"evaluate_javascript: {exc}"

    def _on_message(self, _ucm, js_value):
        try:
            raw = js_value.to_json(0) if hasattr(js_value, "to_json") else js_value.get_js_value().to_json(0)
            payload = json.loads(raw)
            if isinstance(payload, str):
                payload = json.loads(payload)
        except Exception:
            return
        kind = payload.get("type")
        if kind == "ready":
            if self._prewarm_pending:
                self._prewarm_pending = False
                self._shell_ready = True
                self._js_ready = True
                # Prewarm complete — begin timed spawn trials.
                GLib.idle_add(self._start_trial_deferred)
                return
            if not self._js_ready:
                self._js_ready = True
                self._shell_ready = True
                if self._trial:
                    self._trial.t_ready_ms = (time.perf_counter() - self._t0) * 1000.0

                def _flush_preready():
                    if self._preready:
                        buffered, self._preready = "".join(self._preready), []
                        self._write_to_term(buffered, bulk=True)

                def _theme_js():
                    # Stand-in for apply_theme()/set_font() JS cost.
                    if self._webview is None:
                        return
                    script = (
                        "window.term && Object.assign(window.term.options, "
                        "{cursorBlink: true});"
                    )
                    try:
                        self._webview.evaluate_javascript(
                            script, len(script), None, None, None, None, None
                        )
                    except Exception:
                        pass

                if self.flush_before_theme:
                    _flush_preready()
                    _theme_js()
                else:
                    _theme_js()
                    _flush_preready()
                if self._bridge is not None:
                    rows = int(payload.get("rows") or 24)
                    cols = int(payload.get("cols") or 80)
                    self._bridge.resize(rows, cols)
        elif kind == "first-paint" and not self._painted:
            self._painted = True
            if self._trial:
                self._trial.t_paint_ms = (time.perf_counter() - self._t0) * 1000.0
            self._finish_trial()
        elif kind == "input" and self._bridge is not None:
            self._bridge.write(payload.get("data", ""))
        elif kind == "resize" and self._bridge is not None:
            self._bridge.resize(payload.get("rows", 24), payload.get("cols", 80))

    def _on_timeout(self):
        self._timeout_id = None
        if self._trial and self._trial.t_paint_ms is None:
            self._trial.error = self._trial.error or "timeout waiting for first-paint"
            # If ready happened and we have preready/live output, force a flush mark.
            if self._js_ready and self._trial.t_flush_ms is not None and not self._painted:
                self._trial.t_paint_ms = self._trial.t_flush_ms
            self._finish_trial()
        return False

    def _finish_trial(self):
        if self._timeout_id is not None:
            try:
                GLib.source_remove(self._timeout_id)
            except Exception:
                pass
            self._timeout_id = None
        if self._bridge is not None:
            try:
                self._bridge.close()
            except Exception:
                pass
            self._bridge = None
        if self._trial is not None:
            self.results.append(self._trial)
            self._trial = None
        if not self.prewarmed and self._window is not None:
            self._window.destroy()
            self._window = None
            self._webview = None
        # Settle before the next WebView/PTY so spawn_async callbacks cannot
        # race the next trial (and WebProcess stays warm without teardown races).
        GLib.timeout_add(250, self._start_trial_deferred)

    def _start_trial_deferred(self):
        self._start_trial()
        return False


def _fmt(ms: Optional[float]) -> str:
    return "  n/a" if ms is None else f"{ms:6.1f}"


def _summary(label: str, values: List[float]) -> str:
    if not values:
        return f"{label}: (no samples)"
    vals = sorted(values)
    med = statistics.median(vals)
    mean = statistics.fmean(vals)
    p90 = vals[max(0, int(round(0.9 * (len(vals) - 1))))]
    return f"{label}: n={len(vals)}  median={med:6.1f}ms  mean={mean:6.1f}ms  p90={p90:6.1f}ms  min={vals[0]:6.1f}ms  max={vals[-1]:6.1f}ms"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--trials", type=int, default=8, help="total trials (1 cold + rest warm)")
    ap.add_argument(
        "--prewarmed",
        action="store_true",
        help="load HTML once, then time spawn→paint only (models hot WebView pool)",
    )
    ap.add_argument(
        "--shell",
        default=os.environ.get("SHELL", "/bin/bash"),
        help="shell binary (default: $SHELL)",
    )
    args = ap.parse_args()

    # Interactive shell with a fixed prompt so output is predictable.
    argv = [args.shell, "--norc", "--noprofile", "-i"]
    html = build_shell_html()
    # Auto-detect fix: termWriteB64 + double-rAF ready land together in the fix.
    flush_before_theme = "termWriteB64" in html
    print(f"measuring with shell={argv!r} trials={args.trials}", flush=True)
    print(f"build_shell_html size: {len(html)} bytes", flush=True)
    print(
        f"mode: flush_before_theme={flush_before_theme} "
        f"(auto from HTML; False = pre-fix ordering)",
        flush=True,
    )

    print(f"prewarmed={args.prewarmed}", flush=True)
    runner = Runner(
        argv=argv,
        trials_left=max(1, args.trials),
        flush_before_theme=flush_before_theme,
        prewarmed=args.prewarmed,
    )
    results = runner.run()

    print()
    print(f"{'trial':>5} {'cold':>5} {'pty':>8} {'ready':>8} {'flush':>8} {'paint':>8}  note")
    for i, t in enumerate(results, 1):
        note = t.error or f"{t.prompt_chars} chars"
        print(
            f"{i:5d} {str(t.cold):>5} {_fmt(t.t_pty_ms)} {_fmt(t.t_ready_ms)} "
            f"{_fmt(t.t_flush_ms)} {_fmt(t.t_paint_ms)}  {note}"
        )

    paints = [t.t_paint_ms for t in results if t.t_paint_ms is not None]
    readies = [t.t_ready_ms for t in results if t.t_ready_ms is not None]
    ptys = [t.t_pty_ms for t in results if t.t_pty_ms is not None]
    warm_paints = [t.t_paint_ms for t in results if t.t_paint_ms is not None and not t.cold]
    cold_paints = [t.t_paint_ms for t in results if t.t_paint_ms is not None and t.cold]

    print()
    print(_summary("t_pty   (first PTY→Python)", ptys))
    print(_summary("t_ready (JS ready)", readies))
    print(_summary("t_paint (term.write ran)", paints))
    print(_summary("t_paint cold only", cold_paints))
    print(_summary("t_paint warm only", warm_paints))

    # Machine-readable line for diffing commits
    med_paint = statistics.median(paints) if paints else None
    med_warm = statistics.median(warm_paints) if warm_paints else None
    med_cold = statistics.median(cold_paints) if cold_paints else None
    print()
    print(
        "RESULT "
        f"paint_median_ms={med_paint} "
        f"paint_cold_ms={med_cold} "
        f"paint_warm_median_ms={med_warm} "
        f"ready_median_ms={statistics.median(readies) if readies else None} "
        f"pty_median_ms={statistics.median(ptys) if ptys else None}"
    )
    return 0 if paints else 1


if __name__ == "__main__":
    raise SystemExit(main())
