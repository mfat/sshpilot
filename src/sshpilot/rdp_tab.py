"""Embedded RDP tab: a remote desktop inside an sshPilot tab (gtk4-frdp).

The connection runs in the gtk4-frdp widget, which owns its FreeRDP session
on a thread of its own; this module only presents it. The saved password is
fetched from the daemon through the protected ``reveal_connection_password``
path (binary secret frames) right before connecting, handed to the widget's
write-only ``password`` property, and wiped. Where the tab is used instead of
FreeRDP's window is decided in ``rdp_view_policy``.
"""

from __future__ import annotations

import logging
import os
import threading
from gettext import gettext as _
from typing import Any, Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .rdp_view_policy import (  # noqa: E402
    CERT_ACCEPT_AND_STORE,
    CERT_ACCEPT_ONCE,
    CERT_REJECT,
    automatic_certificate_answer,
    display_properties,
    host_and_port,
)

logger = logging.getLogger(__name__)

_GTKFRDP_NAMESPACE = "GtkFrdp"
_GTKFRDP_VERSION = "4.0"
_available: Optional[bool] = None


def _bundled_libdir() -> str:
    """The private gtk4-frdp the deb/rpm/Arch packages install, or ''."""

    try:
        from .build_config import RDP_TAB_LIBDIR
    except ImportError:  # checkout, pip: no Meson install
        return ""
    return RDP_TAB_LIBDIR if RDP_TAB_LIBDIR and os.path.isdir(RDP_TAB_LIBDIR) else ""


_BUNDLED_LIBRARY = "libgtk4-frdp-4.0.so.0"


def _use_bundled_gtk4_frdp(libdir: str) -> None:
    """Put the packaged gtk4-frdp ahead of any system copy in GI's paths.

    Raises OSError when the library cannot load -- FreeRDP 3 or GTK missing or
    too old, since the packages only recommend them. GI would merely warn and
    hand back a namespace whose widget fails when a tab opens.
    """

    import ctypes

    ctypes.CDLL(os.path.join(libdir, _BUNDLED_LIBRARY))
    typelibs = os.path.join(libdir, "girepository-1.0")
    repository = gi._gi.Repository.get_default()
    if hasattr(repository, "prepend_search_path"):  # PyGObject >= 3.52
        repository.prepend_search_path(typelibs)
        repository.prepend_library_path(libdir)
        return
    # Older PyGObject is built on libgirepository-1.0, where these are static.
    gi.require_version("GIRepository", "2.0")
    from gi.repository import GIRepository

    GIRepository.Repository.prepend_search_path(typelibs)
    GIRepository.Repository.prepend_library_path(libdir)


def embedded_rdp_available() -> bool:
    """Whether gtk4-frdp (GtkFrdp-4.0) can be loaded. Cached."""

    global _available
    if _available is None:
        libdir = _bundled_libdir()
        try:
            if libdir:
                _use_bundled_gtk4_frdp(libdir)
            gi.require_version(_GTKFRDP_NAMESPACE, _GTKFRDP_VERSION)
            from gi.repository import GtkFrdp

            GtkFrdp.Display.__gtype__  # noqa: B018
            _available = True
        except Exception as error:  # ImportError, ValueError, GLib/RuntimeError
            logger.info("gtk4-frdp (GtkFrdp-%s) not usable (%s); RDP opens in FreeRDP's window",
                        _GTKFRDP_VERSION, error)
            _available = False
    return _available


class RdpTab(Gtk.Box):
    """A tab showing one RDP connection."""

    def __init__(
        self,
        window: Any,
        connection: Any,
        data: dict,
        *,
        open_in_window: Callable[[Any], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self.connection = connection
        # Full settings from the daemon (rdp_view_policy.connection_data_from_details).
        self.data = dict(data)
        self._open_in_window = open_in_window
        self._display = None
        self._connected = False
        self._closing = False
        self._last_error: Optional[str] = None

        self._overlay = Gtk.Overlay(hexpand=True, vexpand=True)
        self.append(self._overlay)

        self._status = Adw.StatusPage(hexpand=True, vexpand=True)
        self._status.add_css_class("background")
        buttons = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        self._reconnect_button = Gtk.Button(label=_("Reconnect"))
        self._reconnect_button.add_css_class("pill")
        self._reconnect_button.add_css_class("suggested-action")
        self._reconnect_button.connect("clicked", lambda *_a: self.connect_session())
        self._window_button = Gtk.Button(label=_("Open in FreeRDP Window"))
        self._window_button.add_css_class("pill")
        self._window_button.connect("clicked", self._on_open_in_window)
        buttons.append(self._reconnect_button)
        buttons.append(self._window_button)
        self._status.set_child(buttons)
        self._overlay.add_overlay(self._status)

    # ---- public ---------------------------------------------------------

    def connect_session(self) -> None:
        """Connect (or reconnect). Fetches the saved password first."""

        self._closing = False
        self._show_status(
            "network-transmit-receive-symbolic",
            _("Connecting…"),
            self._target_text(),
            buttons=False,
        )
        self._reveal_password(self._start)

    def close_session(self) -> None:
        """End the session; used when the tab closes."""

        self._closing = True
        if self._display is not None:
            try:
                self._display.close()
            except Exception:
                logger.debug("Closing the RDP display failed", exc_info=True)

    @property
    def connected(self) -> bool:
        return self._connected

    # ---- connection -----------------------------------------------------

    def _target_text(self) -> str:
        host, port = host_and_port(dict(self.data))
        host = host or str(getattr(self.connection, "hostname", "") or "")
        return f"{host}:{port}" if port != 3389 else host

    def _reveal_password(self, then: Callable[[Optional[str]], None]) -> None:
        """Fetch the saved password off the UI thread (``None`` if none)."""

        client = getattr(self.window, "client", None)
        ready = getattr(self.window, "_daemon_ready", None)
        if client is None or not callable(getattr(client, "reveal_connection_password", None)) \
                or not (callable(ready) and ready()):
            then(None)
            return

        connection = self.connection

        def worker() -> None:
            value: Optional[str] = None
            try:
                from .api.connection_identity import connection_id_for

                secret = client.reveal_connection_password(connection_id_for(connection))
                try:
                    value = secret.decode("utf-8") or None
                finally:
                    secret[:] = b"\0" * len(secret)
                    secret.clear()
            except Exception:
                # No saved password, or no secret backend: the server asks.
                logger.debug("No saved RDP password available", exc_info=True)
            GLib.idle_add(lambda: (then(value), False)[1])

        threading.Thread(target=worker, name="rdp-password", daemon=True).start()

    def _start(self, password: Optional[str]) -> None:
        if self._closing:
            return
        gi.require_version(_GTKFRDP_NAMESPACE, _GTKFRDP_VERSION)
        from gi.repository import GtkFrdp

        data = dict(self.data)
        host, port = host_and_port(data)
        host = host or str(getattr(self.connection, "hostname", "") or "")

        if self._display is not None:
            self._overlay.set_child(None)
            self._display = None

        display = GtkFrdp.Display()
        for name, value in display_properties(data).items():
            display.set_property(name.replace("_", "-"), value)
        username = str(data.get("username") or "").strip()
        domain = str(data.get("domain") or "").strip()
        if username:
            display.props.username = username
        if domain:
            display.props.domain = domain
        if password:
            display.props.password = password  # write-only; not kept here

        display.connect("rdp-connected", self._on_connected)
        display.connect("rdp-disconnected", self._on_disconnected)
        display.connect("rdp-error", self._on_error)
        display.connect("rdp-auth-failure", self._on_error)
        display.connect("rdp-needs-certificate-verification", self._on_certificate, False)
        display.connect("rdp-needs-certificate-change-verification",
                        self._on_changed_certificate)
        display.connect("rdp-needs-authentication", self._on_authentication)

        self._display = display
        self._connected = False
        self._last_error: Optional[str] = None
        self._overlay.set_child(display)
        display.open_host(host, port)

    # ---- widget signals ---------------------------------------------------

    def _on_connected(self, display) -> None:
        if display is not self._display:
            return
        self._connected = True
        self._status.set_visible(False)
        display.grab_focus()

    def _on_error(self, display, message: str) -> None:
        if display is self._display:
            self._last_error = message

    def _on_disconnected(self, display) -> None:
        if display is not self._display or self._closing:
            return
        was_connected = self._connected
        self._connected = False
        if self._last_error:
            # FreeRDP's text is shown unchanged as the diagnostic.
            self._show_status("dialog-error-symbolic",
                              _("Could Not Connect") if not was_connected else _("Connection Lost"),
                              self._last_error)
        else:
            self._show_status("network-offline-symbolic", _("Disconnected"),
                              _("The remote session ended."))

    def _show_status(self, icon: str, title: str, description: str, *, buttons: bool = True) -> None:
        self._status.set_icon_name(icon)
        self._status.set_title(title)
        self._status.set_description(GLib.markup_escape_text(description or ""))
        self._status.get_child().set_visible(buttons)
        self._status.set_visible(True)

    def _on_open_in_window(self, _button) -> None:
        self.close_session()
        self._open_in_window(self.connection)

    # ---- prompts -----------------------------------------------------------

    def _certificate_policy(self) -> str:
        return str(dict(self.data).get("cert_policy") or "prompt")

    def _on_certificate(self, display, host, port, common_name, subject, issuer,
                        fingerprint, flags, changed=False, old=None) -> None:
        automatic = automatic_certificate_answer(self._certificate_policy(), changed=changed)
        finish = (display.certificate_change_verify_ex_finish if changed
                  else display.certificate_verify_ex_finish)
        if automatic is not None:
            finish(automatic)
            return

        if changed:
            heading = _("Server Certificate Changed")
            body = _("The certificate of {host} is different from the one accepted before. "
                     "This can mean the server was reinstalled, or that someone is "
                     "intercepting the connection.").format(host=host)
        else:
            heading = _("Unknown Server Certificate")
            body = _("The identity of {host} cannot be verified. Check the "
                     "fingerprint before you continue.").format(host=host)
        details = _("Common name: {name}\nIssuer: {issuer}\nFingerprint: {fingerprint}").format(
            name=common_name, issuer=issuer, fingerprint=fingerprint)

        dialog = Adw.AlertDialog(heading=heading, body=f"{body}\n\n{details}")
        dialog.add_response("reject", _("Cancel"))
        dialog.add_response("once", _("Accept Once"))
        dialog.add_response("store", _("Accept and Remember"))
        dialog.set_response_appearance(
            "store", Adw.ResponseAppearance.DESTRUCTIVE if changed else Adw.ResponseAppearance.SUGGESTED)
        dialog.set_close_response("reject")
        dialog.set_default_response("reject" if changed else "store")
        answers = {"reject": CERT_REJECT, "once": CERT_ACCEPT_ONCE, "store": CERT_ACCEPT_AND_STORE}
        # The session thread waits for the answer; the UI keeps running.
        dialog.connect("response", lambda _d, response: finish(answers.get(response, CERT_REJECT)))
        dialog.present(self.window)

    def _on_changed_certificate(self, display, host, port, common_name, subject, issuer,
                                fingerprint, old_subject, old_issuer, old_fingerprint,
                                flags) -> None:
        self._on_certificate(display, host, port, common_name, subject, issuer,
                             fingerprint, flags, changed=True)

    def _on_authentication(self, display) -> None:
        data = dict(self.data)
        dialog = Adw.AlertDialog(heading=_("Log In"),
                                 body=_("Enter your credentials for {host}.").format(
                                     host=self._target_text()))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        user = Gtk.Entry(text=str(data.get("username") or ""), placeholder_text=_("Username"),
                         activates_default=True)
        domain = Gtk.Entry(text=str(data.get("domain") or ""), placeholder_text=_("Domain"),
                           activates_default=True)
        password = Gtk.PasswordEntry(placeholder_text=_("Password"), show_peek_icon=True,
                                     activates_default=True)
        for entry in (user, domain, password):
            box.append(entry)
        dialog.set_extra_child(box)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("login", _("Log In"))
        dialog.set_response_appearance("login", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("login")
        dialog.set_close_response("cancel")

        def on_response(_dialog, response: str) -> None:
            if response == "login":
                display.authenticate_finish(user.get_text(), password.get_text(),
                                            domain.get_text())
            else:
                display.authenticate_finish(None, None, None)  # cancels

        dialog.connect("response", on_response)
        dialog.present(self.window)
        if user.get_text():
            password.grab_focus()
