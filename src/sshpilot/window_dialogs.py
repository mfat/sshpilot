"""Config-related dialogs/windows for MainWindow.

Extracted from window.py as a mixin (matching WindowActions and the other
Window*Mixin modules) to shrink the window.py god-object. MainWindow inherits
this; methods keep their signatures and ``self.`` state access.

Also hosts the shared in-app SSH password / passphrase prompt helpers
(:func:`show_ssh_password_dialog`, :func:`_show_password_passphrase_dialog`)
and Wayland-safe modal parenting (:func:`resolve_app_modal_parent`,
:func:`present_for_modal_dialog`). ``window`` re-exports those for callers
that historically imported them from there.
"""

import logging
import os
import threading
from datetime import datetime
from typing import Any, Optional

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango
from gettext import gettext as _

from .accessibility import set_accessible_name, set_accessible_selected
from .gtk.secret_transfer_messages import (
    format_secret_transfer_error,
    format_secret_transfer_message,
    format_secret_transfer_messages,
)

logger = logging.getLogger(__name__)

# Minimum content width for export/import backup dialogs.
BACKUP_DIALOG_MIN_WIDTH = 520
# Export uses a normal modal window (wider than the Adw.Dialog sheets).
BACKUP_EXPORT_WINDOW_WIDTH = 800
# Cap the scrolled body so tall destinations (SSH + warning) scroll instead of
# growing past the screen; the window itself sizes to content below this.
BACKUP_EXPORT_CONTENT_MAX_HEIGHT = 640
BACKUP_EXPORT_CLAMP_MAX = 700

# Backup category keys (mirrors ``BackupManager.BACKUP_OPTION_KEYS``). The daemon owns
# the backup engine; the frontend only renders these as option rows.
_BACKUP_OPTION_KEYS = ("app_settings", "ssh_config", "known_hosts", "secrets", "private_keys")


def _normalize_backup_options(options=None):
    """Complete boolean option map — same semantics as ``BackupManager``."""
    merged = {
        "app_settings": True,
        "ssh_config": True,
        "known_hosts": True,
        "secrets": True,
        "private_keys": False,
    }
    if options:
        for key in _BACKUP_OPTION_KEYS:
            if key in options:
                merged[key] = bool(options[key])
    return merged


def _secrets_persist_for(parent):
    """Daemon-backed ``persists_secrets`` for a modal parent (default True).

    Resolves the window's ``secrets_controller`` and reads its cached state; a
    missing controller or absent cache reports ``True`` (the store checkbox is
    shown), matching the pre-daemon default.

    Must never issue a blocking ``load_state()`` RPC: this runs on the GTK
    thread while presenting a password dialog for an operation that may itself
    hold the daemon's secret-service lock (an SSH-server backup holds it
    across its connect, whose password/host-key prompts arrive here). A
    synchronous state query then deadlocks against that very operation — no
    prompt is shown until the export times out and releases the lock, which is
    exactly the late-prompt failure.

    The cache is primed when the controller is built
    (``MainWindow._prime_secret_state``) and refreshed by the vault gate and the
    per-connection unlock check, so it is normally warm. A cold one still falls
    back to ``True``, which on an SSH-Agent-Only backend offers a checkbox that
    stores nothing — so a cold read also kicks an off-thread refresh, and the
    next prompt is right."""
    controller = getattr(parent, "secrets_controller", None)
    if controller is None:
        try:
            if not isinstance(parent, Gtk.Window):
                parent = parent.get_root()
            controller = getattr(parent, "secrets_controller", None)
        except Exception:
            controller = None
    if controller is None:
        return True
    try:
        getter = getattr(controller, "state", None)
        cached = getter() if callable(getter) else None
        if cached is None:
            _refresh_secret_state_async(controller)
            return True
        return bool(getattr(cached, "persists_secrets", True))
    except Exception:
        return True


def _refresh_secret_state_async(controller) -> None:
    """Fill a cold secret-state cache in the background.

    Never blocks: the caller is on the GTK thread, possibly inside a nested
    dialog loop for an operation that holds the daemon's secret-service lock.
    """
    def _warm():
        try:
            controller.load_state()
        except Exception:
            logger.debug("Refreshing secret backend state failed", exc_info=True)

    try:
        threading.Thread(target=_warm, name="secret-state-refresh", daemon=True).start()
    except Exception:
        logger.debug("Could not start the secret state refresh", exc_info=True)


def parent_window(parent):
    """Return a ``Gtk.Window`` for APIs that require one, given a window *or* a widget.

    ``transient_for``, :class:`Gtk.FileDialog` and friends only accept a
    :class:`Gtk.Window`. Callers may hold a widget instead — Settings is an
    :class:`Adw.NavigationPage` inside the main window — so resolve the
    widget's root. Windows pass through untouched and ``None`` stays ``None``.

    This is the cheap structural unwrap. Use :func:`resolve_app_modal_parent`
    when you want the app's primary window regardless of what you were handed.
    """
    if parent is None or isinstance(parent, Gtk.Window):
        return parent
    try:
        return parent.get_root()
    except Exception:
        return None


def resolve_app_modal_parent(from_widget=None) -> "Gtk.Window":
    """Return the primary app window to use as a modal dialog parent.

    Use this before showing any modal from code that runs inside a secondary
    window — e.g. :class:`FileManagerWindow`, a plugin page tab, or a progress
    window — so the dialog stacks correctly on Wayland.

    Resolution order:

    1. ``Gtk.Application.window`` (the live :class:`MainWindow`)
    2. Any registered window whose class name is ``MainWindow``
    3. If ``from_widget`` is set: embedded root (``_embedded_parent.get_root()``),
       ``get_transient_for()``, ``get_root()``, or ``from_widget`` itself when it
       is a :class:`Gtk.Window`
    4. ``Gtk.Application.get_active_window()``

    Raises :class:`RuntimeError` if no suitable parent exists.

    See also :func:`present_for_modal_dialog` and :func:`show_ssh_password_dialog`.
    Pair with ``present_for_modal_dialog(parent)`` before ``dialog.present()``.
    """
    app = None
    if from_widget is not None:
        try:
            app = from_widget.get_application()
        except Exception:
            app = None
    if app is None:
        app = Gtk.Application.get_default()

    if app is not None:
        main_win = getattr(app, "window", None)
        if main_win is not None and isinstance(main_win, Gtk.Window):
            return main_win
        for win in app.get_windows():
            if win.__class__.__name__ == "MainWindow":
                return win

    if from_widget is not None:
        embedded_parent = getattr(from_widget, "_embedded_parent", None)
        if embedded_parent is not None:
            try:
                root = embedded_parent.get_root()
                if root is not None:
                    return root
            except Exception:
                pass
        try:
            transient = from_widget.get_transient_for()
            if transient is not None:
                return transient
        except Exception:
            pass
        try:
            root = from_widget.get_root()
            if root is not None:
                return root
        except Exception:
            pass
        if isinstance(from_widget, Gtk.Window):
            return from_widget

    if app is not None:
        try:
            active = app.get_active_window()
            if active is not None and isinstance(active, Gtk.Window):
                return active
        except Exception:
            pass

    raise RuntimeError("No modal parent window available")


def resolve_topmost_prompt_parent(windows, active_window, main_window):
    """Pick the window a routed askpass prompt should stack on.

    A visible **modal** secondary window (e.g. the SCP browse ``Adw.Window``)
    is blocking input, so the prompt must parent to it — not the main window,
    and not merely whatever GTK reports as "active" (a modal transient does not
    reliably become the active window on Wayland, so the main window can still
    win :func:`Gtk.Application.get_active_window`). Resolution:

    1. The active window, if it is a visible modal secondary window.
    2. Any other visible modal secondary window (``windows`` is expected in
       :func:`Gtk.Application.get_windows` order — most recently focused
       first — so the first match is the most recently focused).
    3. The active window, if visible (non-modal secondary window, e.g. FM).
    4. The main window.

    Pure function over already-extracted GTK state so it can be unit-tested.
    """
    def _visible(win):
        try:
            return bool(win.get_visible())
        except Exception:
            return False

    def _modal(win):
        try:
            return bool(win.get_modal())
        except Exception:
            return False

    modal_secondary = [
        w for w in (windows or [])
        if w is not main_window and _visible(w) and _modal(w)
    ]
    if modal_secondary:
        if active_window in modal_secondary:
            return active_window
        return modal_secondary[0]
    if active_window is not None and active_window is not main_window \
            and _visible(active_window):
        return active_window
    return main_window


def associate_window_with_parent_application(window, parent) -> None:
    """Register *window* with *parent*'s :class:`Gtk.Application`.

    A bare ``Adw.Window``/``Gtk.Window`` is absent from
    :func:`Gtk.Application.get_windows` unless it is explicitly associated —
    ``set_transient_for`` alone does not register it. Anything that presents
    itself as a blocking modal secondary (SCP browse, the connection editor,
    …) must call this so :func:`resolve_topmost_prompt_parent` can find it
    when a routed prompt (e.g. a vault master-password unlock) needs to stack
    above it instead of the main window.
    """
    try:
        app = parent.get_application() if parent is not None else None
    except Exception:
        app = None
    if app is None:
        return
    try:
        window.set_application(app)
    except Exception:
        pass


def present_for_modal_dialog(window: Gtk.Window) -> None:
    """Raise *window* before showing a modal child so it stacks on top (Wayland).

    Calls ``unminimize()`` and ``present()`` on *window*. Always invoke this on
    the parent returned by :func:`resolve_app_modal_parent` immediately before
    presenting a modal dialog.
    """
    try:
        window.unminimize()
    except Exception:
        pass
    try:
        window.present()
    except Exception as exc:
        logger.debug("Failed to present modal parent window: %s", exc)


def show_ssh_password_dialog(
    *,
    from_widget=None,
    parent_window: Optional[Gtk.Window] = None,
    display_name: str = "",
    host: Optional[str] = None,
    username: Optional[str] = None,
    connection: Any = None,
    connection_manager: Optional[Any] = None,
    heading: Optional[str] = None,
    body: Optional[str] = None,
    store_label: Optional[str] = None,
    on_store: Optional[Any] = None,
    allow_store: Optional[bool] = None,
    on_dialog: Optional[Any] = None,
) -> Optional[str]:
    """Show the standard in-app SSH **password** dialog (blocking).

    This is the single supported entry point for prompting the user for an SSH
    login password from core features, plugins (advanced), and secondary windows
    (file manager, authorized-keys editor, external SFTP mount, …). Do **not**
    roll a custom password dialog — use this helper so Wayland stacking, copy,
    and keyring storage behave consistently. Internally this is an
    ``Adw.Dialog`` with a header bar (Cancel / confirm), a boxed-list
    ``PasswordEntryRow``, and an optional Store checkbox.

    The dialog is modal, parented on :class:`MainWindow` (via
    :func:`resolve_app_modal_parent`), and blocks until the user dismisses it
    (nested ``GLib.MainLoop``). **Must be called on the GTK main thread.**

    ``on_dialog`` receives a handle with a ``close()`` that takes the dialog
    down from the outside (as a cancel) and unwinds the nested loop. Pass it
    when the prompt's owner can go away while it is up — a daemon interaction
    that expires, say — otherwise the dialog outlives whatever it was asking
    on behalf of and cannot be dismissed.

    See module docstring / ``docs/architecture.md`` for call examples. Also re-exported
    from :mod:`sshpilot.window` for historical imports.
    """
    storage_host = host
    storage_user = username
    prompt_name = display_name

    if connection is not None:
        storage_user = storage_user or getattr(connection, "username", None)
        storage_host = (
            storage_host
            or getattr(connection, "hostname", None)
            or getattr(connection, "host", None)
            or getattr(connection, "nickname", None)
        )
        if not prompt_name:
            nickname = getattr(connection, "nickname", None)
            user_label = storage_user or ""
            host_label = storage_host or ""
            prompt_name = (
                str(nickname)
                if nickname
                else (f"{user_label}@{host_label}" if user_label else str(host_label))
            )

    if parent_window is not None:
        parent = parent_window
    else:
        parent = resolve_app_modal_parent(from_widget)

    present_for_modal_dialog(parent)
    return _show_password_passphrase_dialog(
        parent,
        prompt_type="password",
        display_name=prompt_name,
        host=storage_host,
        username=storage_user,
        connection=connection,
        connection_manager=connection_manager,
        heading=heading,
        body=body,
        store_label=store_label,
        on_store=on_store,
        allow_store=allow_store,
        on_dialog=on_dialog,
    )


class _DialogHandle:
    """A ``close()``-able stand-in for a dialog that blocks in a nested loop.

    :func:`_show_password_passphrase_dialog` owns its ``Adw.Dialog`` inside a
    nested ``GLib.MainLoop``, so a caller holding the widget could hide it but
    would leave that loop spinning. This closes the dialog *and* unwinds the
    loop, by running the same cancel path the Cancel button uses — so callers
    that keep dialogs in a ``{id: dialog}`` map can treat it like any other.
    """

    __slots__ = ("_dismiss",)

    def __init__(self, dismiss) -> None:
        self._dismiss = dismiss

    def close(self) -> None:
        self._dismiss()


def _show_password_passphrase_dialog(
    parent_window,
    prompt_type: str = "password",
    display_name: str = "",
    key_path: Optional[str] = None,
    host: Optional[str] = None,
    username: Optional[str] = None,
    connection: Optional[Any] = None,
    connection_manager: Optional[Any] = None,
    *,
    heading: Optional[str] = None,
    body: Optional[str] = None,
    store_label: Optional[str] = None,
    on_store: Optional[Any] = None,
    allow_store: Optional[bool] = None,
    on_dialog: Optional[Any] = None,
) -> Optional[str]:
    """Show a graphical password or passphrase dialog.

    ``Adw.Dialog`` + header bar (Cancel / confirm) with a boxed-list password
    row and an optional Store checkbox below it — not a SwitchRow (which reads
    like another text field).
    """
    from . import icon_utils

    password_result = [None]
    main_loop = GLib.MainLoop()
    done = [False]

    if heading is None:
        if prompt_type == "passphrase":
            heading = _("Passphrase Required")
        elif prompt_type == "challenge":
            heading = _("Authentication Required")
        else:
            heading = _("Password Required")
    if body is None:
        if prompt_type == "passphrase":
            if key_path:
                key_name = os.path.basename(key_path)
                body = _("Enter the passphrase for key “{key_name}”.").format(
                    key_name=key_name
                )
            else:
                body = _("Enter your passphrase.")
        elif prompt_type == "challenge":
            body = display_name or _("Enter the verification code.")
        elif display_name:
            body = _("Enter your password for {display_name}.").format(
                display_name=display_name
            )
        else:
            body = _("Enter your password.")

    if prompt_type == "passphrase":
        entry_title = _("Passphrase")
        default_store_label = _("Store passphrase")
        confirm_label = _("Unlock")
    elif prompt_type == "challenge":
        entry_title = _("Verification code")
        default_store_label = ""
        confirm_label = _("Continue")
    else:
        entry_title = _("Password")
        default_store_label = _("Store password")
        confirm_label = _("OK")
    if not store_label:
        store_label = default_store_label
    if allow_store is None:
        allow_store = prompt_type in ("password", "passphrase")
    # Persistence is an explicit daemon/plugin callback. A GTK dialog never
    # writes a secret through a local manager or secret helper.
    allow_store = bool(allow_store and on_store is not None)

    dialog = Adw.Dialog()
    dialog.set_title(heading)
    # follows_content_size=True would ignore content_width and shrink to the
    # PreferencesGroup's natural size — keep an explicit width instead.
    dialog.set_content_width(480)
    dialog.set_follows_content_size(False)

    cancel_btn = Gtk.Button(label=_("Cancel"))
    ok_btn = Gtk.Button(label=confirm_label)
    ok_btn.add_css_class("suggested-action")

    header = Adw.HeaderBar()
    header.set_show_start_title_buttons(False)
    header.set_show_end_title_buttons(False)
    header.pack_start(cancel_btn)
    header.pack_end(ok_btn)

    icon = icon_utils.new_image_from_icon_name("dialog-password-symbolic")
    icon.set_pixel_size(48)
    icon.set_halign(Gtk.Align.CENTER)

    body_label = Gtk.Label(label=body)
    body_label.set_wrap(True)
    body_label.set_justify(Gtk.Justification.CENTER)
    body_label.set_halign(Gtk.Align.CENTER)
    body_label.add_css_class("dim-label")

    password_row = Adw.PasswordEntryRow(title=entry_title)
    group = Adw.PreferencesGroup()
    group.add(password_row)

    store_checkbox = Gtk.CheckButton(label=store_label or _("Store password"))
    store_checkbox.set_active(False)

    persists_secrets = _secrets_persist_for(parent_window)

    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
    content.set_margin_top(18)
    content.set_margin_bottom(24)
    content.set_margin_start(24)
    content.set_margin_end(24)
    content.append(icon)
    content.append(body_label)
    content.append(group)
    if allow_store and persists_secrets:
        content.append(store_checkbox)
    elif allow_store and not persists_secrets:
        no_store_label = Gtk.Label(
            label=_(
                "Secret storage is set to SSH Agent Only — passwords and passphrases "
                "are not saved by SSH Pilot."
            ),
        )
        no_store_label.set_wrap(True)
        no_store_label.set_xalign(0)
        for css in ("dim-label", "caption"):
            try:
                no_store_label.add_css_class(css)
            except Exception:
                pass
        content.append(no_store_label)

    toolbar = Adw.ToolbarView()
    toolbar.add_top_bar(header)
    toolbar.set_content(content)
    dialog.set_child(toolbar)

    def _finish(ok: bool) -> None:
        if done[0]:
            return
        done[0] = True
        if ok:
            entered = password_row.get_text()
            if entered:
                password_result[0] = entered
                store_checked = bool(
                    allow_store and persists_secrets and store_checkbox.get_active()
                )
                if allow_store and store_checked:
                    if on_store is not None:
                        try:
                            on_store(entered)
                        except Exception as e:
                            logger.debug("Failed to store via on_store hook: %s", e)
                    elif prompt_type == "password":
                        # Secret persistence must be supplied by the daemon
                        # interaction path via ``on_store``. There is no GTK
                        # connection-manager fallback.
                        logger.debug("Password entered without a daemon store callback")
            else:
                password_result[0] = None
        else:
            password_result[0] = None
        try:
            dialog.close()
        except Exception:
            pass
        main_loop.quit()

    cancel_btn.connect("clicked", lambda _b: _finish(False))
    ok_btn.connect("clicked", lambda _b: _finish(True))
    dialog.set_default_widget(ok_btn)

    try:
        password_row.connect("entry-activated", lambda _r: _finish(True))
    except (TypeError, AttributeError):
        key_controller = Gtk.EventControllerKey()

        def on_key_pressed(_controller, keyval, _keycode, _state):
            if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
                _finish(True)
                return True
            if keyval == Gdk.KEY_Escape:
                _finish(False)
                return True
            return False

        key_controller.connect("key-pressed", on_key_pressed)
        dialog.add_controller(key_controller)

    def _on_closed(*_args):
        if not done[0]:
            _finish(False)

    try:
        dialog.connect("closed", _on_closed)
    except TypeError:
        dialog.connect("close-request", lambda *_a: (_finish(False), False)[1])

    dialog.present(parent_window)
    # grab_focus() returns True — idle_add would re-run forever and steal
    # keystrokes after the first character unless we return SOURCE_REMOVE.
    GLib.idle_add(lambda: (password_row.grab_focus(), False)[1])

    if on_dialog is not None:
        # Hand the caller a way to take this dialog down again. It blocks in a
        # nested main loop, so an owner that goes away mid-prompt (a daemon
        # interaction that expired, a backup call that returned) can only get
        # the dialog off screen from the inside — see _DialogHandle.
        try:
            on_dialog(_DialogHandle(lambda: _finish(False)))
        except Exception:
            logger.debug("on_dialog hook failed", exc_info=True)

    main_loop.run()
    return password_result[0]


class WindowConfigDialogsMixin:
    """Known-hosts editor, preferences, and config export/import dialogs."""

    def show_known_hosts_editor(self):
        """Show known hosts editor window"""
        logger.info("Show known hosts editor window")
        client = getattr(self, "client", None)
        if client is None:
            logger.error("Known hosts editor requires a daemon-backed client")
            self._simple_dialog(
                _("Known hosts unavailable"),
                _("Connect to the SSH Pilot daemon before editing known hosts."),
            )
            return
        try:
            from .known_hosts_editor import KnownHostsEditorWindow
            editor = KnownHostsEditorWindow(self, client)
            editor.present()
        except Exception as e:
            logger.error(f"Failed to open known hosts editor: {e}")

    def show_preferences(self, page_id=None):
        """Enter Settings mode (pushes onto the main NavigationView).

        ``page_id`` optionally selects a preferences page by stack id
        (e.g. ``plugins``, ``security-&-credentials``).
        """
        logger.info("Show preferences")
        nav = getattr(self, 'nav_view', None)
        if nav is None:
            logger.error('NavigationView unavailable; cannot open Settings mode')
            return

        prefs = getattr(self, '_preferences_window', None)
        if prefs is None:
            try:
                # Imported lazily so the heavy Preferences module stays off the
                # startup path — only needed when Settings is opened.
                from .preferences import PreferencesWindow
                controller = self._build_ssh_overrides_controller()
                prefs = PreferencesWindow(
                    self, self.config, ssh_overrides_controller=controller
                )
                self._preferences_window = prefs
            except Exception as e:
                logger.error(f"Failed to create preferences page: {e}")
                return

        try:
            visible = nav.get_visible_page()
            if visible is not prefs:
                # Re-push after a previous pop (page stays alive for reuse).
                nav.push(prefs)
            if page_id:
                prefs.select_page(page_id)
        except Exception as e:
            logger.error(f"Failed to show preferences: {e}")

    def _simple_dialog(self, heading, body):
        d = Adw.MessageDialog(transient_for=self, modal=True, heading=heading, body=body)
        d.add_response('ok', _('OK'))
        d.present()

    def show_export_dialog(self):
        """Show the backup export flow: pick connections + encryption, then save a .spbk."""
        logger.info("Show export backup dialog")
        try:
            self._show_export_options_dialog()
        except Exception as e:
            logger.error(f"Failed to show export dialog: {e}")

    # Destination cards, in the order the dialog lays them out.
    _BACKUP_DESTINATIONS = ('file', 'bitwarden', 'ssh')

    def _reopen_export_options(self, **kwargs):
        """Bring the Export Backup options window back after a follow-up cancel.

        Start Backup closes this window before the file picker / Bitwarden setup /
        vault unlock / passphrase prompt. Cancelling any of those must restore
        the options (and the user's choices) rather than dropping the flow.
        """
        GLib.idle_add(lambda: (self._show_export_options_dialog(**kwargs), False)[1])

    @staticmethod
    def _export_was_user_cancelled(result) -> bool:
        """True when the daemon stopped because the user dismissed a prompt."""
        status = getattr(result, "status", None)
        if getattr(status, "value", status) == "interaction_required":
            message = getattr(result, "message", None)
            code = getattr(message, "code", None)
            code_value = getattr(code, "value", code)
            # Timeout is not a cancel — leave that as a failure dialog.
            return code_value != "encryption_request_timed_out"
        return False

    def _show_export_options_dialog(self, prefill_ids=None, encrypt_default=True,
                                    option_defaults=None, error=None,
                                    destination='file', target_nick=None, remote_dir=None,
                                    mirror_logins_default=False):
        """Choose where the backup goes and how it is protected.

        A backup always carries the whole configuration — app settings, groups,
        the SSH config, known hosts and every saved secret — so the only
        choices here are the ones that change what actually leaves the machine:
        the destination, the passphrase, and whether the private key files
        themselves are copied out of ``~/.ssh``.

        ``prefill_ids`` is still accepted because the reopen paths pass it, but
        the export is no longer scoped to a subset of connections.
        """
        try:
            connections = list(self.connection_manager.get_connections()) \
                if self.connection_manager else []
        except Exception:
            connections = []
        from sshpilot import icon_utils
        option_defaults = _normalize_backup_options(option_defaults)

        # Modal window + Clamp (same scaffold as session manager / key chooser).
        # Height follows the page so the default File destination is not padded
        # with empty space under the footnote; taller destinations scroll.
        dialog = Adw.Window(transient_for=self, modal=True)
        dialog.set_title(_("Backup"))
        dialog.set_default_size(BACKUP_EXPORT_WINDOW_WIDTH, -1)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title=_("Backup")))

        cancel_btn = Gtk.Button(label=_("Cancel"))
        cancel_btn.connect('clicked', lambda _b: dialog.close())
        header.pack_start(cancel_btn)

        start_btn = Gtk.Button(label=_("Start Backup"))
        start_btn.add_css_class('suggested-action')
        header.pack_end(start_btn)
        toolbar.add_top_bar(header)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_propagate_natural_height(True)
        scroller.set_max_content_height(BACKUP_EXPORT_CONTENT_MAX_HEIGHT)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(BACKUP_EXPORT_CLAMP_MAX)
        clamp.set_margin_top(18)
        clamp.set_margin_bottom(24)
        clamp.set_margin_start(12)
        clamp.set_margin_end(12)

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        clamp.set_child(page)
        scroller.set_child(clamp)
        toolbar.set_content(scroller)
        dialog.set_content(toolbar)

        # Validation stays in-dialog (a separate alert would hide behind this window on reopen).
        if error:
            err_group = Adw.PreferencesGroup()
            err_row = Adw.ActionRow(title=error)
            try:
                err_row.add_css_class('error')
            except Exception:
                pass
            err_icon = icon_utils.new_image_from_icon_name('dialog-error-symbolic')
            err_row.add_prefix(err_icon)
            err_group.add(err_row)
            page.append(err_group)

        intro = Gtk.Label(
            label=_("A backup lets you move everything to a new system, or restore it after "
                    "a reinstall. It includes your connections, groups, app settings, known "
                    "hosts and saved passwords."))
        intro.set_wrap(True)
        intro.set_xalign(0)
        intro.add_css_class('dim-label')
        page.append(intro)

        def _section(title):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            label = Gtk.Label(label=title)
            label.set_xalign(0)
            label.set_margin_start(2)
            label.add_css_class('heading')
            box.append(label)
            page.append(box)
            return box

        def _wrap_subtitle(row):
            # Adw >= 1.3; older runtimes ellipsize instead, which is only cosmetic.
            try:
                row.set_subtitle_lines(0)
            except Exception:
                pass

        # -- destination -------------------------------------------------
        dest_section = _section(_("Where should it go?"))
        cards_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        dest_section.append(cards_box)

        selected_dest = [destination if destination in self._BACKUP_DESTINATIONS else 'file']
        cards = {}

        def _add_destination_card(key, icon_name, title, subtitle):
            """One boxed-list row that reads as a card; the accent ring marks the pick."""
            row = Adw.ActionRow(title=title, subtitle=subtitle)
            row.set_activatable(True)
            try:
                row.set_title_lines(0)
            except Exception:
                pass
            _wrap_subtitle(row)
            icon = icon_utils.new_image_from_icon_name(icon_name, 24)
            icon.set_valign(Gtk.Align.CENTER)
            row.add_prefix(icon)
            check = icon_utils.new_image_from_icon_name('object-select-symbolic')
            check.set_valign(Gtk.Align.CENTER)
            check.add_css_class('accent')
            row.add_suffix(check)

            listbox = Gtk.ListBox()
            listbox.set_selection_mode(Gtk.SelectionMode.NONE)
            listbox.add_css_class('boxed-list')
            listbox.add_css_class('backup-destination')
            listbox.append(row)
            cards_box.append(listbox)
            row.connect('activated', lambda _r, k=key: _select_destination(k))
            cards[key] = (listbox, check, icon)

        _add_destination_card(
            'file', 'archive-symbolic',
            _("Backup to file"),
            _("One .spbk file you choose the folder for. Good for a USB stick or your own "
              "cloud drive."))
        _add_destination_card(
            'bitwarden', 'bitwarden',
            _("Bitwarden vault"),
            _("Saved as a secure note in your vault. Your Bitwarden account already encrypts "
              "it, so there is no extra passphrase to remember."))
        _add_destination_card(
            'ssh', 'network-server-symbolic',
            _("One of your servers"),
            _("Uploaded over SFTP to a host from your inventory. Always protected with a "
              "passphrase."))

        # -- SSH target (only shown for the server destination) ----------
        from .host_picker import show_host_picker
        selected_ssh_target = [None]

        ssh_list = Gtk.ListBox()
        ssh_list.set_selection_mode(Gtk.SelectionMode.NONE)
        ssh_list.add_css_class('boxed-list')

        def _ssh_target_subtitle(conn):
            if conn is None:
                return _("Choose a server…")
            host = getattr(conn, 'hostname', '') or getattr(conn, 'host', '')
            user = getattr(conn, 'username', '')
            if user and host:
                return f"{user}@{host}"
            return host or _("Selected server")

        server_row = Adw.ActionRow(title=_("Server"))
        server_row.set_activatable(True)
        chosen_label = Gtk.Label()
        chosen_label.add_css_class('dim-label')
        chosen_label.set_valign(Gtk.Align.CENTER)
        server_row.add_suffix(chosen_label)
        pick_btn = Gtk.Button()
        pick_btn.set_icon_name('pan-down-symbolic')
        pick_btn.set_tooltip_text(_("Pick from inventory"))
        pick_btn.add_css_class('flat')
        pick_btn.set_valign(Gtk.Align.CENTER)
        server_row.add_suffix(pick_btn)
        ssh_list.append(server_row)

        ssh_dir_row = Adw.EntryRow(title=_("Folder"))
        ssh_dir_row.set_text(remote_dir or "~/sshpilot-backups/")
        ssh_list.append(ssh_dir_row)
        page.append(ssh_list)

        def _set_ssh_target(conn):
            selected_ssh_target[0] = conn
            if conn is None:
                chosen_label.set_label('')
                chosen_label.set_visible(False)
                server_row.set_subtitle(_("Choose a server…"))
            else:
                nick = (getattr(conn, 'nickname', '')
                        or (conn.get_effective_host() if hasattr(conn, 'get_effective_host') else '')
                        or '?')
                chosen_label.set_label(nick)
                chosen_label.set_visible(True)
                server_row.set_subtitle(_ssh_target_subtitle(conn))

        def _open_ssh_host_picker(_widget=None):
            show_host_picker(
                self, pick_btn, _set_ssh_target,
                toast=lambda msg: self._simple_dialog(_("No servers"), msg),
                connections=connections,
            )

        pick_btn.connect('clicked', _open_ssh_host_picker)
        server_row.connect('activated', lambda _r: _open_ssh_host_picker())

        # Prefill: explicit nickname from a prior reopen, else the first inventory host.
        prefill_conn = None
        if target_nick:
            for c in connections:
                if getattr(c, 'nickname', None) == target_nick:
                    prefill_conn = c
                    break
        if prefill_conn is None and connections:
            prefill_conn = connections[0]
        _set_ssh_target(prefill_conn)

        # -- security ----------------------------------------------------
        security_section = _section(_("Security"))
        security_list = Gtk.ListBox()
        security_list.set_selection_mode(Gtk.SelectionMode.NONE)
        security_list.add_css_class('boxed-list')
        security_section.append(security_list)

        enc_row = Adw.SwitchRow(title=_("Protect the backup with a passphrase"))
        enc_row.set_active(bool(encrypt_default))
        enc_row.add_prefix(icon_utils.new_image_from_icon_name('channel-secure-symbolic'))
        _wrap_subtitle(enc_row)
        security_list.append(enc_row)

        bw_info_row = Adw.ActionRow(
            title=_("Bitwarden encrypts the backup with your vault credentials, so no "
                    "separate passphrase is needed."))
        try:
            bw_info_row.set_title_lines(0)
        except Exception:
            pass
        bw_info_row.add_css_class('dim-label')
        bw_info_row.add_prefix(
            icon_utils.new_image_from_icon_name('dialog-information-symbolic'))
        security_list.append(bw_info_row)

        mirror_logins_row = Adw.SwitchRow(
            title=_("Also copy saved secrets as Bitwarden login items"),
            subtitle=_("Creates normal login entries in your vault in addition to the "
                       "backup note."),
        )
        mirror_logins_row.set_active(bool(mirror_logins_default))
        _wrap_subtitle(mirror_logins_row)
        security_list.append(mirror_logins_row)

        keys_row = Adw.SwitchRow(
            title=_("Include your private key files"),
            subtitle=_("Copies the keys themselves out of ~/.ssh. Leave this off unless the "
                       "new machine needs them."),
        )
        keys_row.set_active(bool(option_defaults.get('private_keys', False)))
        keys_icon = icon_utils.new_image_from_icon_name('dialog-password-symbolic')
        keys_row.add_prefix(keys_icon)
        _wrap_subtitle(keys_row)
        security_list.append(keys_row)

        # Plain-text risk is called out where the switches are, not in a follow-up alert.
        warn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        warn_box.add_css_class('backup-warning')
        warn_icon = icon_utils.new_image_from_icon_name('dialog-warning-symbolic')
        warn_icon.set_valign(Gtk.Align.START)
        warn_icon.add_css_class('error')
        warn_box.append(warn_icon)
        warn_label = Gtk.Label(
            label=_("Without a passphrase your keys and saved passwords are written in plain "
                    "text — anyone who opens the file can read them."))
        warn_label.set_wrap(True)
        warn_label.set_xalign(0)
        warn_label.set_hexpand(True)
        warn_label.add_css_class('error')
        warn_box.append(warn_label)
        security_section.append(warn_box)

        footnote = Gtk.Label()
        footnote.set_wrap(True)
        footnote.set_xalign(0)
        footnote.set_margin_start(2)
        footnote.add_css_class('dim-label')
        footnote.add_css_class('caption')
        page.append(footnote)

        # -- state sync --------------------------------------------------
        syncing = [False]

        def _sync(*_a):
            if syncing[0]:
                return
            syncing[0] = True
            try:
                dest = selected_dest[0]
                for key, (listbox, check, _icon) in cards.items():
                    on = key == dest
                    check.set_visible(on)
                    if on:
                        listbox.add_css_class('selected')
                    else:
                        listbox.remove_css_class('selected')

                ssh_list.set_visible(dest == 'ssh')
                bw_info_row.set_visible(dest == 'bitwarden')
                mirror_logins_row.set_visible(dest == 'bitwarden')
                enc_row.set_visible(dest != 'bitwarden')
                # A server upload is always encrypted; the vault does its own encryption.
                if dest == 'ssh':
                    enc_row.set_active(True)
                enc_row.set_sensitive(dest == 'file')

                encrypt_on = dest != 'bitwarden' and enc_row.get_active()
                if dest == 'ssh':
                    enc_row.set_subtitle(
                        _("Required for a server upload. You will be asked for the "
                          "passphrase when the export starts."))
                elif encrypt_on:
                    enc_row.set_subtitle(
                        _("You will be asked for a passphrase when the export starts. Keep "
                          "it somewhere safe — it cannot be recovered."))
                else:
                    enc_row.set_subtitle(
                        _("Off: saved passwords are written in plain text and readable by "
                          "anyone with the file."))

                keys_on = keys_row.get_active()
                if keys_on:
                    keys_icon.add_css_class('error')
                else:
                    keys_icon.remove_css_class('error')
                warn_box.set_visible(keys_on and not encrypt_on)

                if dest == 'bitwarden':
                    footnote.set_label(
                        _("The backup is stored as a secure note named after today's date. "
                          "Importing it later asks for your vault, not a passphrase."))
                elif dest == 'ssh':
                    footnote.set_label(
                        _("The file is uploaded over your existing SSH connection. Nothing "
                          "is left behind on this computer."))
                else:
                    footnote.set_label(_("You will pick the folder and file name next."))
            finally:
                syncing[0] = False

        def _select_destination(key):
            selected_dest[0] = key
            _sync()

        enc_row.connect('notify::active', _sync)
        keys_row.connect('notify::active', _sync)
        _sync()

        def reopen(**kwargs):
            dialog.close()
            self._reopen_export_options(**kwargs)

        def on_start(_btn):
            dest = selected_dest[0]
            # Everything but the private keys always travels with the backup.
            options = {
                'app_settings': True,
                'ssh_config': True,
                'known_hosts': True,
                'secrets': True,
                'private_keys': keys_row.get_active(),
            }
            encrypt_on = dest != 'bitwarden' and enc_row.get_active()
            remote_text = ssh_dir_row.get_text()
            ssh_nick = (
                getattr(selected_ssh_target[0], 'nickname', None)
                if selected_ssh_target[0] is not None else None
            )
            # Preserve every choice so a cancel in a follow-up dialog can restore
            # this screen instead of abandoning the export flow.
            reopen_state = {
                'encrypt_default': encrypt_on if dest != 'bitwarden' else True,
                'option_defaults': options,
                'destination': dest,
                'target_nick': ssh_nick,
                'remote_dir': remote_text,
                'mirror_logins_default': mirror_logins_row.get_active(),
            }

            if dest == 'bitwarden':
                dialog.close()
                self._export_to_bitwarden(
                    connections, options,
                    mirror_logins=mirror_logins_row.get_active(),
                    reopen=reopen_state)
                return

            if dest == 'ssh':
                target = selected_ssh_target[0]
                if target is None:
                    reopen(error=_("Choose a server to back up to."), **reopen_state)
                    return
                dialog.close()
                self._export_to_ssh_server(
                    connections, options, True, target,
                    remote_text.strip() or "~/sshpilot-backups",
                    reopen=reopen_state)
                return

            dialog.close()
            if encrypt_on:
                self._choose_export_path(
                    connections, True, options, reopen=reopen_state)
            elif options['private_keys']:
                # Copying the keys themselves out of ~/.ssh unprotected still gets a
                # confirmation; the plain-text cost of the rest is stated on the switch.
                self._confirm_plaintext_then_export(
                    connections, None, options, reopen=reopen_state)
            else:
                self._choose_export_path(
                    connections, None, options, reopen=reopen_state)

        start_btn.connect('clicked', on_start)
        dialog.present()

    # Labels for the "what is in this backup" list, in reading order.
    _BACKUP_CATEGORY_LABELS = (
        ('app_settings', "App settings and groups"),
        ('ssh_config', "Connections and SSH config"),
        ('known_hosts', "Known hosts"),
        ('secrets', "Saved passwords and passphrases"),
        ('private_keys', "Private key files"),
    )

    def _backup_progress_dialog(self, *, title, status, destination_label,
                                path=None, options=None):
        """The export progress sheet; returns ``(set_status, close)``.

        Same contract as :func:`bitwarden_setup.progress_dialog`, and an
        ``Adw.Dialog`` for the same reason: the daemon prompts this waits on
        (the backup passphrase, a vault unlock) are dialogs themselves, and
        libadwaita only stacks them above this one when both are hosted by the
        same window.

        Dismissing it does **not** cancel anything.  The daemon owns the export
        and exposes no way to stop it once started, so a "cancel" here only
        ever hid the outcome of work that kept running.  Closing the sheet now
        means "run it in the background" and the result is still reported.
        """
        if not hasattr(Adw, "Dialog"):
            from .bitwarden_setup import progress_dialog
            return progress_dialog(self, title, status)

        from sshpilot import icon_utils

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        body.set_margin_top(20)
        body.set_margin_bottom(22)
        body.set_margin_start(22)
        body.set_margin_end(22)

        where = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        dest_label = Gtk.Label(label=destination_label)
        dest_label.set_wrap(True)
        dest_label.set_xalign(0)
        where.append(dest_label)
        if path:
            path_label = Gtk.Label(label=path)
            path_label.set_wrap(True)
            path_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            path_label.set_xalign(0)
            path_label.set_selectable(True)
            path_label.add_css_class('backup-path')
            path_label.add_css_class('caption')
            path_label.add_css_class('dim-label')
            where.append(path_label)
        body.append(where)

        # The daemon runs the export as one atomic call and reports no
        # intermediate phases, so this is a single live status line over a
        # static list of what the backup carries — not a fake step counter.
        running = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        spinner = Gtk.Spinner()
        spinner.set_size_request(18, 18)
        spinner.set_valign(Gtk.Align.CENTER)
        spinner.start()
        running.append(spinner)
        status_label = Gtk.Label(label=status)
        status_label.set_wrap(True)
        status_label.set_xalign(0)
        status_label.set_hexpand(True)
        running.append(status_label)
        body.append(running)

        opts = _normalize_backup_options(options)
        included = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for key, label in self._BACKUP_CATEGORY_LABELS:
            if not opts.get(key):
                continue
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            row.set_margin_top(4)
            row.set_margin_bottom(4)
            tick = icon_utils.new_image_from_icon_name('object-select-symbolic')
            tick.set_valign(Gtk.Align.CENTER)
            tick.add_css_class('dim-label')
            row.append(tick)
            text = Gtk.Label(label=_(label))
            text.set_wrap(True)
            text.set_xalign(0)
            text.set_hexpand(True)
            text.add_css_class('dim-label')
            row.append(text)
            included.append(row)
        body.append(included)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        toolbar.set_content(body)

        dialog = Adw.Dialog()
        dialog.set_title(title)
        dialog.set_content_width(480)
        dialog.set_child(toolbar)

        background_btn = Gtk.Button(label=_("Run in Background"))
        background_btn.connect('clicked', lambda _b: dialog.close())
        body.append(background_btn)

        note = Gtk.Label(
            label=_("Closing this window does not cancel the export — it keeps running and "
                    "you will be told when it finishes. Passphrase prompts open on top of it."))
        note.set_wrap(True)
        note.set_xalign(0)
        note.add_css_class('dim-label')
        note.add_css_class('caption')
        body.append(note)

        state = {"closed": False}

        def _on_closed(*_args):
            state["closed"] = True
            spinner.stop()

        dialog.connect("closed", _on_closed)
        dialog.present(self)

        def close():
            if state["closed"]:
                return
            state["closed"] = True
            spinner.stop()
            try:
                dialog.force_close()
            except Exception:
                logger.debug("Could not close the export progress dialog", exc_info=True)

        return status_label.set_text, close

    def _show_export_result(self, *, path, options, encrypt, counts,
                            warnings=(), connection_count=0):
        """The "Backup Saved" sheet: where it went and what went with it.

        ``encrypt`` is ``'vault'`` for Bitwarden (the vault does its own
        encryption), otherwise truthy/falsy for the passphrase.
        """
        from sshpilot import icon_utils

        counts = counts or {}
        opts = _normalize_backup_options(options)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        body.set_margin_top(20)
        body.set_margin_bottom(22)
        body.set_margin_start(22)
        body.set_margin_end(22)

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        check = icon_utils.new_image_from_icon_name('check-round-outline2-symbolic', 26)
        check.set_valign(Gtk.Align.START)
        check.add_css_class('success')
        head.append(check)
        head_text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        head_text.set_hexpand(True)
        title_label = Gtk.Label(label=_("Your backup is saved."))
        title_label.set_wrap(True)
        title_label.set_xalign(0)
        title_label.add_css_class('title-4')
        head_text.append(title_label)
        if path:
            path_label = Gtk.Label(label=path)
            path_label.set_wrap(True)
            path_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            path_label.set_xalign(0)
            path_label.set_selectable(True)
            path_label.add_css_class('backup-path')
            path_label.add_css_class('caption')
            path_label.add_css_class('dim-label')
            head_text.append(path_label)
        head.append(head_text)
        body.append(head)

        summary = Gtk.ListBox()
        summary.set_selection_mode(Gtk.SelectionMode.NONE)
        summary.add_css_class('boxed-list')

        def _summary_row(title, value, css=None):
            row = Adw.ActionRow(title=title)
            label = Gtk.Label(label=value)
            label.set_valign(Gtk.Align.CENTER)
            label.add_css_class(css or 'dim-label')
            row.add_suffix(label)
            summary.append(row)

        _summary_row(_("Connections"), str(connection_count))
        _summary_row(_("Saved passwords"), str(counts.get('credentials', 0)))
        _summary_row(
            _("Private keys"),
            str(counts.get('private_keys', 0)) if opts.get('private_keys')
            else _("Not included"))
        if counts.get('mirrored'):
            _summary_row(_("Bitwarden login items"), str(counts.get('mirrored')))
        if encrypt == 'vault':
            _summary_row(_("Protection"), _("Vault encryption"), css='success')
        else:
            _summary_row(_("Passphrase protection"),
                         _("On") if encrypt else _("Off"),
                         css='success' if encrypt else 'warning')
        body.append(summary)

        if warnings:
            warn_label = Gtk.Label(
                label="\n\n".join(format_secret_transfer_messages(warnings)))
            warn_label.set_wrap(True)
            warn_label.set_xalign(0)
            warn_label.add_css_class('warning')
            body.append(warn_label)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        buttons.set_halign(Gtk.Align.END)
        if path:
            copy_btn = Gtk.Button(label=_("Copy Path"))

            def on_copy(btn):
                try:
                    self.get_clipboard().set(path)
                    btn.set_label(_("Copied"))
                except Exception:
                    logger.debug("Could not copy the backup path", exc_info=True)

            copy_btn.connect('clicked', on_copy)
            buttons.append(copy_btn)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        toolbar.set_content(body)

        if hasattr(Adw, "Dialog"):
            dialog = Adw.Dialog()
            dialog.set_title(_("Backup Saved"))
            dialog.set_content_width(480)
            dialog.set_child(toolbar)
            done_btn = Gtk.Button(label=_("Done"))
            done_btn.add_css_class('suggested-action')
            done_btn.connect('clicked', lambda _b: dialog.force_close())
            buttons.append(done_btn)
            body.append(buttons)
            dialog.present(self)
            return

        window = Adw.Window(transient_for=self, modal=True)
        window.set_title(_("Backup Saved"))
        window.set_default_size(BACKUP_DIALOG_MIN_WIDTH, -1)
        window.set_content(toolbar)
        done_btn = Gtk.Button(label=_("Done"))
        done_btn.add_css_class('suggested-action')
        done_btn.connect('clicked', lambda _b: window.close())
        buttons.append(done_btn)
        body.append(buttons)
        window.present()

    def _export_to_bitwarden(self, connections, options, mirror_logins=False,
                             reopen=None):
        """Get Bitwarden ready, then store the backup manifest in a secure note (optionally also
        mirroring saved secrets as Bitwarden login items). The manifest is built and written
        entirely inside the daemon — the frontend only shows progress and the outcome counts."""
        from .bitwarden_backup_setup import ensure_bitwarden_ready

        def after_ready(ready):
            if not ready:
                if reopen is not None:
                    self._reopen_export_options(**reopen)
                return

            # Show progress *before* the vault-unlock gate. Bitwarden (and the
            # current secrets backend check) can sit idle for a long time; without
            # this sheet the options window is already gone and the main window
            # looks like nothing is happening. Unlock prompts stack above it.
            _set_status, close_spinner = self._backup_progress_dialog(
                title=_("Exporting Backup"),
                status=_("Preparing Bitwarden backup…"),
                destination_label=_("Writing to your Bitwarden vault"),
                options=options)

            def do_export(*_a):
                controller = self._secrets_controller()
                _set_status(_("Exporting to Bitwarden — this may take a while…"))

                def worker():
                    try:
                        result = controller.export_backup(
                            destination="bitwarden",
                            connection_ids=self._connection_ids_for(connections),
                            options=options, mirror_logins=mirror_logins)
                        payload = ('ok', result)
                    except Exception as e:
                        logger.error("Bitwarden export failed: %s", e)
                        payload = ('error', str(e))
                    GLib.idle_add(lambda: (_report(payload), False)[1])

                def _report(p):
                    close_spinner()
                    if p[0] != 'ok':
                        self._simple_dialog(_("Export Failed"), p[1])
                        return
                    result = p[1]
                    if self._export_was_user_cancelled(result):
                        if reopen is not None:
                            self._reopen_export_options(**reopen)
                        return
                    if result.status.value != 'success':
                        # A refused export is reported like any other failure:
                        # only raised exceptions used to reach the log, so a
                        # failure the daemon returned as a result left none.
                        logger.error(
                            "Bitwarden export failed: %s", result.message or "no detail"
                        )
                        msg = (
                            format_secret_transfer_message(result.message)
                            if result.message is not None
                            else _("Bitwarden export failed.")
                        )
                        if result.warnings:
                            msg += "\n\n" + "\n\n".join(
                                format_secret_transfer_messages(result.warnings)
                            )
                        self._simple_dialog(_("Export Failed"), msg)
                        return
                    self._show_export_result(
                        path=result.path or _("Secure note in your Bitwarden vault"),
                        options=options, encrypt='vault',
                        counts=result.counts or {}, warnings=result.warnings,
                        connection_count=len(connections or []))

                threading.Thread(target=worker, daemon=True).start()

            def _on_vault_cancelled():
                close_spinner()
                if reopen is not None:
                    self._reopen_export_options(**reopen)

            # Reading saved secrets for the manifest may need the CURRENT secrets backend unlocked.
            self._run_after_vault_unlock_for_secrets(
                do_export, needed=bool(options.get('secrets')),
                cancelled_heading=_("Export Cancelled"),
                on_cancelled=_on_vault_cancelled)

        ensure_bitwarden_ready(self, after_ready)

    def _secrets_controller(self):
        """The daemon-backed secrets controller the window is bound to.

        Backup export/import is daemon-owned: when no controller is reachable there is
        nothing this process may do locally, so callers surface the error."""
        controller = getattr(self, "secrets_controller", None)
        if controller is None:
            raise RuntimeError(
                _("Secret storage is managed by the SSH Pilot daemon, which is not connected."))
        return controller

    def _present_ssh_backup_prompts(self, *targets):
        """Present the daemon's SSH prompts for a backup against *targets*.

        A backup to an SSH server connects inside the daemon, so its login
        password, key passphrase and host-key prompts arrive with no scope id
        the frontend could bind to — only the connection they belong to. This
        opens a presenter for that connection; close it (``close()``) as soon
        as the backup call returns. Returns ``None`` when no daemon client is
        available, which callers treat as "no prompts to present".

        Must be called on the GTK thread, and so must the ``close()``.
        """
        client = getattr(self, "client", None)
        bridge = getattr(self, "client_bridge", None)
        if client is None or bridge is None:
            return None
        ids = set()
        for target in targets:
            if isinstance(target, str):
                ids.add(target)
                continue
            for attribute in ("id", "uuid", "nickname", "hostname"):
                value = getattr(target, attribute, None)
                if value:
                    ids.add(str(value))
        if not ids:
            return None
        try:
            from .gtk.backup_interaction_presenter import (
                BackupServerInteractionPresenter,
            )

            return BackupServerInteractionPresenter(
                client, bridge, self, connection_ids=ids
            )
        except Exception:
            logger.debug("Backup interaction presenter unavailable", exc_info=True)
            return None

    @staticmethod
    def _close_ssh_backup_prompts(presenter):
        """Close a presenter from :meth:`_present_ssh_backup_prompts`, if any."""
        if presenter is None:
            return
        try:
            presenter.close()
        except Exception:
            logger.debug("Closing the backup interaction presenter failed", exc_info=True)

    @staticmethod
    def _connection_ids_for(connections):
        """Map connection records to the id/nickname keys the daemon resolves."""
        out = []
        for conn in connections or []:
            key = (getattr(conn, 'nickname', '') or getattr(conn, 'id', '') or '')
            if key:
                out.append(str(key))
        return out

    def _export_to_ssh_server(self, connections, options, encrypt, target, remote_dir,
                              reopen=None):
        """Store the backup manifest as a ``.spbk`` file in ``remote_dir`` on ``target``.

        The manifest is built, encrypted (when ``encrypt``) and uploaded entirely inside the
        daemon; the daemon also resolves the connection password from its own secret manager.
        The passphrase is collected by a protected interaction, never by this window."""
        controller = self._secrets_controller()

        def do_export(*_a):
            who = getattr(target, 'nickname', '') or getattr(target, 'hostname', '') or '?'
            dest = "ssh:{}:{}".format(
                getattr(target, 'nickname', '') or getattr(target, 'hostname', '') or '',
                remote_dir)
            _set_status, close_spinner = self._backup_progress_dialog(
                title=_("Exporting Backup"),
                status=_("Backing up to {host} — this may take a while…").format(host=who),
                destination_label=_("Writing to {host}").format(host=who),
                path=remote_dir, options=options)

            prompts = self._present_ssh_backup_prompts(target)

            def worker():
                try:
                    result = controller.export_backup(
                        destination=dest,
                        connection_ids=self._connection_ids_for(connections),
                        options={**options, 'encrypted': bool(encrypt)})
                    payload = ('ok', result)
                except Exception as e:
                    logger.error("SSH server export failed: %s", e)
                    payload = ('error', str(e))
                GLib.idle_add(lambda: (_report(payload), False)[1])

            def _report(p):
                self._close_ssh_backup_prompts(prompts)
                close_spinner()
                if p[0] != 'ok':
                    self._simple_dialog(_("Export Failed"), p[1])
                    return
                result = p[1]
                if self._export_was_user_cancelled(result):
                    if reopen is not None:
                        self._reopen_export_options(**reopen)
                    return
                if result.status.value != 'success':
                    logger.error(
                        "SSH server export failed: %s", result.message or "no detail"
                    )
                    msg = (
                        format_secret_transfer_message(result.message)
                        if result.message is not None
                        else _("Export failed.")
                    )
                    self._simple_dialog(_("Export Failed"), msg)
                    return
                self._show_export_result(
                    path=result.path or "{}:{}".format(who, remote_dir),
                    options=options, encrypt=bool(encrypt),
                    counts=result.counts or {}, warnings=result.warnings,
                    connection_count=len(connections or []))

            threading.Thread(target=worker, daemon=True).start()

        self._run_after_vault_unlock_for_secrets(
            do_export, needed=bool(options.get('secrets')),
            cancelled_heading=_("Export Cancelled"),
            on_cancelled=(
                (lambda: self._reopen_export_options(**reopen))
                if reopen is not None else None
            ))

    def _confirm_plaintext_then_export(self, connections, sel_ids, options,
                                       on_confirm=None, reopen=None):
        sensitive_items = []
        if options.get('secrets'):
            sensitive_items.append(_("saved passwords and passphrases"))
        if options.get('private_keys'):
            sensitive_items.append(_("private keys"))
        sensitive_text = _(" and ").join(sensitive_items) if sensitive_items else _("backup data")
        warn = Adw.MessageDialog(
            transient_for=self, modal=True, heading=_("Export without encryption?"),
            body=_("{items} will be written in PLAIN TEXT and readable by anyone with the "
                   "file. Continue?").format(items=sensitive_text))
        warn.add_response('back', _('Go Back'))
        warn.add_response('plain', _('Export Unencrypted'))
        warn.set_response_appearance('plain', Adw.ResponseAppearance.DESTRUCTIVE)
        warn.set_close_response('back')

        def on_warn(dlg, resp):
            if resp == 'plain':
                if on_confirm is not None:
                    on_confirm()
                else:
                    self._choose_export_path(
                        connections, None, options, reopen=reopen)
            elif reopen is not None:
                self._reopen_export_options(**reopen)
            else:
                # Legacy callers without a reopen snapshot still get the options window.
                self._reopen_export_options(
                    prefill_ids=sel_ids, encrypt_default=False,
                    option_defaults=options)
        warn.connect('response', on_warn)
        warn.present()

    def _run_local_export(self, *, export_path, connections, options, encrypt,
                          reopen=None):
        """Run the daemon-owned ``.spbk`` export off the GTK main thread.

        The daemon RPC below blocks on the encryption passphrase interaction
        (when ``encrypt``): the interaction can only be claimed and answered
        once the GTK main loop is free to process it, so the RPC must not run
        on the calling (GTK) thread — see ``_export_to_ssh_server``/
        ``_run_daemon_import`` for the same pattern. Running it inline on the
        caller used to deadlock the export until the interaction expired
        (issue #1200)."""
        controller = self._secrets_controller()
        _set_status, close_spinner = self._backup_progress_dialog(
            title=_("Exporting Backup"),
            status=_("Exporting backup — this may take a while…"),
            destination_label=_("Writing to this computer"),
            path=export_path, options=options)

        def worker():
            try:
                result = controller.export_backup(
                    destination=export_path,
                    connection_ids=self._connection_ids_for(connections),
                    options={**options, 'encrypted': bool(encrypt)})
                payload = ('ok', result)
            except Exception as e:
                logger.error(f"Export failed: {e}")
                payload = ('error', str(e))
            GLib.idle_add(lambda: (_report(payload), False)[1])

        def _report(p):
            close_spinner()
            if p[0] != 'ok':
                self._simple_dialog(_("Export Failed"), p[1])
                return
            result = p[1]
            if self._export_was_user_cancelled(result):
                if reopen is not None:
                    self._reopen_export_options(**reopen)
                return
            if result.status.value != 'success':
                logger.error("Export failed: %s", result.message or "no detail")
                msg = (
                    format_secret_transfer_message(result.message)
                    if result.message is not None
                    else _("Unknown error")
                )
                self._simple_dialog(_("Export Failed"), msg)
                return
            self._show_export_result(
                path=result.path or export_path, options=options,
                encrypt=bool(encrypt), counts=result.counts or {},
                warnings=result.warnings,
                connection_count=len(connections or []))

        threading.Thread(target=worker, daemon=True).start()

    def _choose_export_path(self, connections, encrypt, options, reopen=None):
        """Pick a ``.spbk`` path (GTK file picker), then run the daemon-owned export.

        ``encrypt`` is a flag: the passphrase itself is collected by a protected
        interaction inside the daemon, so it never passes through this window."""
        file_dialog = Gtk.FileDialog()
        file_dialog.set_title(_("Export Backup"))
        file_dialog.set_initial_name(
            f"sshpilot_backup_{datetime.now().strftime('%Y%m%d')}.spbk")
        spbk_filter = Gtk.FileFilter()
        spbk_filter.set_name(_("SSH Pilot backup (*.spbk)"))
        spbk_filter.add_pattern("*.spbk")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(spbk_filter)
        file_dialog.set_filters(filters)
        file_dialog.set_default_filter(spbk_filter)
        try:
            docs_path = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOCUMENTS)
            if docs_path:
                file_dialog.set_initial_folder(Gio.File.new_for_path(docs_path))
        except Exception:
            pass

        def on_save_response(dialog, result):
            try:
                file = dialog.save_finish(result)
            except Exception as e:
                # Gtk.FileDialog reports dismiss as GLib.Error with
                # GTK_DIALOG_ERROR_DISMISSED (2).
                if getattr(e, 'code', None) == 2:
                    logger.info("Export cancelled by user")
                    if reopen is not None:
                        self._reopen_export_options(**reopen)
                    return
                if not isinstance(e, GLib.Error):
                    raise
                logger.error(f"Export failed: {e}")
                self._simple_dialog(_("Export Failed"), str(e))
                return
            if not file:
                if reopen is not None:
                    self._reopen_export_options(**reopen)
                return
            export_path = file.get_path()
            if not export_path.endswith('.spbk'):
                export_path += '.spbk'

            def do_export(*_args):
                self._run_local_export(
                    export_path=export_path, connections=connections,
                    options=options, encrypt=encrypt, reopen=reopen)

            self._run_after_vault_unlock_for_secrets(
                do_export,
                needed=bool(options.get('secrets')),
                cancelled_heading=_("Export Cancelled"),
                on_cancelled=(
                    (lambda: self._reopen_export_options(**reopen))
                    if reopen is not None else None
                ),
            )

        file_dialog.save(self, None, on_save_response)

    def show_import_dialog(self):
        """Ask where to import from (file, Bitwarden, SSH, or Ásbrú), then run that flow."""
        logger.info("Show import source dialog")
        try:
            dialog = Adw.MessageDialog(
                transient_for=self, modal=True, heading=_("Import Configuration"),
                body=_(
                    "Import a backup from a file, an SSH server, Bitwarden, "
                    "or an Ásbrú Connection Manager export."
                ))
            dialog.add_response('cancel', _('Cancel'))
            dialog.add_response('asbru', _('From Ásbrú…'))
            dialog.add_response('bitwarden', _('From Bitwarden'))
            dialog.add_response('ssh', _('From SSH Server'))
            dialog.add_response('file', _('From File'))
            dialog.set_default_response('file')
            dialog.set_close_response('cancel')

            def on_source(_d, resp):
                if resp == 'file':
                    self._import_from_file()
                elif resp == 'bitwarden':
                    self._import_from_bitwarden()
                elif resp == 'ssh':
                    self._import_from_ssh_server()
                elif resp == 'asbru':
                    self._import_from_asbru()
            dialog.connect('response', on_source)
            dialog.present()
        except Exception as e:
            logger.error(f"Failed to show import source dialog: {e}")

    def _import_from_asbru(self):
        """Import connections from an Ásbrú Connection Manager export YAML."""
        logger.info("Show Ásbrú import file chooser")
        try:
            file_dialog = Gtk.FileDialog()
            file_dialog.set_title(_("Import from Ásbrú"))

            filter_yml = Gtk.FileFilter()
            filter_yml.set_name(_("Ásbrú export (*.yml, *.yaml)"))
            filter_yml.add_pattern("*.yml")
            filter_yml.add_pattern("*.yaml")

            filter_all = Gtk.FileFilter()
            filter_all.set_name(_("All files"))
            filter_all.add_pattern("*")

            filters = Gio.ListStore.new(Gtk.FileFilter)
            filters.append(filter_yml)
            filters.append(filter_all)
            file_dialog.set_filters(filters)
            file_dialog.set_default_filter(filter_yml)

            def on_open_response(dialog, result):
                try:
                    file = dialog.open_finish(result)
                    if file:
                        self._begin_asbru_import(file.get_path())
                except GLib.Error as e:
                    if e.code == 2:
                        logger.info("Ásbrú import cancelled by user")
                    else:
                        logger.error("Ásbrú import file selection failed: %s", e)
                except Exception as e:
                    logger.error("Ásbrú import file selection failed: %s", e)

            file_dialog.open(self, None, on_open_response)
        except Exception as e:
            logger.error("Failed to show Ásbrú import dialog: %s", e)

    def _begin_asbru_import(self, import_path: str):
        """Preview then apply an Ásbrú export via the daemon API."""
        from .bitwarden_backup_setup import progress_dialog

        client = getattr(self, "client", None)
        if client is None or not hasattr(client, "preview_asbru_import"):
            self._simple_dialog(
                _("Import Failed"),
                _("Ásbrú import requires a connected daemon client."),
            )
            return

        cancelled = {"v": False}
        _set_status, close_spinner = progress_dialog(
            self,
            _("Import from Ásbrú"),
            _("Reading Ásbrú export…"),
            on_cancel=lambda: cancelled.__setitem__("v", True),
        )

        def worker():
            try:
                preview = client.preview_asbru_import(import_path)
                payload = ("preview", preview)
            except Exception as e:
                logger.error("Ásbrú import preview failed: %s", e)
                payload = ("error", str(e))
            GLib.idle_add(lambda: (_after_preview(payload), False)[1])

        def _after_preview(p):
            if cancelled["v"]:
                return
            close_spinner()
            if p[0] != "preview":
                self._simple_dialog(_("Import Failed"), p[1])
                return
            preview = p[1]
            if preview.errors:
                self._simple_dialog(
                    _("Import Failed"),
                    "\n".join(preview.errors),
                )
                return
            add_n = len(preview.connections_to_add)
            skip_n = len(preview.connections_to_skip)
            group_n = len(preview.groups_to_add)
            if add_n == 0 and group_n == 0:
                body = _(
                    "No new connections to import. "
                    "{skip} existing nickname(s) would be skipped."
                ).format(skip=skip_n)
                self._simple_dialog(_("Nothing to Import"), body)
                return
            body = _(
                "Import {add} connection(s) and {groups} group(s)?\n"
                "{skip} existing nickname(s) will be skipped."
            ).format(add=add_n, groups=group_n, skip=skip_n)
            if preview.warnings:
                body += "\n\n" + "\n".join(preview.warnings[:8])
                if len(preview.warnings) > 8:
                    body += "\n…"

            confirm = Adw.MessageDialog(
                transient_for=self,
                modal=True,
                heading=_("Import from Ásbrú"),
                body=body,
            )
            confirm.add_response("cancel", _("Cancel"))
            confirm.add_response("import", _("Import"))
            confirm.set_default_response("import")
            confirm.set_close_response("cancel")

            def on_confirm(_d, resp):
                if resp == "import":
                    self._run_asbru_import(import_path)

            confirm.connect("response", on_confirm)
            confirm.present()

        threading.Thread(target=worker, daemon=True).start()

    def _run_asbru_import(self, import_path: str):
        from .api.models.connections import AsbruImportRequest
        from .bitwarden_backup_setup import progress_dialog

        client = getattr(self, "client", None)
        cancelled = {"v": False}
        _set_status, close_spinner = progress_dialog(
            self,
            _("Import from Ásbrú"),
            _("Importing connections…"),
            on_cancel=lambda: cancelled.__setitem__("v", True),
        )

        def worker():
            try:
                result = client.import_asbru(AsbruImportRequest(source=import_path))
                payload = ("ok", result)
            except Exception as e:
                logger.error("Ásbrú import failed: %s", e)
                payload = ("error", str(e))
            GLib.idle_add(lambda: (_after(payload), False)[1])

        def _after(p):
            if cancelled["v"]:
                return
            close_spinner()
            if p[0] != "ok":
                self._simple_dialog(_("Import Failed"), p[1])
                return
            result = p[1]
            lines = [result.message or _("Import finished.")]
            if result.partial_failures:
                lines.append("")
                lines.extend(result.partial_failures[:10])
            if result.warnings:
                lines.append("")
                lines.extend(result.warnings[:6])
            heading = _("Import Complete") if result.ok else _("Import Completed with Errors")
            self._simple_dialog(heading, "\n".join(lines))

        threading.Thread(target=worker, daemon=True).start()

    def _import_from_file(self):
        """Show the file chooser for a .spbk / .json import."""
        logger.info("Show import configuration dialog")
        try:
            # Create file chooser dialog for opening
            file_dialog = Gtk.FileDialog()
            file_dialog.set_title(_("Import Configuration"))
            
            # Backups (.spbk) and legacy JSON configs.
            filter_backup = Gtk.FileFilter()
            filter_backup.set_name(_("SSH Pilot backups & configs"))
            filter_backup.add_pattern("*.spbk")
            filter_backup.add_pattern("*.json")

            filter_all = Gtk.FileFilter()
            filter_all.set_name(_("All files"))
            filter_all.add_pattern("*")

            filters = Gio.ListStore.new(Gtk.FileFilter)
            filters.append(filter_backup)
            filters.append(filter_all)
            file_dialog.set_filters(filters)
            file_dialog.set_default_filter(filter_backup)
            
            # Set default folder to user's documents or home
            try:
                docs_path = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOCUMENTS)
                if docs_path:
                    file_dialog.set_initial_folder(Gio.File.new_for_path(docs_path))
            except Exception:
                pass
            
            def on_open_response(dialog, result):
                try:
                    file = dialog.open_finish(result)
                    if file:
                        import_path = file.get_path()
                        self._begin_import(import_path)

                except GLib.Error as e:
                    # Check if user cancelled the dialog (error code 2 = GTK_DIALOG_ERROR_DISMISSED)
                    if e.code == 2:
                        logger.info("Import cancelled by user")
                    else:
                        logger.error(f"Import file selection failed: {e}")
                except Exception as e:
                    logger.error(f"Import file selection failed: {e}")
            
            file_dialog.open(self, None, on_open_response)
            
        except Exception as e:
            logger.error(f"Failed to show import dialog: {e}")

    def _import_from_bitwarden(self):
        """Ensure Bitwarden is ready, list sshPilot backups, then restore the chosen one.
        Listing and reading run inside the daemon — the frontend only sees metadata."""
        from .bitwarden_backup_setup import ensure_bitwarden_ready

        def proceed(ready):
            if not ready:
                return
            from .bitwarden_backup_setup import progress_dialog
            controller = self._secrets_controller()
            cancelled = {'v': False}
            _set_status, close_spinner = progress_dialog(
                self, _("Import from Bitwarden"), _("Loading backups from Bitwarden…"),
                on_cancel=lambda: cancelled.__setitem__('v', True))

            def worker():   # bw list items is slow — keep it off the main thread
                try:
                    payload = ('ok', controller.list_bitwarden_backups())
                except Exception as e:
                    logger.error("Listing Bitwarden backups failed: %s", e)
                    payload = ('error', str(e))
                GLib.idle_add(lambda: (_after_list(payload), False)[1])

            def _after_list(p):
                if cancelled['v']:
                    return
                close_spinner()
                if p[0] != 'ok':
                    self._simple_dialog(_("Import Failed"), p[1])
                    return
                entries = p[1]
                if not entries:
                    self._simple_dialog(
                        _("No backups found"),
                        _("No SSH Pilot backups were found in your Bitwarden vault."))
                    return
                self._show_bitwarden_entry_chooser(entries)

            threading.Thread(target=worker, daemon=True).start()

        ensure_bitwarden_ready(self, proceed)

    @staticmethod
    def _entry_label(entry):
        """Display name for a daemon-provided backup entry dict (id/name/date)."""
        name = entry.get('name', '') if isinstance(entry, dict) else getattr(entry, 'name', '')
        date = entry.get('date', '') if isinstance(entry, dict) else getattr(entry, 'date', '')
        return name + (f"  ({date[:10]})" if date else "")

    def _choose_backup_entry(self, entries, *, heading, on_chosen):
        """Radio list of backup-entry dicts (id/name/date metadata only); calls
        ``on_chosen(entry)`` on the main thread when the user hits Restore. Shared by
        the Bitwarden and SSH-server import flows."""
        dialog = Adw.MessageDialog(
            transient_for=self, modal=True, heading=heading,
            body=_("Choose a backup to restore:"))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_start(12); box.set_margin_end(12)
        box.set_margin_top(12); box.set_margin_bottom(12)
        radios = []
        group = None
        for entry in entries:
            rb = Gtk.CheckButton(label=self._entry_label(entry))
            if group is None:
                group = rb
                rb.set_active(True)
            else:
                rb.set_group(group)
            radios.append((rb, entry))
            box.append(rb)
        dialog.set_extra_child(box)
        dialog.add_response('cancel', _('Cancel'))
        dialog.add_response('open', _('Restore'))
        dialog.set_response_appearance('open', Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response('open')
        dialog.set_close_response('cancel')

        def on_resp(_d, resp):
            if resp != 'open':
                return
            entry = next((e for rb, e in radios if rb.get_active()), None)
            if entry is not None:
                on_chosen(entry)
        dialog.connect('response', on_resp)
        dialog.present()

    def _show_bitwarden_entry_chooser(self, entries):
        def on_chosen(entry):
            from .bitwarden_backup_setup import progress_dialog
            controller = self._secrets_controller()
            entry_id = entry.get('id', '') if isinstance(entry, dict) else getattr(entry, 'id', '')
            cancelled = {'v': False}
            _set_status, close_spinner = progress_dialog(
                self, _("Import from Bitwarden"), _("Reading backup from Bitwarden…"),
                on_cancel=lambda: cancelled.__setitem__('v', True))

            def worker():   # bw get item is slow — keep it off the main thread
                try:
                    payload = ('ok', controller.preview_bitwarden_backup(entry_id=entry_id))
                except Exception as e:
                    logger.error("Reading Bitwarden backup failed: %s", e)
                    payload = ('error', str(e))
                GLib.idle_add(lambda: (_after_read(payload), False)[1])

            def _after_read(p):
                if cancelled['v']:
                    return
                close_spinner()
                if p[0] != 'ok':
                    self._simple_dialog(_("Import Failed"), p[1])
                    return
                preview = p[1]
                if preview.error is not None:
                    self._simple_dialog(
                        _("Import Failed"),
                        format_secret_transfer_message(preview.error),
                    )
                    return
                self._show_import_mode_dialog(
                    preview.included,
                    self._make_bitwarden_import_apply(entry_id, preview.included),
                    source_kind='bitwarden')

            threading.Thread(target=worker, daemon=True).start()

        self._choose_backup_entry(
            entries, heading=_("Import from Bitwarden"), on_chosen=on_chosen)

    def _import_from_ssh_server(self):
        """Pick a server + remote dir, list its sshPilot backups, download one, then import it."""
        try:
            connections = list(self.connection_manager.get_connections()) \
                if self.connection_manager else []
        except Exception:
            connections = []
        if not connections:
            self._simple_dialog(
                _("No servers"), _("You have no saved servers to import from."))
            return

        from .host_picker import show_host_picker

        dialog = Adw.Dialog()
        dialog.set_title(_("Import from SSH Server"))
        dialog.set_content_width(BACKUP_DIALOG_MIN_WIDTH)
        dialog.set_follows_content_size(True)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_start_title_buttons(False)
        header.set_show_end_title_buttons(False)
        header.set_title_widget(Adw.WindowTitle(
            title=_("Import from SSH Server"),
            subtitle=_("Choose a server and the directory your backups are stored in."),
        ))
        cancel_btn = Gtk.Button(label=_("Cancel"))
        cancel_btn.connect('clicked', lambda _b: dialog.close())
        header.pack_start(cancel_btn)
        continue_btn = Gtk.Button(label=_("Continue"))
        continue_btn.add_css_class('suggested-action')
        header.pack_end(continue_btn)
        toolbar.add_top_bar(header)

        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup()
        group.add_css_class('boxed-list')

        selected_target = [None]

        server_row = Adw.ActionRow(title=_("Server"))
        server_row.set_activatable(True)
        chosen_label = Gtk.Label()
        chosen_label.add_css_class('dim-label')
        chosen_label.set_valign(Gtk.Align.CENTER)
        server_row.add_suffix(chosen_label)
        pick_btn = Gtk.Button()
        pick_btn.set_icon_name('pan-down-symbolic')
        pick_btn.set_tooltip_text(_("Pick from inventory"))
        pick_btn.add_css_class('flat')
        pick_btn.set_valign(Gtk.Align.CENTER)
        server_row.add_suffix(pick_btn)

        def _set_target(conn):
            selected_target[0] = conn
            if conn is None:
                chosen_label.set_label('')
                chosen_label.set_visible(False)
                server_row.set_subtitle(_("Choose a server…"))
            else:
                nick = getattr(conn, 'nickname', '') or '?'
                host = getattr(conn, 'hostname', '') or getattr(conn, 'host', '')
                user = getattr(conn, 'username', '')
                chosen_label.set_label(nick)
                chosen_label.set_visible(True)
                server_row.set_subtitle(
                    f"{user}@{host}" if user and host else (host or _("Selected server")))

        def _open_picker(_widget=None):
            show_host_picker(
                self, pick_btn, _set_target,
                toast=lambda msg: self._simple_dialog(_("No servers"), msg),
                connections=connections,
            )

        pick_btn.connect('clicked', _open_picker)
        server_row.connect('activated', lambda _r: _open_picker())
        _set_target(connections[0] if connections else None)
        group.add(server_row)

        dir_row = Adw.EntryRow(title=_("Remote directory"))
        dir_row.set_text("~/sshpilot-backups/")
        group.add(dir_row)
        page.add(group)
        toolbar.set_content(page)
        dialog.set_child(toolbar)

        def on_continue(_btn):
            target = selected_target[0]
            if target is None:
                self._simple_dialog(
                    _("Choose a server"),
                    _("Pick a server from your inventory to import from."))
                return
            dialog.close()
            remote = dir_row.get_text().strip() or "~/sshpilot-backups"
            self._ssh_import_list_backups(target, remote)

        continue_btn.connect('clicked', on_continue)
        dialog.present(self)

    def _ssh_import_list_backups(self, target, remote_dir):
        from .bitwarden_backup_setup import progress_dialog
        controller = self._secrets_controller()
        nick = getattr(target, 'nickname', '') or getattr(target, 'hostname', '') or ''
        cancelled = {'v': False}
        _set_status, close_spinner = progress_dialog(
            self, _("Import from SSH Server"), _("Loading backups…"),
            on_cancel=lambda: cancelled.__setitem__('v', True))

        prompts = self._present_ssh_backup_prompts(target, nick)

        def worker():
            try:
                payload = ('ok', controller.list_ssh_backups(
                    connection_id=nick, remote_dir=remote_dir))
            except Exception as e:
                logger.error("Listing SSH backups failed: %s", e)
                # A reachable server with an empty directory returns []; this
                # branch means the connect itself failed (cancelled prompt,
                # rejected host key, refused directory), which must not be
                # reported as "no backups found".
                payload = ('error', format_secret_transfer_error(e) or str(e))
            GLib.idle_add(lambda: (_after(payload), False)[1])

        def _after(p):
            self._close_ssh_backup_prompts(prompts)
            if cancelled['v']:
                return
            close_spinner()
            if p[0] != 'ok':
                self._simple_dialog(_("Import Failed"), p[1])
                return
            entries = p[1]
            if not entries:
                self._simple_dialog(
                    _("No backups found"),
                    _("No SSH Pilot backups were found in {path} on that server.").format(path=remote_dir))
                return
            self._choose_backup_entry(
                entries, heading=_("Import from SSH Server"),
                on_chosen=lambda entry: self._ssh_import_preview(target, remote_dir, entry))

        threading.Thread(target=worker, daemon=True).start()

    def _ssh_import_preview(self, target, remote_dir, entry):
        """Ask the daemon to preview one SSH-stored backup (it owns the download and
        decryption), then show the import-mode dialog."""
        from .bitwarden_backup_setup import progress_dialog
        controller = self._secrets_controller()
        nick = getattr(target, 'nickname', '') or getattr(target, 'hostname', '') or ''
        entry_id = entry.get('id', '') if isinstance(entry, dict) else getattr(entry, 'id', '')
        cancelled = {'v': False}
        _set_status, close_spinner = progress_dialog(
            self, _("Import from SSH Server"), _("Reading backup…"),
            on_cancel=lambda: cancelled.__setitem__('v', True))

        prompts = self._present_ssh_backup_prompts(target, nick)

        def worker():
            try:
                payload = ('ok', controller.preview_ssh_backup(
                    connection_id=nick, remote_dir=remote_dir, entry_id=entry_id))
            except Exception as e:
                logger.error("Reading SSH backup failed: %s", e)
                payload = ('error', str(e))
            GLib.idle_add(lambda: (_after(payload), False)[1])

        def _after(p):
            self._close_ssh_backup_prompts(prompts)
            if cancelled['v']:
                return
            close_spinner()
            if p[0] != 'ok':
                self._simple_dialog(_("Import Failed"), p[1])
                return
            preview = p[1]
            if preview.error is not None:
                self._simple_dialog(
                    _("Import Failed"),
                    format_secret_transfer_message(preview.error),
                )
                return
            self._show_import_mode_dialog(
                preview.included,
                self._make_ssh_import_apply(
                    nick, remote_dir, entry_id, preview.included,
                    encrypted=bool(getattr(preview, 'encrypted', False))),
                source_kind='ssh')

        threading.Thread(target=worker, daemon=True).start()

    def _begin_import(self, import_path: str):
        """Route an import by format: .spbk (daemon preview) vs legacy JSON."""
        try:
            from .backup_archive import is_spbk
            if is_spbk(import_path):
                self._import_spbk(import_path)
            else:
                self._show_import_mode_dialog(
                    None, self._make_json_import_apply(import_path),
                    source_kind='json')
        except Exception as e:
            logger.error(f"Failed to start import: {e}")
            self._simple_dialog(_("Import Failed"), str(e))

    @staticmethod
    def _safe_unlink(path):
        try:
            if path:
                os.unlink(path)
        except OSError:
            pass

    def _import_spbk(self, import_path: str, on_cleanup=None):
        """Ask the daemon to preview a .spbk (it owns decryption), then show the import-mode
        dialog with the included categories.

        ``on_cleanup`` (optional) fires once the preview resolves (or on any terminal
        error) — used to delete a downloaded temp file that's no longer needed."""
        from .bitwarden_backup_setup import progress_dialog
        controller = self._secrets_controller()
        cancelled = {'v': False}
        _set_status, close_spinner = progress_dialog(
            self, _("Import Configuration"), _("Reading backup…"),
            on_cancel=lambda: cancelled.__setitem__('v', True))

        def worker():
            try:
                payload = ('ok', controller.preview_backup(source=import_path))
            except Exception as e:
                logger.error(f"Failed to preview backup: {e}")
                payload = ('error', str(e))
            GLib.idle_add(lambda: (_after(payload), False)[1])

        def _after(p):
            if on_cleanup:
                on_cleanup()
            if cancelled['v']:
                return
            close_spinner()
            if p[0] != 'ok':
                self._simple_dialog(_("Import Failed"), p[1])
                return
            preview = p[1]
            if preview.error is not None:
                self._simple_dialog(
                    _("Import Failed"),
                    format_secret_transfer_message(preview.error),
                )
                return
            self._show_import_mode_dialog(
                preview.included,
                self._make_spbk_import_apply(import_path, preview.included),
                source_kind='spbk')

        threading.Thread(target=worker, daemon=True).start()

    def _show_import_mode_dialog(self, included, on_apply, *, source_kind='spbk'):
        """Show dialog to select import mode (replace or merge) and — when the daemon
        previewed the manifest — which categories to restore.

        ``included`` is the daemon's per-category map (``None`` for legacy JSON,
        which has no previewable manifest). ``on_apply(mode, restore_options)`` runs
        the daemon-owned import; the frontend never holds the manifest."""
        try:
            from sshpilot import icon_utils

            dialog = Adw.Dialog()
            dialog.set_title(_("Import Configuration"))
            dialog.set_content_width(BACKUP_DIALOG_MIN_WIDTH)
            dialog.set_follows_content_size(True)

            toolbar = Adw.ToolbarView()
            header = Adw.HeaderBar()
            header.set_show_start_title_buttons(False)
            header.set_show_end_title_buttons(False)
            header.set_title_widget(Adw.WindowTitle(
                title=_("Import Configuration"),
                subtitle=_("Choose how to import the configuration."),
            ))

            cancel_btn = Gtk.Button(label=_("Cancel"))
            cancel_btn.connect('clicked', lambda _b: dialog.close())
            header.pack_start(cancel_btn)

            import_btn = Gtk.Button(label=_("Import"))
            import_btn.add_css_class('suggested-action')
            header.pack_end(import_btn)
            toolbar.add_top_bar(header)

            page = Adw.PreferencesPage()
            toolbar.set_content(page)
            dialog.set_child(toolbar)

            mode_group = Adw.PreferencesGroup(title=_("Import mode"))
            mode_row = Adw.ComboRow(title=_("Mode"))
            mode_row.set_model(Gtk.StringList.new([
                _("Replace current configuration"),
                _("Merge with current configuration"),
            ]))
            mode_group.add(mode_row)
            page.add(mode_group)

            def sync_mode_subtitle(*_a):
                if mode_row.get_selected() == 0:
                    mode_row.set_subtitle(
                        _("All current settings will be replaced with the imported "
                          "configuration."))
                else:
                    mode_row.set_subtitle(
                        _("Add new connections and groups; preserve existing ones."))
            mode_row.connect('notify::selected', sync_mode_subtitle)
            # Merge is the non-destructive choice (and the daemon's own default when
            # a caller omits ``mode`` entirely) - default the picker to it too, so
            # Replace is something you opt into rather than something you must
            # remember to opt out of.
            mode_row.set_selected(1)
            sync_mode_subtitle()

            restore_checks = {}
            if included is not None:
                restore_group = Adw.PreferencesGroup(title=_("Restore"))
                labels = {
                    'app_settings': _("App settings and groups"),
                    'ssh_config': _("Connection profiles (SSH config)"),
                    'known_hosts': _("Known hosts"),
                    'secrets': _("Saved secrets (passwords and passphrases)"),
                    'private_keys': _("Private key files"),
                }
                for key in _BACKUP_OPTION_KEYS:
                    row = Adw.SwitchRow(title=labels[key])
                    row.set_active(bool(included.get(key, False)))
                    row.set_sensitive(bool(included.get(key, False)))
                    if not included.get(key, False):
                        row.set_subtitle(_("This backup does not include this item."))
                    if key == 'private_keys':
                        try:
                            row.add_css_class('error')
                        except Exception:
                            pass
                        row.add_prefix(
                            icon_utils.new_image_from_icon_name('dialog-warning-symbolic'))
                    restore_checks[key] = row
                    restore_group.add(row)
                page.add(restore_group)

            notice_group = Adw.PreferencesGroup()
            notice_group.set_description(
                _("A protected backup will be created automatically before importing."))
            page.add(notice_group)

            def on_import(_btn):
                mode = 'replace' if mode_row.get_selected() == 0 else 'merge'
                if included is not None:
                    restore_options = {
                        key: check.get_active()
                        for key, check in restore_checks.items()
                    }
                    if not any(restore_options.values()):
                        self._simple_dialog(
                            _("Nothing selected"),
                            _("Choose at least one item to restore from this backup."))
                        dialog.close()
                        GLib.idle_add(lambda: (self._show_import_mode_dialog(
                            included, on_apply, source_kind=source_kind), False)[1])
                        return
                    dialog.close()
                    will_replace_ssh = restore_options.get('ssh_config', False)
                    self._guard_default_mode_replace(
                        mode, will_replace_ssh,
                        lambda: on_apply(mode, restore_options))
                else:
                    # Legacy JSON: we can't see categories up front, so assume ssh_config.
                    dialog.close()
                    self._guard_default_mode_replace(
                        mode, True,
                        lambda: on_apply(mode, None))

            import_btn.connect('clicked', on_import)
            dialog.present(self)

        except Exception as e:
            logger.error(f"Failed to show import mode dialog: {e}")

    def _guard_default_mode_replace(self, mode: str, will_replace_ssh: bool, proceed):
        """Ask the daemon whether a replace targets shared user SSH state."""
        if mode != 'replace' or not will_replace_ssh:
            proceed()
            return
        owner = getattr(self, 'parent_window', None) or self
        client = getattr(self, 'client', None) or getattr(owner, 'client', None)
        bridge = getattr(self, 'client_bridge', None) or getattr(owner, 'client_bridge', None)
        try:
            from .api.capabilities import Capability
            mode_supported = (
                client is not None
                and client.get_capabilities().supports(Capability.OPERATION_MODE)
            )
        except Exception:
            mode_supported = False
        if client is None or bridge is None or not mode_supported:
            self._simple_dialog(
                _("Import Unavailable"),
                _("The daemon is unavailable, so SSH restore safety could not be verified."),
            )
            return

        def _on_status(status):
            from .api.models.daemon import OperationMode

            if status.active_mode is not OperationMode.DEFAULT:
                proceed()
                return
            warn = Adw.MessageDialog(
                transient_for=self,
                modal=True,
                heading=_("Replace your global SSH config?"),
                body=_(
                    "Replace mode will overwrite the Default user SSH configuration "
                    "entirely. That configuration is shared with ssh, scp, git and "
                    "other SSH tools; unmanaged Host, Include and Match rules may be "
                    "lost. Choose Merge instead to add hosts without overwriting."
                ),
            )
            warn.add_response('cancel', _('Cancel'))
            warn.add_response('replace', _('Replace Anyway'))
            warn.set_response_appearance('replace', Adw.ResponseAppearance.DESTRUCTIVE)
            warn.set_close_response('cancel')
            warn.connect('response', lambda _dlg, resp: proceed() if resp == 'replace' else None)
            warn.present()

        bridge.submit(
            client.get_operation_mode,
            on_success=_on_status,
            on_error=lambda _error: self._simple_dialog(
                _("Import Unavailable"),
                _("The daemon is unavailable, so SSH restore safety could not be verified."),
            ),
        )

    def _run_after_vault_unlock_for_secrets(self, proceed, *, needed: bool,
                                            cancelled_heading: str,
                                            on_cancelled=None):
        """Run ``proceed()`` once the session vault is unlocked when secrets are involved.

        The lock state comes from the daemon (the controller's ``load_state``). If the
        vault is locked, prompt to unlock through the daemon. When unlock fails or the
        user cancels, show ``cancelled_heading`` and do **not** call ``proceed()`` —
        export/import must not silently continue with zero credentials restored or included.

        ``on_cancelled`` (export flows) restores the options dialog instead of a
        dead-end cancel alert when the user dismisses the unlock prompt.
        """
        if not needed:
            proceed()
            return
        controller = getattr(self, "secrets_controller", None)
        if controller is None:
            self._simple_dialog(
                cancelled_heading,
                _("Secret storage is managed by the SSH Pilot daemon, which is not "
                  "connected, so the operation was cancelled."))
            return
        try:
            state = controller.load_state()
        except Exception:
            # If we cannot even verify the vault, abort rather than silently proceed with a
            # backup/restore that would be missing credentials.
            logger.warning("Pre-operation vault unlock check failed; aborting", exc_info=True)
            self._simple_dialog(
                cancelled_heading,
                _("Could not verify the secret vault, so the operation was cancelled to avoid "
                  "leaving saved credentials out. Try again."))
            return
        if state is None or not state.needs_unlock:
            proceed()
            return
        from .secret_unlock_dialog import prompt_unlock

        def _on_done(unlocked):
            if unlocked:
                proceed()
                return
            if on_cancelled is not None:
                on_cancelled()
                return
            self._simple_dialog(
                cancelled_heading,
                _("Your secret vault must be unlocked to include or restore saved "
                  "credentials. Unlock it and try again."))

        prompt_unlock(self, on_done=_on_done)

    def _make_spbk_import_apply(self, import_path, included):
        """Daemon-owned import apply for a ``.spbk`` file."""
        def apply(mode, restore_options):
            controller = self._secrets_controller()
            opts = {'mode': mode}
            if restore_options:
                opts.update(restore_options)
            self._run_daemon_import(
                lambda: controller.import_backup(source=import_path, options=opts),
                needed=bool((included or {}).get('secrets')),
                cancelled_heading=_("Import Cancelled"))
        return apply

    def _make_json_import_apply(self, import_path):
        """Daemon-owned import apply for a legacy JSON config (no secrets)."""
        def apply(mode, _restore_options):
            controller = self._secrets_controller()
            self._run_daemon_import(
                lambda: controller.import_backup(
                    source=import_path, options={'mode': mode}),
                needed=False,
                cancelled_heading=_("Import Cancelled"))
        return apply

    def _make_bitwarden_import_apply(self, entry_id, included):
        """Daemon-owned import apply for a Bitwarden backup note."""
        def apply(mode, restore_options):
            controller = self._secrets_controller()
            opts = {'mode': mode}
            if restore_options:
                opts.update(restore_options)
            self._run_daemon_import(
                lambda: controller.import_bitwarden_backup(
                    entry_id=entry_id, options=opts),
                needed=bool((included or {}).get('secrets')),
                cancelled_heading=_("Import Cancelled"))
        return apply

    def _make_ssh_import_apply(self, connection_id, remote_dir, entry_id, included,
                               encrypted=False):
        """Daemon-owned import apply for an SSH-stored backup.

        ``encrypted`` comes from the preview the user just confirmed. The daemon
        cannot probe a remote archive the way it probes a local ``.spbk``, so
        without this it would spend its first attempt (a connect and a full
        download) discovering that a passphrase is needed."""
        def apply(mode, restore_options):
            controller = self._secrets_controller()
            opts = {'mode': mode, 'encrypted': bool(encrypted)}
            if restore_options:
                opts.update(restore_options)
            self._run_daemon_import(
                lambda: controller.import_ssh_backup(
                    connection_id=connection_id, remote_dir=remote_dir,
                    entry_id=entry_id, options=opts),
                needed=bool((included or {}).get('secrets')),
                cancelled_heading=_("Import Cancelled"),
                ssh_target=connection_id)
        return apply

    def _run_daemon_import(self, run, *, needed: bool, cancelled_heading: str,
                           ssh_target=None):
        """Run a daemon-owned import via ``run()`` (zero-arg callable returning a
        ``SecretTransferResult``), gated on the vault unlock when the backup carries
        credentials, with a spinner. The frontend never holds the manifest.

        ``ssh_target`` names the server when the archive is being read off one,
        so the connect's own prompts can be presented for the length of the
        call; a local or Bitwarden import connects to nothing and passes none."""
        def do_apply(*_args):
            from .bitwarden_backup_setup import progress_dialog
            _set_status, close_spinner = progress_dialog(
                self, _("Import Configuration"),
                _("Applying backup — this may take a while…"))
            prompts = (
                self._present_ssh_backup_prompts(ssh_target)
                if ssh_target is not None
                else None
            )

            def worker():
                try:
                    payload = ('ok', run())
                except Exception as e:
                    logger.error("Backup import failed: %s", e)
                    payload = ('error', str(e))
                GLib.idle_add(lambda: (_report(payload), False)[1])

            def _report(payload):
                self._close_ssh_backup_prompts(prompts)
                close_spinner()
                if payload[0] == 'error':
                    self._simple_dialog(_("Import Failed"), payload[1])
                    return
                self._show_daemon_import_result(payload[1])

            threading.Thread(target=worker, daemon=True).start()

        self._run_after_vault_unlock_for_secrets(
            do_apply, needed=needed, cancelled_heading=cancelled_heading)

    def _show_daemon_import_result(self, result):
        """Display a daemon ``SecretTransferResult`` import outcome — counts and warnings only."""
        if result.status.value != 'success':
            msg = (
                format_secret_transfer_message(result.message)
                if result.message is not None
                else _("The backup could not be imported")
            )
            if result.warnings:
                msg += "\n\n" + "\n\n".join(
                    format_secret_transfer_messages(result.warnings)
                )
            self._simple_dialog(_("Import Failed"), msg)
            return
        counts = result.counts or {}
        restored = int(counts.get('restored', 0) or 0)
        skipped_creds = int(counts.get('skipped', 0) or 0)
        keys_written = int(counts.get('keys_written', 0) or 0)
        keys_skipped = int(counts.get('keys_skipped', 0) or 0)
        ignored = int(counts.get('ignored_secrets', 0) or 0)
        lines = []
        if not any((restored, keys_written, skipped_creds, keys_skipped, ignored)):
            lines.append(_("Backup imported successfully."))
        else:
            done = []
            if restored:
                done.append(_("{count} credential(s)").format(count=restored))
            if keys_written:
                done.append(_("{count} private key(s)").format(count=keys_written))
            if done:
                lines.append(_("Restored {items}.").format(items=_(", ").join(done)))
            if skipped_creds:
                lines.append(_("{} credential(s) already existed and were left untouched — "
                               "SSH Pilot never overwrites a saved secret.").format(skipped_creds))
            if keys_skipped:
                lines.append(_("{} private key(s) already existed and were left untouched — "
                               "SSH Pilot never overwrites a private key.").format(keys_skipped))
            if ignored:
                lines.append(_("{} saved password(s)/key(s) in this .json file were not "
                               "imported — legacy JSON backups can't restore secrets. Use an "
                               "encrypted .spbk backup to include them.").format(ignored))
        if result.warnings:
            lines.append("\n\n".join(
                format_secret_transfer_messages(result.warnings)
            ))
        lines.append(_("Reload now to apply the imported configuration. Some settings may still "
                       "need a full restart of SSH Pilot to take effect."))
        body = "\n\n".join(lines)

        success_dialog = Adw.MessageDialog(
            transient_for=self, modal=True, heading=_("Import Successful"), body=body)
        success_dialog.add_response('ok', _('OK'))
        success_dialog.add_response('restart', _('Reload Now'))
        success_dialog.set_response_appearance('restart', Adw.ResponseAppearance.SUGGESTED)

        def on_success_response(dialog, response):
            if response == 'restart':
                try:
                    self.config.reload_json_cache_strict()
                    if self.connection_manager:
                        self.connection_manager.refresh()
                    if self.group_manager:
                        self.group_manager._load_groups()
                    self.rebuild_connection_list()
                    self.toast_overlay.add_toast(Adw.Toast.new(_("Configuration reloaded")))
                except Exception as e:
                    logger.error(f"Failed to reload configuration: {e}")
            dialog.destroy()

        success_dialog.connect('response', on_success_response)
        success_dialog.present()

    # --- Group management dialogs (create / edit / rename tag / assign) -----

    def _group_form_dialog(self, *, title, label, confirm_label, on_confirm,
                           initial_name='', placeholder='', select_text=False,
                           with_color=True, initial_color=None):
        """Small Adw.Dialog form: name entry plus an optional color row.

        ``on_confirm(name, color)`` runs on the confirm button; return True to
        close the dialog, False to keep it open (e.g. validation failed).
        ``color`` is an RGBA string, or None when unset/cleared.

        The *on_confirm* callback may also accept a ``set_busy`` keyword
        argument ``(set_busy: Callable[[bool], None])``.  When provided the
        dialog disables all controls while the callback is running so that
        the user cannot re-submit; the callback re-enables them by calling
        ``set_busy(False)`` on failure.

        The *on_confirm* callback may also accept a ``close_dialog`` keyword
        argument ``(close_dialog: Callable[[], None])``.  When provided it
        lets an asynchronous callback dismiss the dialog once the operation
        has actually completed.
        """
        dialog = Adw.Dialog()
        dialog.set_title(title)
        dialog.set_content_width(400)

        header = Adw.HeaderBar()
        header.set_show_start_title_buttons(False)
        header.set_show_end_title_buttons(False)

        cancel_button = Gtk.Button(label=_('Cancel'))
        header.pack_start(cancel_button)

        confirm_button = Gtk.Button(label=confirm_label)
        confirm_button.add_css_class('suggested-action')
        header.pack_end(confirm_button)

        content_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content_area.set_margin_start(20)
        content_area.set_margin_end(20)
        content_area.set_margin_top(20)
        content_area.set_margin_bottom(20)
        content_area.set_spacing(12)

        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(header)
        toolbar_view.set_content(content_area)
        dialog.set_child(toolbar_view)

        form_label = Gtk.Label(label=label)
        form_label.set_wrap(True)
        form_label.set_xalign(0)
        content_area.append(form_label)

        entry = Gtk.Entry()
        if initial_name:
            entry.set_text(initial_name)
        if placeholder:
            entry.set_placeholder_text(placeholder)
        entry.set_activates_default(True)
        entry.set_hexpand(True)
        # Derive the accessible name from the prompt already shown above the
        # entry; a bare Gtk.Entry is otherwise exposed unnamed.
        set_accessible_name(entry, label)
        content_area.append(entry)

        color_button = None
        color_selected = False
        if with_color:
            color_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            color_row.set_hexpand(True)
            color_label = Gtk.Label(label=_("Group color"))
            color_label.set_xalign(0)
            color_label.set_hexpand(True)
            color_row.append(color_label)

            color_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            color_button = Gtk.ColorButton()
            color_button.set_use_alpha(True)
            color_button.set_title(_("Select group color"))
            rgba = Gdk.RGBA()
            if initial_color:
                try:
                    if rgba.parse(initial_color):
                        color_selected = True
                    else:
                        rgba.red = rgba.green = rgba.blue = 0
                        rgba.alpha = 0
                except Exception:
                    rgba.red = rgba.green = rgba.blue = 0
                    rgba.alpha = 0
            else:
                rgba.red = rgba.green = rgba.blue = 0
                rgba.alpha = 0
            color_button.set_rgba(rgba)

            def on_color_set(_button):
                nonlocal color_selected
                color_selected = True

            color_button.connect('color-set', on_color_set)
            color_controls.append(color_button)

            clear_color_button = Gtk.Button(label=_("Clear"))
            clear_color_button.add_css_class('flat')

            def on_clear_color(_button):
                nonlocal color_selected
                color_selected = False
                cleared = Gdk.RGBA()
                cleared.red = cleared.green = cleared.blue = 0
                cleared.alpha = 0
                color_button.set_rgba(cleared)

            clear_color_button.connect('clicked', on_clear_color)
            color_controls.append(clear_color_button)

            color_row.append(color_controls)
            content_area.append(color_row)

        def _set_controls_enabled(enabled: bool) -> None:
            entry.set_sensitive(enabled)
            confirm_button.set_sensitive(enabled)
            cancel_button.set_sensitive(enabled)
            if color_button is not None:
                color_button.set_sensitive(enabled)

        def on_confirm_clicked(_button):
            name = entry.get_text().strip()
            color = None
            if color_button is not None:
                rgba_value = color_button.get_rgba()
                if color_selected and rgba_value.alpha > 0:
                    color = rgba_value.to_string()
            try:
                result = on_confirm(
                    name,
                    color,
                    set_busy=lambda busy: _set_controls_enabled(not busy),
                    close_dialog=dialog.close,
                )
            except TypeError:
                try:
                    result = on_confirm(
                        name,
                        color,
                        set_busy=lambda busy: _set_controls_enabled(not busy),
                    )
                except TypeError:
                    result = on_confirm(name, color)
            if result is True:
                dialog.close()

        cancel_button.connect('clicked', lambda _b: dialog.close())
        confirm_button.connect('clicked', on_confirm_clicked)
        dialog.set_default_widget(confirm_button)
        dialog.present(self)

        def focus_entry():
            entry.grab_focus()
            if select_text:
                entry.select_region(0, -1)
            return False

        GLib.idle_add(focus_entry)

    def on_create_group_action(self, action, param=None):
        """Handle create group action."""
        try:
            def create(name, color, set_busy=None, close_dialog=None):
                if not name:
                    self._simple_dialog(_("Error"), _("Please enter a group name."))
                    return False
                controller = getattr(self.group_manager, 'controller', None)
                if controller is None:
                    self._simple_dialog(
                        _("Service unavailable"),
                        _("Connect to the SSH Pilot daemon before creating groups."),
                    )
                    return False
                if set_busy is not None:
                    set_busy(True)
                def _do_create():
                    return controller.client.create_group(
                        name, parent_id="", color=color or "",
                    )
                def _on_created(new_group_id):
                    self.rebuild_connection_list()
                    if set_busy is not None:
                        set_busy(False)
                    if close_dialog is not None:
                        close_dialog()
                    return True
                def _on_error(error):
                    if set_busy is not None:
                        set_busy(False)
                    self._simple_dialog(
                        _("Error"),
                        _("Failed to create group: {error}").format(
                            error=str(error),
                        ),
                    )
                controller.run(
                    _do_create,
                    on_success=_on_created,
                    on_error=_on_error,
                )
                return False  # keep dialog open until async completes

            self._group_form_dialog(
                title=_("Create New Group"),
                label=_("Enter a name for the new group:"),
                confirm_label=_('Create'),
                on_confirm=create,
                placeholder=_("e.g., Production Servers"),
            )
        except Exception as e:
            logger.error(f"Failed to show create group dialog: {e}")

    def on_edit_group_action(self, action, param=None):
        """Handle edit group action"""
        try:
            logger.debug("Edit group action triggered")
            # Get the group row from context menu or selected row
            selected_row = getattr(self, '_context_menu_group_row', None)
            if not selected_row:
                selected_row = self.connection_list.get_selected_row()
            if not selected_row:
                logger.debug("No selected row")
                return
            if not hasattr(selected_row, 'group_id'):
                logger.debug("Selected row is not a group row")
                return

            group_id = selected_row.group_id
            group_info = self.group_manager.groups.get(group_id)
            if not group_info:
                logger.debug(f"Group info not found for ID: {group_id}")
                return

            def save(name, color, set_busy=None, close_dialog=None):
                if not name:
                    self._simple_dialog(_("Error"), _("Please enter a group name."))
                    return False
                controller = getattr(self.group_manager, 'controller', None)
                if controller is None:
                    self._simple_dialog(
                        _("Service unavailable"),
                        _("Connect to the SSH Pilot daemon before editing groups."),
                    )
                    return False
                if set_busy is not None:
                    set_busy(True)
                from sshpilot.api.models.connection_store import (
                    GroupId,
                    SetGroupColorRequest,
                )
                steps = [
                    lambda _prev: controller.client.rename_group(group_id, name),
                    lambda _prev: controller.client.set_group_color(
                        SetGroupColorRequest(
                            group_id=GroupId(group_id),
                            color=color or "",
                        )
                    ),
                ]

                def _on_done(_result):
                    self.rebuild_connection_list()
                    if set_busy is not None:
                        set_busy(False)
                    if close_dialog is not None:
                        close_dialog()
                def _on_error(error):
                    if set_busy is not None:
                        set_busy(False)
                    self._simple_dialog(
                        _("Error"),
                        _("Failed to update group: {error}").format(
                            error=str(error),
                        ),
                    )
                controller.run_sequence(
                    steps,
                    on_success=_on_done,
                    on_error=_on_error,
                )
                return False  # keep dialog open until async completes

            self._group_form_dialog(
                title=_("Edit Group"),
                label=_("Enter a new name for the group:"),
                confirm_label=_('Save'),
                on_confirm=save,
                initial_name=group_info['name'],
                select_text=True,
                initial_color=group_info.get('color'),
            )
        except Exception as e:
            logger.error(f"Failed to show edit group dialog: {e}")

    def on_rename_tag_action(self, tag_row):
        """Rename a virtual tag group: rewrite the tag on all tagged connections."""
        try:
            if not getattr(tag_row, 'is_tag_group', False):
                return
            if tag_row.group_info.get('untagged'):
                return  # the Untagged section is not a real tag
            old_name = str(tag_row.group_info.get('name', ''))
            old_key = str(tag_row.group_info.get('tag_key', '')) or old_name.casefold()

            def save(name, _color, set_busy=None, close_dialog=None):
                if not name:
                    self._simple_dialog(_("Error"), _("Please enter a tag name."))
                    return False
                if ',' in name:
                    self._simple_dialog(_("Error"), _("Tag names cannot contain commas."))
                    return False
                if name == old_name:
                    return True
                from .tag_groups import migrate_expanded_state
                controller = getattr(self.group_manager, 'controller', None)
                client = getattr(self.group_manager, 'client', None)
                if controller is not None and client is not None:
                    # Route authoritative tag mutation through the daemon RPC.
                    if set_busy is not None:
                        set_busy(True)
                    from sshpilot.api.models.connection_store import RenameTagRequest
                    def _do_rename():
                        return client.rename_tag(
                            RenameTagRequest(old_tag=old_name, new_tag=name),
                        )
                    def _on_done(_result):
                        # Update frontend expansion state only after daemon rename
                        # succeeds.
                        try:
                            state = self.config.get_setting('ui.tag_groups_expanded', {}) or {}
                            self.config.set_setting(
                                'ui.tag_groups_expanded',
                                migrate_expanded_state(state, old_key, name.casefold()),
                            )
                        except Exception:
                            logger.debug("Failed to migrate tag expansion state", exc_info=True)
                        self.rebuild_connection_list()
                        if set_busy is not None:
                            set_busy(False)
                        if close_dialog is not None:
                            close_dialog()
                    def _on_error(error):
                        if set_busy is not None:
                            set_busy(False)
                        self._simple_dialog(
                            _("Error"),
                            _("Failed to rename tag: {error}").format(
                                error=str(error),
                            ),
                        )
                    controller.run(
                        _do_rename,
                        on_success=_on_done,
                        on_error=_on_error,
                    )
                    return False  # keep dialog open until async completes
                # No controller/client available — service unavailable.
                self._simple_dialog(
                    _("Service unavailable"),
                    _("Connect to the SSH Pilot daemon before renaming tags."),
                )
                return False

            self._group_form_dialog(
                title=_("Rename Tag"),
                label=_("Enter a new name for the tag:"),
                confirm_label=_('Save'),
                on_confirm=save,
                initial_name=old_name,
                select_text=True,
                with_color=False,
            )
        except Exception as e:
            logger.error(f"Failed to show rename tag dialog: {e}")


    def on_move_to_group_action(self, action, param=None):
        """Handle move to group action"""
        self._open_group_assignment_dialog('move')

    def on_copy_to_group_action(self, action, param=None):
        """Handle copy to group action (keeps existing memberships)"""
        self._open_group_assignment_dialog('copy')

    def _open_group_assignment_dialog(self, mode: str = 'move'):
        """Show the dialog used by both 'Move to Group' and 'Copy to Group'.

        ``mode`` is either ``'move'`` (relocate the connection to the chosen
        group) or ``'copy'`` (add it to the chosen group while keeping it in any
        group it already belongs to).

        All mutations run through the controller as serialized sequences.  The
        dialog disables its controls while busy and only closes after the full
        sequence succeeds.
        """
        is_copy = mode == 'copy'
        try:
            connections = self._get_target_connections(prefer_context=True)
            if not connections:
                return

            connection_nicknames = [
                conn.nickname for conn in connections if hasattr(conn, 'nickname')
            ]
            if not connection_nicknames:
                return

            controller = getattr(self.group_manager, 'controller', None)
            if controller is None:
                self._simple_dialog(
                    _("Service unavailable"),
                    _("Connect to the SSH Pilot daemon before using group operations."),
                )
                return

            available_groups = self.get_available_groups()
            logger.debug(f"Available groups for {mode} dialog: {len(available_groups)} groups")

            from sshpilot import icon_utils

            title_text = _("Copy to Group") if is_copy else _("Move to Group")
            confirm_label = _("Copy") if is_copy else _("Move")

            # Adwaita dialog scaffold (GNOME HIG): Adw.Dialog + ToolbarView/HeaderBar
            # with a PreferencesPage body (provides insets and grouped row styling).
            dialog = Adw.Dialog()
            dialog.set_title(title_text)
            dialog.set_content_width(420)
            dialog.set_follows_content_size(True)

            toolbar_view = Adw.ToolbarView()
            header = Adw.HeaderBar()
            header.set_show_start_title_buttons(False)
            header.set_show_end_title_buttons(False)

            cancel_button = Gtk.Button(label=_("Cancel"))
            cancel_button.connect('clicked', lambda _b: dialog.close())
            header.pack_start(cancel_button)

            confirm_button = Gtk.Button(label=confirm_label)
            confirm_button.add_css_class('suggested-action')
            header.pack_end(confirm_button)

            toolbar_view.add_top_bar(header)

            page = Adw.PreferencesPage()
            body_scroller = Gtk.ScrolledWindow()
            body_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            body_scroller.set_propagate_natural_height(True)
            body_scroller.set_max_content_height(420)
            body_scroller.set_child(page)
            toolbar_view.set_content(body_scroller)
            dialog.set_child(toolbar_view)

            # --- Create new group -------------------------------------------
            create_group = Adw.PreferencesGroup()
            create_group.set_title(_("Create New Group"))
            if len(connection_nicknames) == 1:
                create_group.set_description(
                    _("Copy the connection to a new group") if is_copy
                    else _("Move the connection to a new group")
                )
            else:
                create_group.set_description(
                    _("Copy the selected connections to a new group") if is_copy
                    else _("Move the selected connections to a new group")
                )

            create_group_entry = Adw.EntryRow()
            create_group_entry.set_title(_("Group name"))
            create_group.add(create_group_entry)

            color_row = Adw.ActionRow()
            color_row.set_title(_("Color"))
            color_row.set_subtitle(_("Optional"))

            color_button = Gtk.ColorButton()
            color_button.set_valign(Gtk.Align.CENTER)
            color_button.set_use_alpha(True)
            color_button.set_title(_("Select group color"))
            initial_rgba = Gdk.RGBA()
            initial_rgba.red = initial_rgba.green = initial_rgba.blue = 0
            initial_rgba.alpha = 0
            color_button.set_rgba(initial_rgba)

            color_selected = False

            def mark_color_selected(_button):
                nonlocal color_selected
                color_selected = True

            color_button.connect('color-set', mark_color_selected)

            def reset_color_selection() -> None:
                nonlocal color_selected
                color_selected = False
                cleared = Gdk.RGBA()
                cleared.red = cleared.green = cleared.blue = 0
                cleared.alpha = 0
                color_button.set_rgba(cleared)

            clear_color_button = Gtk.Button(label=_("Clear"))
            clear_color_button.set_valign(Gtk.Align.CENTER)
            clear_color_button.add_css_class('flat')
            clear_color_button.connect('clicked', lambda _btn: reset_color_selection())

            color_row.add_suffix(color_button)
            color_row.add_suffix(clear_color_button)
            create_group.add(color_row)
            page.add(create_group)

            # --- Existing groups (single selection via a checkmark) ---------
            existing_group = Adw.PreferencesGroup()
            existing_group.set_title(_("Existing Groups"))

            selected_group_id = None
            selected_row_ref = None

            def has_valid_target() -> bool:
                if create_group_entry.get_text().strip():
                    return True
                return selected_group_id is not None

            def update_confirm_state(*_args):
                confirm_button.set_sensitive(has_valid_target())

            def select_group_row(row) -> None:
                nonlocal selected_group_id, selected_row_ref
                if selected_row_ref is row:
                    return
                if selected_row_ref is not None:
                    selected_row_ref._check.set_visible(False)
                    set_accessible_selected(selected_row_ref, False)
                selected_row_ref = row
                selected_group_id = row.group_id
                row._check.set_visible(True)
                # The chosen row is marked only by a checkmark image, which
                # tells assistive technology nothing; publish the state too.
                set_accessible_selected(row, True)
                update_confirm_state()

            if available_groups:
                for group in available_groups:
                    row = Adw.ActionRow()
                    row.set_title(group['name'])
                    row.set_activatable(True)
                    icon = icon_utils.new_image_from_icon_name('folder-symbolic')
                    row.add_prefix(icon)
                    check = Gtk.Image.new_from_icon_name('object-select-symbolic')
                    check.set_visible(False)
                    set_accessible_name(check, _("Selected"))
                    row.add_suffix(check)
                    row._check = check
                    row.group_id = group['id']
                    row.connect('activated', select_group_row)
                    set_accessible_selected(row, False)
                    existing_group.add(row)
            else:
                existing_group.set_description(_("No groups yet — create one above."))

            page.add(existing_group)

            # --- Validation + actions (business logic preserved) ------------
            def find_existing_group_id(name: str):
                lowered = name.lower()
                for group in available_groups:
                    if group['name'].lower() == lowered:
                        return group['id']
                return None

            def show_group_exists_error(message: str) -> None:
                error = Adw.AlertDialog(
                    heading=_("Group Already Exists"),
                    body=message,
                )
                error.add_response('ok', _("OK"))
                error.set_default_response('ok')
                error.set_close_response('ok')
                error.present(dialog)

            create_group_entry.connect('changed', update_confirm_state)
            update_confirm_state()

            def _set_controls_enabled(enabled: bool) -> None:
                """Disable all dialog controls while an async operation runs."""
                confirm_button.set_sensitive(enabled)
                cancel_button.set_sensitive(enabled)
                create_group_entry.set_sensitive(enabled)
                color_button.set_sensitive(enabled)
                clear_color_button.set_sensitive(enabled)

            def _show_error(message: str) -> None:
                _set_controls_enabled(True)
                self._simple_dialog(_("Error"), message)

            def _build_move_steps(nicknames, target_group_id):
                """Build sequence steps to move/copy all nicknames to target_group_id."""
                steps = []
                if is_copy:
                    from sshpilot.api.models.connection_store import (
                        ConnectionId, CopyConnectionToGroupRequest, GroupId,
                    )
                    for nick in nicknames:
                        steps.append(
                            lambda _prev, n=nick: controller.client.copy_connection_to_group(
                                CopyConnectionToGroupRequest(
                                    connection_id=ConnectionId(n),
                                    group_id=GroupId(target_group_id),
                                )
                            )
                        )
                else:
                    for nick in nicknames:
                        steps.append(
                            lambda _prev, n=nick: controller.client.assign_connection_to_group(
                                n, target_group_id
                            )
                        )
                return steps

            def perform_move() -> bool:
                group_name = create_group_entry.get_text().strip()
                if group_name:
                    existing_group_id = find_existing_group_id(group_name)
                    if existing_group_id:
                        steps = _build_move_steps(connection_nicknames, existing_group_id)
                    else:
                        selected_color = None
                        rgba_value = color_button.get_rgba()
                        if color_selected and rgba_value.alpha > 0:
                            selected_color = rgba_value.to_string()
                        # Reuse the production step builder so the
                        # create-group result is validated as a nonempty
                        # string group ID and reused across every assignment
                        # step (run_sequence only threads the previous step's
                        # return value, which is a bool for assignment RPCs).
                        from sshpilot.gtk.group_store import (
                            build_create_and_assign_steps,
                        )
                        steps = build_create_and_assign_steps(
                            controller.client,
                            group_name,
                            connection_nicknames,
                            is_copy=is_copy,
                            color=selected_color or "",
                        )
                elif selected_group_id is not None:
                    steps = _build_move_steps(connection_nicknames, selected_group_id)
                else:
                    return False

                _set_controls_enabled(False)

                def _on_done(_result):
                    dialog.close()
                    self.rebuild_connection_list()

                def _on_error(error):
                    _show_error(
                        _("Failed to {mode}: {error}").format(
                            mode=mode, error=str(error),
                        )
                    )

                controller.run_sequence(
                    steps,
                    on_success=_on_done,
                    on_error=_on_error,
                )
                return False  # dialog stays open until async completes

            def on_confirm(*_args):
                if has_valid_target():
                    perform_move()

            confirm_button.connect('clicked', on_confirm)
            create_group_entry.connect('entry-activated', lambda _e: on_confirm())

            dialog.present(self)

        except Exception as e:
            logger.error(f"Failed to show move to group dialog: {e}")
