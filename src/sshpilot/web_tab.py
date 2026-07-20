"""A minimal embedded web-page tab (WebKit 6) with a system-browser fallback.

Used by plugins via ``ctx.ui.open_web_tab`` to show a discovered web UI —
e.g. a Docker container's published port tunnelled to localhost — inside the
app. Deliberately tiny: back / reload / read-only URL / "Open in browser".
"""

from __future__ import annotations

import functools
import logging
from gettext import gettext as _

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, Gtk, Pango  # noqa: E402

logger = logging.getLogger(__name__)


def open_url_in_browser(url: str) -> bool:
    """Open ``url`` in the system default browser (portal-friendly first)."""
    try:
        Gio.AppInfo.launch_default_for_uri(url, None)
        return True
    except Exception as exc:
        logger.debug("Default URI handler failed for %s: %s", url, exc)
    try:
        import webbrowser

        return bool(webbrowser.open(url))
    except Exception as exc:
        logger.error("Failed to open %s in a browser: %s", url, exc)
        return False


@functools.lru_cache(maxsize=1)
def webkit_available() -> bool:
    """Whether WebKitGTK 6 (the GTK4 API) is importable."""
    try:
        gi.require_version("WebKit", "6.0")
        from gi.repository import WebKit  # noqa: F401

        return True
    except Exception:
        return False


class WebTab(Gtk.Box):
    """A WebView with a slim toolbar. Construct only when ``webkit_available()``."""

    def __init__(self, url: str):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        from gi.repository import WebKit

        self._webview = WebKit.WebView()
        try:
            # Web UIs on https-looking ports are reached through a localhost
            # tunnel, so the certificate can never match the hostname.
            self._webview.get_network_session().set_tls_errors_policy(
                WebKit.TLSErrorsPolicy.IGNORE)
        except Exception:
            logger.debug("Could not relax TLS policy", exc_info=True)
        # Same trick as the terminal backend: returning True suppresses
        # WebKit's built-in context menu.
        self._webview.connect("context-menu", lambda *_a: True)
        self._webview.set_vexpand(True)
        self._webview.connect("load-changed", self._on_load_changed)
        self._webview.connect("notify::uri", self._on_uri_changed)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        bar.add_css_class("toolbar")
        self._back = Gtk.Button.new_from_icon_name("go-previous-symbolic")
        self._back.add_css_class("flat")
        self._back.set_tooltip_text(_("Back"))
        self._back.set_sensitive(False)
        self._back.connect("clicked", lambda _b: self._webview.go_back())
        bar.append(self._back)
        reload_btn = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        reload_btn.add_css_class("flat")
        reload_btn.set_tooltip_text(_("Reload"))
        reload_btn.connect("clicked", lambda _b: self._webview.reload())
        bar.append(reload_btn)
        self._url_lbl = Gtk.Label(label=url, xalign=0, hexpand=True)
        self._url_lbl.add_css_class("dim-label")
        self._url_lbl.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        bar.append(self._url_lbl)
        ext = Gtk.Button.new_from_icon_name("adw-external-link-symbolic")
        ext.add_css_class("flat")
        ext.set_tooltip_text(_("Open in browser"))
        ext.connect(
            "clicked",
            lambda _b: open_url_in_browser(self._webview.get_uri() or url))
        bar.append(ext)
        self.append(bar)
        self.append(self._webview)
        self._webview.load_uri(url)

    def _on_load_changed(self, view, _event) -> None:
        self._back.set_sensitive(view.can_go_back())

    def _on_uri_changed(self, view, _pspec) -> None:
        uri = view.get_uri()
        if uri:
            self._url_lbl.set_text(uri)
