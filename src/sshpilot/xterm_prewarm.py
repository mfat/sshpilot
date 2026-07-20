"""Pre-warmed WebView pool for the embedded PyXterm terminal backend."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

_POOL_TARGET = 1
_WARM_TIMEOUT_SEC = 30


@dataclass
class _ShellEntry:
    webview: Any
    ucm: Any
    js_ready: bool = False
    owner: Any = None
    loaded: bool = False
    warm_timeout_id: Optional[int] = None


class XtermShellPool:
    """Keep one or more xterm.js shells hot so new tabs skip load_html."""

    _ready: list[_ShellEntry] = []
    _warming: list[_ShellEntry] = []
    _by_ucm: dict[int, _ShellEntry] = {}

    @classmethod
    def schedule_prewarm(cls, config) -> None:
        try:
            backend = (config.get_setting("terminal.backend", "vte") or "vte").lower()
        except Exception:
            backend = "vte"
        if backend not in ("pyxterm", "pyxterm2"):
            return
        try:
            import gi

            gi.require_version("GLib", "2.0")
            from gi.repository import GLib

            GLib.idle_add(cls._ensure_warming_idle)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to schedule xterm pool prewarm: %s", exc)

    @classmethod
    def _ensure_warming_idle(cls) -> bool:
        cls.ensure_warming()
        return False

    @classmethod
    def ensure_warming(cls) -> None:
        total = len(cls._ready) + len(cls._warming)
        while total < _POOL_TARGET:
            cls._start_warming()
            total += 1

    @classmethod
    def acquire_for_owner(cls, owner) -> Optional[_ShellEntry]:
        while cls._ready:
            entry = cls._ready.pop(0)
            if not entry.js_ready:
                cls._discard_entry(entry)
                continue
            entry.owner = owner
            # Queued before any spawn output; WebKit runs JS FIFO on this view.
            cls._reset_entry(entry)
            cls.ensure_warming()
            return entry
        # Prefer an in-progress warmer over starting a second cold load_html.
        # The tab inherits the already-loading WebView; ready still arrives via
        # _dispatch_message once xterm.js finishes (js_ready may still be False).
        while cls._warming:
            entry = cls._warming.pop(0)
            if entry.webview is None or entry.ucm is None:
                cls._discard_entry(entry)
                continue
            cls._cancel_warm_timeout(entry)
            entry.owner = owner
            if entry.js_ready:
                cls._reset_entry(entry)
            cls.ensure_warming()
            return entry
        return None

    @classmethod
    def create_for_owner(cls, owner, WebKit) -> _ShellEntry:
        entry = cls._create_entry(WebKit)
        entry.owner = owner
        return entry

    @classmethod
    def release(cls, entry: Optional[_ShellEntry]) -> None:
        if entry is None:
            return
        entry.owner = None
        try:
            parent = entry.webview.get_parent()
            if parent is not None and hasattr(parent, "set_child"):
                parent.set_child(None)
        except Exception:  # noqa: BLE001
            pass
        cls._reset_entry(entry)
        if entry.js_ready and entry.loaded:
            if len(cls._ready) < _POOL_TARGET:
                cls._ready.append(entry)
            else:
                cls._discard_entry(entry)
        else:
            cls._discard_entry(entry)
        cls.ensure_warming()

    @classmethod
    def mark_loaded(cls, entry: Optional[_ShellEntry]) -> None:
        if entry is not None:
            entry.loaded = True

    @classmethod
    def _discard_entry(cls, entry: Optional[_ShellEntry]) -> None:
        if entry is None:
            return
        cls._cancel_warm_timeout(entry)
        try:
            cls._ready.remove(entry)
        except ValueError:
            pass
        try:
            cls._warming.remove(entry)
        except ValueError:
            pass
        cls._by_ucm.pop(id(entry.ucm), None)
        try:
            parent = entry.webview.get_parent()
            if parent is not None and hasattr(parent, "set_child"):
                parent.set_child(None)
        except Exception:  # noqa: BLE001
            pass
        entry.webview = None
        entry.ucm = None
        entry.owner = None

    @classmethod
    def _cancel_warm_timeout(cls, entry: _ShellEntry) -> None:
        if entry.warm_timeout_id is None:
            return
        try:
            import gi

            gi.require_version("GLib", "2.0")
            from gi.repository import GLib

            GLib.source_remove(entry.warm_timeout_id)
        except Exception:  # noqa: BLE001
            pass
        entry.warm_timeout_id = None

    @classmethod
    def _on_warm_timeout(cls, entry: _ShellEntry) -> bool:
        entry.warm_timeout_id = None
        if entry.js_ready or entry.owner is not None:
            return False
        if entry in cls._warming:
            logger.debug("Discarding PyXterm shell that never reached ready")
            cls._discard_entry(entry)
            cls.ensure_warming()
        return False

    @classmethod
    def _start_warming(cls) -> None:
        try:
            import gi

            gi.require_version("GLib", "2.0")
            gi.require_version("WebKit", "6.0")
            from gi.repository import GLib, WebKit

            entry = cls._create_entry(WebKit)
            cls._warming.append(entry)
            cls._load_html(entry)
            entry.warm_timeout_id = GLib.timeout_add_seconds(
                _WARM_TIMEOUT_SEC, cls._on_warm_timeout, entry
            )
            logger.debug("Started warming PyXterm shell for pool")
        except Exception as exc:  # noqa: BLE001
            logger.debug("PyXterm pool warm failed: %s", exc)

    @classmethod
    def _create_entry(cls, WebKit) -> _ShellEntry:
        from gi.repository import Gdk

        ucm = WebKit.UserContentManager()
        try:
            ucm.register_script_message_handler("sshpilotPty", None)
        except TypeError:
            ucm.register_script_message_handler("sshpilotPty")
        ucm.connect("script-message-received::sshpilotPty", cls._dispatch_message)
        webview = WebKit.WebView(user_content_manager=ucm)
        try:
            settings = webview.get_settings()
            if settings:
                settings.set_property("enable-javascript", True)
        except Exception:  # noqa: BLE001
            pass
        try:
            if hasattr(webview, "set_background_color"):
                black = Gdk.RGBA()
                black.parse("#000000")
                webview.set_background_color(black)
        except Exception:  # noqa: BLE001
            pass
        entry = _ShellEntry(webview=webview, ucm=ucm)
        cls._by_ucm[id(ucm)] = entry
        return entry

    @classmethod
    def _load_html(cls, entry: _ShellEntry) -> None:
        from .xterm_shell import build_shell_html

        entry.loaded = True
        entry.webview.load_html(build_shell_html(), "http://localhost/")

    @classmethod
    def load_for_entry(cls, entry: _ShellEntry) -> None:
        if entry.loaded:
            return
        cls._load_html(entry)

    @classmethod
    def _reset_entry(cls, entry: _ShellEntry) -> None:
        if entry.webview is None:
            return
        try:
            script = (
                "(function(){"
                "if(window.term){window.term.clear();window.term.reset();}"
                "return true;"
                "})();"
            )
            if hasattr(entry.webview, "evaluate_javascript"):
                entry.webview.evaluate_javascript(script, len(script), None, None, None, None, None)
            elif hasattr(entry.webview, "run_javascript"):
                entry.webview.run_javascript(script, None, None, None)
        except Exception:  # noqa: BLE001
            pass

    @classmethod
    def _dispatch_message(cls, ucm, js_value) -> None:
        entry = cls._by_ucm.get(id(ucm))
        if entry is None:
            return
        payload = cls._parse_payload(js_value)
        if payload is None:
            return
        if payload.get("type") == "ready" and not entry.js_ready:
            entry.js_ready = True
            cls._cancel_warm_timeout(entry)
            # Only unowned warmers enter the pool; owned entries (create_for_owner)
            # must never be appended or discarded here.
            if entry.owner is None and entry in cls._warming:
                cls._warming.remove(entry)
                if len(cls._ready) < _POOL_TARGET:
                    cls._ready.append(entry)
                    logger.debug("PyXterm shell entered ready pool")
                else:
                    cls._discard_entry(entry)
        owner = entry.owner
        if owner is not None and hasattr(owner, "_on_pty_message"):
            owner._on_pty_message(ucm, js_value)

    @staticmethod
    def _parse_payload(js_value) -> Optional[dict]:
        try:
            if hasattr(js_value, "to_json"):
                raw = js_value.to_json(0)
            else:
                raw = js_value.get_js_value().to_json(0)
            payload = json.loads(raw)
            if isinstance(payload, str):
                payload = json.loads(payload)
            return payload
        except Exception:  # noqa: BLE001
            return None


def schedule_xterm_prewarm(config) -> None:
    """Backward-compatible entry point used by main.py."""
    XtermShellPool.schedule_prewarm(config)
