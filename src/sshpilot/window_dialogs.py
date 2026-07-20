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

from gi.repository import Adw, Gdk, Gio, GLib, Gtk
from gettext import gettext as _

from .platform_utils import get_config_dir

logger = logging.getLogger(__name__)

# Minimum content width for export/import backup dialogs.
BACKUP_DIALOG_MIN_WIDTH = 520
# Export uses a normal modal window (wider than the Adw.Dialog sheets).
BACKUP_EXPORT_WINDOW_WIDTH = 640
BACKUP_EXPORT_WINDOW_HEIGHT = 720
BACKUP_EXPORT_CLAMP_MAX = 560


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
    2. Any other visible modal secondary window (last = most recently mapped).
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
        return modal_secondary[-1]
    if active_window is not None and active_window is not main_window \
            and _visible(active_window):
        return active_window
    return main_window


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
    )


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
    allow_store = prompt_type in ("password", "passphrase")

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

    persists_secrets = True
    try:
        from .secret_storage import get_secret_manager
        persists_secrets = get_secret_manager().persists_secrets()
    except Exception:
        persists_secrets = True

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
                "are not saved by sshPilot."
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
                    elif prompt_type == "passphrase" and key_path:
                        try:
                            from .askpass_utils import store_passphrase
                            store_passphrase(key_path, entered)
                        except Exception as e:
                            logger.debug("Failed to store passphrase: %s", e)
                    elif prompt_type == "password" and connection_manager:
                        try:
                            if connection is not None and hasattr(
                                connection_manager, "store_connection_password"
                            ):
                                connection_manager.store_connection_password(
                                    connection, entered, username=username
                                )
                            elif host and username:
                                from .credential_model import canonical_password_host
                                canonical = canonical_password_host(
                                    {
                                        "hostname": host,
                                        "host": host,
                                        "username": username,
                                    }
                                )
                                store_host = canonical or host
                                connection_manager.store_password(
                                    store_host, username, entered
                                )
                        except Exception as e:
                            logger.debug("Failed to store password: %s", e)
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

    main_loop.run()
    return password_result[0]


class WindowConfigDialogsMixin:
    """Known-hosts editor, preferences, and config export/import dialogs."""

    def show_known_hosts_editor(self):
        """Show known hosts editor window"""
        logger.info("Show known hosts editor window")
        try:
            from .known_hosts_editor import KnownHostsEditorWindow
            editor = KnownHostsEditorWindow(self, self.connection_manager)
            editor.present()
        except Exception as e:
            logger.error(f"Failed to open known hosts editor: {e}")

    def show_preferences(self):
        """Show preferences dialog"""
        logger.info("Show preferences dialog")
        existing = getattr(self, '_preferences_window', None)
        if existing is not None:
            try:
                # Adw.Dialog.present() takes the parent to present over.
                existing.present(self)
                return
            except Exception:
                self._preferences_window = None
        try:
            # Imported lazily so the heavy Preferences module stays off the
            # startup path — it's only needed when the dialog is opened.
            from .preferences import PreferencesWindow
            preferences_window = PreferencesWindow(self, self.config)
            self._preferences_window = preferences_window
            # Adw.Dialog emits 'closed' rather than a window's 'close-request'.
            preferences_window.connect(
                'closed',
                lambda _d: setattr(self, '_preferences_window', None),
            )
            preferences_window.present(self)
        except Exception as e:
            logger.error(f"Failed to show preferences dialog: {e}")

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

    def _show_export_options_dialog(self, prefill_ids=None, encrypt_default=True,
                                    option_defaults=None, error=None,
                                    destination='file', target_nick=None, remote_dir=None):
        """Select backup categories, scoped connections, destination, and encryption."""
        try:
            connections = list(self.connection_manager.get_connections()) \
                if self.connection_manager else []
        except Exception:
            connections = []
        from .backup_manager import BackupManager
        from sshpilot import icon_utils
        option_defaults = BackupManager.normalize_backup_options(option_defaults)

        # Modal window + Clamp (same scaffold as session manager / key chooser).
        dialog = Adw.Window(transient_for=self, modal=True)
        dialog.set_title(_("Export Backup"))
        dialog.set_default_size(BACKUP_EXPORT_WINDOW_WIDTH, BACKUP_EXPORT_WINDOW_HEIGHT)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title=_("Export Backup")))

        cancel_btn = Gtk.Button(label=_("Cancel"))
        cancel_btn.connect('clicked', lambda _b: dialog.close())
        header.pack_start(cancel_btn)

        continue_btn = Gtk.Button(label=_("Continue"))
        continue_btn.add_css_class('suggested-action')
        header.pack_end(continue_btn)
        toolbar.add_top_bar(header)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)

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

        include_group = Adw.PreferencesGroup()
        include_group.set_title(_("Include"))
        include_group.set_description(_("Choose what this backup should contain."))
        include_group.add_css_class('boxed-list')

        def make_switch_row(title, active=False, subtitle=None):
            row = Adw.SwitchRow(title=title)
            if subtitle:
                row.set_subtitle(subtitle)
            row.set_active(bool(active))
            return row

        app_settings_row = make_switch_row(
            _("App settings and groups"), option_defaults.get('app_settings', False))
        ssh_config_row = make_switch_row(
            _("Connection profiles (SSH config)"), option_defaults.get('ssh_config', False))
        known_hosts_row = make_switch_row(
            _("Known hosts"), option_defaults.get('known_hosts', False))

        private_keys_row = make_switch_row(
            _("Private key files"), option_defaults.get('private_keys', False),
            subtitle=_("Not recommended — protect the backup with a passphrase if you include keys."))
        try:
            private_keys_row.add_css_class('error')
        except Exception:
            pass
        warn_icon = icon_utils.new_image_from_icon_name('dialog-warning-symbolic')
        private_keys_row.add_prefix(warn_icon)

        secrets_row = Adw.ExpanderRow(
            title=_("Saved secrets (passwords and passphrases)"),
            subtitle=_("Save passwords and passphrases for these connections"),
        )
        secrets_row.set_show_enable_switch(True)
        secrets_row.set_enable_expansion(bool(option_defaults.get('secrets', False)))
        secrets_row.set_expanded(False)

        select_all_row = Adw.ActionRow(title=_("Select all"))
        select_all_cb = Gtk.CheckButton()
        select_all_cb.set_valign(Gtk.Align.CENTER)
        select_all_cb.add_css_class('selection-mode')
        select_all_row.add_suffix(select_all_cb)
        select_all_row.set_activatable(True)
        select_all_row.connect(
            'activated',
            lambda _r: select_all_cb.set_active(not select_all_cb.get_active()),
        )
        secrets_row.add_row(select_all_row)

        # (cb, conn, key, action_row) — action_row is reparented when only private keys are on.
        checks = []
        prefill = set(prefill_ids or [])
        select_all_by_default = not prefill
        for conn in connections:
            try:
                label = getattr(conn, 'nickname', '') or conn.get_effective_host() or '?'
                key = getattr(conn, 'nickname', '') or label
            except Exception:
                label, key = '?', '?'
            conn_row = Adw.ActionRow(title=label)
            cb = Gtk.CheckButton()
            cb.set_active(True if select_all_by_default else key in prefill)
            cb.set_valign(Gtk.Align.CENTER)
            cb.add_css_class('selection-mode')
            conn_row.add_suffix(cb)
            conn_row.set_activatable(True)
            conn_row.connect(
                'activated',
                lambda _r, button=cb: button.set_active(not button.get_active()),
            )
            secrets_row.add_row(conn_row)
            checks.append((cb, conn, key, conn_row))

        # When only private keys are on, the secrets expander stays collapsed — show the same
        # connection picks in a sibling group (widgets are reparented, not duplicated).
        keys_conn_group = Adw.PreferencesGroup(
            title=_("Connections"),
            description=_("Select which connections' private key files to include."),
        )
        keys_conn_group.add_css_class('boxed-list')

        include_group.add(app_settings_row)
        include_group.add(ssh_config_row)
        include_group.add(known_hosts_row)
        include_group.add(private_keys_row)
        include_group.add(secrets_row)
        page.append(include_group)
        page.append(keys_conn_group)

        option_rows = {
            'app_settings': app_settings_row,
            'ssh_config': ssh_config_row,
            'known_hosts': known_hosts_row,
            'private_keys': private_keys_row,
        }

        def on_select_all(*_a):
            active = select_all_cb.get_active()
            for cb, _c, _k, _r in checks:
                cb.set_active(active)
        select_all_cb.connect('notify::active', on_select_all)
        select_all_cb.set_active(
            bool(checks) and all(cb.get_active() for cb, _c, _k, _r in checks)
        )

        def _secrets_enabled():
            return bool(secrets_row.get_enable_expansion())

        def _reparent_connection_rows(to_expander):
            """Move select-all + connection rows between the expander and keys_conn_group."""
            rows = [select_all_row] + [r for _cb, _c, _k, r in checks]
            for row in rows:
                parent = row.get_parent()
                if parent is not None:
                    parent.remove(row)
                if to_expander:
                    secrets_row.add_row(row)
                else:
                    keys_conn_group.add(row)

        def sync_connection_controls(*_a):
            secrets_on = _secrets_enabled()
            keys_on = private_keys_row.get_active()
            if secrets_on:
                _reparent_connection_rows(True)
                keys_conn_group.set_visible(False)
            elif keys_on:
                _reparent_connection_rows(False)
                keys_conn_group.set_visible(True)
            else:
                _reparent_connection_rows(True)
                keys_conn_group.set_visible(False)
                secrets_row.set_expanded(False)

        def on_private_keys_toggled(row, _pspec):
            if not private_keys_alert_guard[0] and row.get_active():
                def decline():
                    private_keys_alert_guard[0] = True
                    try:
                        row.set_active(False)
                    finally:
                        private_keys_alert_guard[0] = False

                self._alert_private_keys_export_risk(dialog, on_decline=decline)
            sync_connection_controls()

        private_keys_alert_guard = [False]
        secrets_row.connect('notify::enable-expansion', sync_connection_controls)
        private_keys_row.connect('notify::active', on_private_keys_toggled)
        sync_connection_controls()

        dest_group = Adw.PreferencesGroup(title=_("Destination"))
        dest_group.add_css_class('boxed-list')
        dest_labels = [
            _("File (.spbk)"),
            _("Bitwarden"),
            _("SSH server"),
        ]
        dest_keys = ['file', 'bitwarden', 'ssh']
        dest_row = Adw.ComboRow(title=_("Save to"))
        dest_row.set_model(Gtk.StringList.new(dest_labels))
        try:
            dest_row.set_selected(dest_keys.index(destination))
        except ValueError:
            dest_row.set_selected(0)
        dest_group.add(dest_row)

        mirror_logins_row = Adw.SwitchRow(
            title=_("Also copy saved secrets as Bitwarden login items"),
            subtitle=_("Creates normal login entries in your vault in addition to the backup note."),
        )
        # Indent under the Bitwarden destination so it reads as a dependent option.
        mirror_indent = Gtk.Box()
        mirror_indent.set_size_request(20, 1)
        mirror_logins_row.add_prefix(mirror_indent)
        dest_group.add(mirror_logins_row)

        # SSH target: searchable inventory host picker (same popover as jump-host / Docker).
        from .host_picker import show_host_picker
        selected_ssh_target = [None]

        def _ssh_target_label(conn):
            if conn is None:
                return ''
            return (getattr(conn, 'nickname', '')
                    or (conn.get_effective_host() if hasattr(conn, 'get_effective_host') else '')
                    or '?')

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

        def _set_ssh_target(conn):
            selected_ssh_target[0] = conn
            nick = _ssh_target_label(conn)
            if conn is None:
                chosen_label.set_label('')
                chosen_label.set_visible(False)
                server_row.set_subtitle(_("Choose a server…"))
            else:
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

        # Prefill: explicit nickname from a prior reopen, else first inventory host
        # (matches the old DropDown defaulting to index 0).
        prefill_conn = None
        if target_nick:
            for c in connections:
                if getattr(c, 'nickname', None) == target_nick:
                    prefill_conn = c
                    break
        if prefill_conn is None and connections:
            prefill_conn = connections[0]
        _set_ssh_target(prefill_conn)
        dest_group.add(server_row)

        ssh_dir_row = Adw.EntryRow(title=_("Remote directory"))
        ssh_dir_row.set_text(remote_dir or "~/sshpilot-backups/")
        dest_group.add(ssh_dir_row)
        page.append(dest_group)

        enc_group = Adw.PreferencesGroup(title=_("Encryption"))
        enc_group.add_css_class('boxed-list')
        enc_row = Adw.SwitchRow(
            title=_("Encrypt with a passphrase"),
            subtitle=_("Without a passphrase, secrets are written in plain text."),
        )
        enc_row.set_active(bool(encrypt_default))
        enc_group.add(enc_row)
        pw_row = Adw.PasswordEntryRow(title=_("Passphrase"))
        enc_group.add(pw_row)
        page.append(enc_group)

        def _dest_key():
            idx = dest_row.get_selected()
            if 0 <= idx < len(dest_keys):
                return dest_keys[idx]
            return 'file'

        def sync_pw(*_a):
            # Bitwarden uses the vault's own encryption — passphrase UI is for file/SSH only.
            key = _dest_key()
            to_bw = key == 'bitwarden'
            to_ssh = key == 'ssh'
            server_row.set_visible(to_ssh)
            ssh_dir_row.set_visible(to_ssh)
            enc_group.set_visible(not to_bw)
            on = enc_row.get_active() and not to_bw
            pw_row.set_visible(on)
            if to_bw:
                enc_row.set_subtitle(
                    _("Bitwarden encrypts the backup with your vault credentials."))
            else:
                enc_row.set_subtitle(
                    _("Without a passphrase, secrets are written in plain text."))
            mirror_logins_row.set_visible(to_bw and _secrets_enabled())

        def on_dest_changed(*_a):
            if _dest_key() == 'ssh':
                enc_row.set_active(True)
            sync_pw()

        enc_row.connect('notify::active', sync_pw)
        dest_row.connect('notify::selected', on_dest_changed)
        secrets_row.connect('notify::enable-expansion', sync_pw)
        sync_pw()

        def reopen(**kwargs):
            dialog.close()
            GLib.idle_add(lambda: (self._show_export_options_dialog(**kwargs), False)[1])

        def on_continue(_btn):
            selected = [conn for cb, conn, _k, _r in checks if cb.get_active()]
            sel_ids = [k for cb, _c, k, _r in checks if cb.get_active()]
            options = {key: row.get_active() for key, row in option_rows.items()}
            options['secrets'] = _secrets_enabled()
            dest = _dest_key()
            encrypt_on = enc_row.get_active()
            remote_text = ssh_dir_row.get_text()
            ssh_nick = getattr(selected_ssh_target[0], 'nickname', None) if selected_ssh_target[0] else None
            if not any(options.values()):
                reopen(
                    prefill_ids=sel_ids, encrypt_default=encrypt_on, option_defaults=options,
                    destination=dest, target_nick=ssh_nick, remote_dir=remote_text,
                    error=_("Choose at least one item to include in the backup."))
                return
            if (options.get('secrets') or options.get('private_keys')) and not selected:
                reopen(
                    prefill_ids=sel_ids, encrypt_default=encrypt_on, option_defaults=options,
                    destination=dest, target_nick=ssh_nick, remote_dir=remote_text,
                    error=_("Select at least one connection to include its saved passwords "
                            "or private keys."))
                return
            if dest == 'bitwarden':
                dialog.close()
                mirror = mirror_logins_row.get_active() and bool(options.get('secrets'))
                self._export_to_bitwarden(selected, options, mirror_logins=mirror)
                return
            if dest == 'ssh':
                target = selected_ssh_target[0]
                if target is None:
                    reopen(
                        prefill_ids=sel_ids, encrypt_default=encrypt_on, option_defaults=options,
                        destination='ssh', remote_dir=remote_text,
                        error=_("Choose a server to back up to."))
                    return
                remote = remote_text.strip() or "~/sshpilot-backups"
                nick = getattr(target, 'nickname', None)
                if encrypt_on:
                    passphrase = pw_row.get_text() or ''
                    if not passphrase:
                        reopen(
                            prefill_ids=sel_ids, encrypt_default=True, option_defaults=options,
                            destination='ssh', target_nick=nick, remote_dir=remote,
                            error=_("Enter a passphrase, or turn off encryption."))
                        return
                    dialog.close()
                    self._export_to_ssh_server(selected, options, passphrase, target, remote)
                elif options.get('secrets') or options.get('private_keys'):
                    dialog.close()
                    self._confirm_plaintext_then_export(
                        selected, sel_ids, options,
                        on_confirm=lambda: self._export_to_ssh_server(
                            selected, options, None, target, remote),
                        destination='ssh', target_nick=nick, remote_dir=remote)
                else:
                    dialog.close()
                    self._export_to_ssh_server(selected, options, None, target, remote)
                return
            if encrypt_on:
                passphrase = pw_row.get_text() or ''
                if not passphrase:
                    reopen(
                        prefill_ids=sel_ids, encrypt_default=True, option_defaults=options,
                        destination='file',
                        error=_("Enter a passphrase, or turn off encryption."))
                    return
                dialog.close()
                self._choose_export_path(selected, passphrase, options)
            else:
                dialog.close()
                if options.get('secrets') or options.get('private_keys'):
                    self._confirm_plaintext_then_export(selected, sel_ids, options)
                else:
                    self._choose_export_path(selected, None, options)

        continue_btn.connect('clicked', on_continue)
        dialog.present()

    def _alert_private_keys_export_risk(self, parent, *, on_decline=None):
        """Warn before including private key files in a backup."""
        heading = _("Private Key Files")
        body = _("It is not recommended to move your private keys to another machine. "
                 "If you still want to do this, make sure to protect the backup "
                 "with a passphrase.")
        if hasattr(Adw, 'AlertDialog'):
            alert = Adw.AlertDialog(heading=heading, body=body)
        else:
            alert = Adw.MessageDialog(
                transient_for=parent, modal=True, heading=heading, body=body,
            )
        alert.add_response('cancel', _('Cancel'))
        alert.add_response('ok', _('OK'))
        alert.set_default_response('ok')
        alert.set_close_response('cancel')

        def on_response(_dlg, resp):
            if resp != 'ok' and on_decline:
                on_decline()

        alert.connect('response', on_response)
        if hasattr(Adw, 'AlertDialog'):
            alert.present(parent)
        else:
            alert.present()

    def _export_to_bitwarden(self, connections, options, mirror_logins=False):
        """Get Bitwarden ready, then store the backup manifest in a secure note (optionally also
        mirroring saved secrets as Bitwarden login items)."""
        from .bitwarden_backup_setup import ensure_bitwarden_ready

        def after_ready(ready):
            if not ready:
                return

            def do_export(*_a):
                from .backup_manager import BackupManager
                from .backup_backends import BitwardenBackupBackend, BackupTooLargeForNote
                from .secret_storage import get_secret_manager
                from .bitwarden_backup_setup import progress_dialog
                name = _("sshPilot Backup {}").format(datetime.now().strftime('%Y-%m-%d %H:%M'))
                bw = get_secret_manager().get_backend("bitwarden")
                mgr = BackupManager(self.config, self.connection_manager)
                backend = BitwardenBackupBackend(bw, item_name=name)

                cancelled = {'v': False}
                _set_status, close_spinner = progress_dialog(
                    self, _("Export to Bitwarden"),
                    _("Exporting to Bitwarden — this may take a while…"),
                    on_cancel=lambda: cancelled.__setitem__('v', True))

                # Building the manifest reads secrets (may hit bw) and the note write spawns bw —
                # all off the main thread so the UI doesn't freeze ("not responding").
                def worker():
                    try:
                        entry = mgr.export_to_backend(
                            backend, connections=connections, options=options,
                            mirror_to=(bw if mirror_logins else None))
                        counts = getattr(mgr, 'last_export_counts', {})
                        mirror = getattr(mgr, 'last_mirror_counts', None)
                        payload = ('ok', entry.name, counts.get('credentials', 0),
                                   counts.get('private_keys', 0), mirror)
                    except BackupTooLargeForNote as e:
                        payload = ('toobig', str(e))
                    except Exception as e:
                        logger.error("Bitwarden export failed: %s", e)
                        payload = ('error', str(e))
                    GLib.idle_add(lambda: (_report(payload), False)[1])

                def _report(p):
                    if cancelled['v']:
                        return   # user cancelled the wait
                    close_spinner()
                    if p[0] == 'ok':
                        msg = _("Backup saved to Bitwarden as “{}”.\n\n{} credential(s) and {} "
                                "private key(s) included.").format(p[1], p[2], p[3])
                        mirror = p[4]
                        if mirror:
                            msg += "\n\n" + _("{} secret(s) also copied as Bitwarden login "
                                              "items.").format(mirror.get('mirrored', 0))
                        self._simple_dialog(_("Export Successful"), msg)
                    elif p[0] == 'toobig':
                        self._simple_dialog(_("Backup too large for Bitwarden"), p[1])
                    else:
                        self._simple_dialog(_("Export Failed"), p[1])

                threading.Thread(target=worker, daemon=True).start()

            # Reading saved secrets for the manifest may need the CURRENT secrets backend unlocked.
            self._run_after_vault_unlock_for_secrets(
                do_export, needed=bool(options.get('secrets')),
                cancelled_heading=_("Export Cancelled"))

        ensure_bitwarden_ready(self, after_ready)

    def _make_ssh_backup_runner(self, connection):
        """Build an SFTP-manager (used only for its ``run_command``) targeting ``connection``,
        exactly as the authorized-keys editor does. Reuses the shared native-auth path."""
        from .file_manager import create_file_manager_backend
        host_value = (getattr(connection, 'hostname', '') or getattr(connection, 'host', '')
                      or getattr(connection, 'nickname', '') or '')
        username = getattr(connection, 'username', '') or ''
        port_value = getattr(connection, 'port', 22) or 22
        ssh_config = None
        if getattr(self, 'config', None) is not None:
            try:
                ssh_config = self.config.get_ssh_config()
            except Exception:
                ssh_config = None
        initial_password = getattr(connection, 'password', None) or None
        if not initial_password and self.connection_manager is not None:
            try:
                # Alias-migrating lookup (handles legacy host aliases), same as the file manager.
                initial_password = self.connection_manager.get_connection_password(connection)
            except Exception:
                initial_password = None
        return create_file_manager_backend(
            str(host_value), str(username), int(port_value),
            password=initial_password, connection=connection,
            connection_manager=self.connection_manager, ssh_config=ssh_config)

    def _ensure_ssh_backup_password(self, connection) -> bool:
        """For a password-auth connection with no stored secret, prompt (blocking) so the ssh
        transfer has credentials. Key/agent/askpass auth needs nothing here. Returns False only
        if the user cancels the prompt. Must run on the GTK main thread."""
        from .sftp_utils import _is_password_auth_enabled
        if not _is_password_auth_enabled(connection):
            return True
        if getattr(connection, 'password', None):
            return True
        pw = None
        if self.connection_manager is not None:
            try:
                pw = self.connection_manager.get_connection_password(connection)
            except Exception:
                pw = None
        if not pw:
            pw = show_ssh_password_dialog(
                from_widget=self, connection=connection,
                connection_manager=self.connection_manager)
            if pw is None:
                return False   # user cancelled
        connection.password = pw   # resolve_native_auth reads connection.password
        return True

    def _export_to_ssh_server(self, connections, options, passphrase, target, remote_dir):
        """Store the backup manifest as a ``.spbk`` file in ``remote_dir`` on ``target``."""
        from .backup_manager import BackupManager
        from .backup_backends import SSHServerBackupBackend, BackupError
        from .bitwarden_backup_setup import progress_dialog

        def do_export(*_a):
            if not self._ensure_ssh_backup_password(target):
                return   # user cancelled the password prompt
            # Seconds in the name so two exports within the same minute don't clobber each other.
            name = "sshpilot_backup_{}.spbk".format(datetime.now().strftime('%Y%m%d_%H%M%S'))
            try:
                runner = self._make_ssh_backup_runner(target)
            except Exception as e:
                logger.error("SSH backup: could not prepare connection: %s", e)
                self._simple_dialog(_("Export Failed"), str(e))
                return
            mgr = BackupManager(self.config, self.connection_manager)
            backend = SSHServerBackupBackend(runner, remote_dir, item_name=name)
            who = getattr(target, 'nickname', '') or getattr(target, 'hostname', '') or '?'

            cancelled = {'v': False}
            _set_status, close_spinner = progress_dialog(
                self, _("Export to SSH Server"),
                _("Backing up to {} — this may take a while…").format(who),
                on_cancel=lambda: cancelled.__setitem__('v', True))

            def worker():
                try:
                    entry = mgr.export_to_backend(
                        backend, connections=connections, passphrase=passphrase,
                        options=options)
                    counts = getattr(mgr, 'last_export_counts', {})
                    payload = ('ok', entry.id, counts.get('credentials', 0),
                               counts.get('private_keys', 0))
                except BackupError as e:
                    payload = ('error', str(e))
                except Exception as e:
                    logger.error("SSH server export failed: %s", e)
                    payload = ('error', str(e))
                GLib.idle_add(lambda: (_report(payload), False)[1])

            def _report(p):
                if cancelled['v']:
                    return
                close_spinner()
                if p[0] == 'ok':
                    self._simple_dialog(
                        _("Export Successful"),
                        _("Backup saved to {}:{}\n\n{} credential(s) and {} private key(s) "
                          "included; encryption: {}.").format(
                            who, p[1], p[2], p[3], _("on") if passphrase else _("off")))
                else:
                    self._simple_dialog(_("Export Failed"), p[1])

            threading.Thread(target=worker, daemon=True).start()

        self._run_after_vault_unlock_for_secrets(
            do_export, needed=bool(options.get('secrets')),
            cancelled_heading=_("Export Cancelled"))

    def _confirm_plaintext_then_export(self, connections, sel_ids, options,
                                       on_confirm=None, destination='file',
                                       target_nick=None, remote_dir=None):
        sensitive_items = []
        if options.get('secrets'):
            sensitive_items.append(_("saved passwords and passphrases"))
        if options.get('private_keys'):
            sensitive_items.append(_("private keys"))
        sensitive_text = _(" and ").join(sensitive_items) if sensitive_items else _("backup data")
        warn = Adw.MessageDialog(
            transient_for=self, modal=True, heading=_("Export without encryption?"),
            body=_("{} will be written in PLAIN TEXT and readable by anyone with the "
                   "file. Continue?").format(sensitive_text))
        warn.add_response('back', _('Go Back'))
        warn.add_response('plain', _('Export Unencrypted'))
        warn.set_response_appearance('plain', Adw.ResponseAppearance.DESTRUCTIVE)
        warn.set_close_response('back')

        def on_warn(dlg, resp):
            if resp == 'plain':
                if on_confirm is not None:
                    on_confirm()
                else:
                    self._choose_export_path(connections, None, options)
            else:
                GLib.idle_add(lambda: (self._show_export_options_dialog(
                    sel_ids, False, options, destination=destination,
                    target_nick=target_nick, remote_dir=remote_dir), False)[1])
        warn.connect('response', on_warn)
        warn.present()

    def _choose_export_path(self, connections, passphrase, options):
        from .backup_manager import BackupManager
        file_dialog = Gtk.FileDialog()
        file_dialog.set_title(_("Export Backup"))
        file_dialog.set_initial_name(
            f"sshpilot_backup_{datetime.now().strftime('%Y%m%d')}.spbk")
        spbk_filter = Gtk.FileFilter()
        spbk_filter.set_name(_("sshPilot backup (*.spbk)"))
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
            except GLib.Error as e:
                if getattr(e, 'code', None) == 2:
                    logger.info("Export cancelled by user")
                else:
                    logger.error(f"Export failed: {e}")
                    self._simple_dialog(_("Export Failed"), str(e))
                return
            if not file:
                return
            export_path = file.get_path()
            if not export_path.endswith('.spbk'):
                export_path += '.spbk'

            def do_export(*_args):
                backup_mgr = BackupManager(self.config, self.connection_manager)
                success, error = backup_mgr.export_backup(
                    export_path, connections=connections, passphrase=passphrase,
                    options=options)
                if success:
                    counts = getattr(backup_mgr, 'last_export_counts', {})
                    msg = _("Backup saved to:\n{}\n\n{} credential(s) and {} private key(s) "
                            "included; encryption: {}.").format(
                        export_path, counts.get('credentials', 0),
                        counts.get('private_keys', 0),
                        _("on") if passphrase else _("off"))
                    skipped = getattr(backup_mgr, 'last_export_skipped_config_files', []) or []
                    if skipped:
                        msg += "\n\n" + _("{} SSH config file(s) outside your ~/.ssh were not "
                                          "included (system or shared files):\n{}").format(
                            len(skipped), "\n".join(skipped))
                    missing_keys = getattr(backup_mgr, 'last_export_missing_key_files', []) or []
                    if missing_keys:
                        msg += "\n\n" + _("{} referenced key file(s) were missing and not "
                                          "included:\n{}").format(
                            len(missing_keys), "\n".join(missing_keys))
                    self._simple_dialog(_("Export Successful"), msg)
                else:
                    self._simple_dialog(_("Export Failed"), error or _("Unknown error"))

            self._run_after_vault_unlock_for_secrets(
                do_export,
                needed=bool(options.get('secrets')),
                cancelled_heading=_("Export Cancelled"),
            )

        file_dialog.save(self, None, on_save_response)

    def show_import_dialog(self):
        """Ask where to import from (file or Bitwarden), then run that flow."""
        logger.info("Show import source dialog")
        try:
            dialog = Adw.MessageDialog(
                transient_for=self, modal=True, heading=_("Import Configuration"),
                body=_("Import a backup from a file, an SSH server, or your Bitwarden vault."))
            dialog.add_response('cancel', _('Cancel'))
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
            dialog.connect('response', on_source)
            dialog.present()
        except Exception as e:
            logger.error(f"Failed to show import source dialog: {e}")

    def _import_from_file(self):
        """Show the file chooser for a .spbk / .json import."""
        logger.info("Show import configuration dialog")
        try:
            # Create file chooser dialog for opening
            file_dialog = Gtk.FileDialog()
            file_dialog.set_title(_("Import Configuration"))
            
            # Backups (.spbk) and legacy JSON configs.
            filter_backup = Gtk.FileFilter()
            filter_backup.set_name(_("sshPilot backups & configs"))
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
        """Ensure Bitwarden is ready, list sshPilot backups, then restore the chosen one."""
        from .bitwarden_backup_setup import ensure_bitwarden_ready

        def proceed(ready):
            if not ready:
                return
            from .backup_backends import BitwardenBackupBackend
            from .secret_storage import get_secret_manager
            from .bitwarden_backup_setup import progress_dialog
            backend = BitwardenBackupBackend(get_secret_manager().get_backend("bitwarden"))
            cancelled = {'v': False}
            _set_status, close_spinner = progress_dialog(
                self, _("Import from Bitwarden"), _("Loading backups from Bitwarden…"),
                on_cancel=lambda: cancelled.__setitem__('v', True))

            def worker():   # bw list items is slow — keep it off the main thread
                try:
                    payload = ('ok', backend.list_exports())
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
                        _("No sshPilot backups were found in your Bitwarden vault."))
                    return
                self._show_bitwarden_entry_chooser(backend, entries)

            threading.Thread(target=worker, daemon=True).start()

        ensure_bitwarden_ready(self, proceed)

    def _choose_backup_entry(self, entries, *, heading, on_chosen):
        """Radio list of ``BackupEntry`` items; calls ``on_chosen(entry)`` on the main thread
        when the user hits Restore. Shared by the Bitwarden and SSH-server import flows."""
        dialog = Adw.MessageDialog(
            transient_for=self, modal=True, heading=heading,
            body=_("Choose a backup to restore:"))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_start(12); box.set_margin_end(12)
        box.set_margin_top(12); box.set_margin_bottom(12)
        radios = []
        group = None
        for entry in entries:
            label = entry.name + (f"  ({entry.date[:10]})" if entry.date else "")
            rb = Gtk.CheckButton(label=label)
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

    def _show_bitwarden_entry_chooser(self, backend, entries):
        def on_chosen(entry):
            from .bitwarden_backup_setup import progress_dialog
            cancelled = {'v': False}
            _set_status, close_spinner = progress_dialog(
                self, _("Import from Bitwarden"), _("Reading backup from Bitwarden…"),
                on_cancel=lambda: cancelled.__setitem__('v', True))

            def worker():   # bw get item is slow — keep it off the main thread
                try:
                    payload = ('ok', backend.read(entry))
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
                # Reuse the .spbk apply path — a Bitwarden-note manifest has the same shape.
                self._show_import_mode_dialog("", manifest=p[1])

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
        from .backup_backends import SSHServerBackupBackend
        from .bitwarden_backup_setup import progress_dialog
        if not self._ensure_ssh_backup_password(target):
            return   # user cancelled the password prompt
        try:
            runner = self._make_ssh_backup_runner(target)
        except Exception as e:
            self._simple_dialog(_("Import Failed"), str(e))
            return
        backend = SSHServerBackupBackend(runner, remote_dir)
        cancelled = {'v': False}
        _set_status, close_spinner = progress_dialog(
            self, _("Import from SSH Server"), _("Loading backups…"),
            on_cancel=lambda: cancelled.__setitem__('v', True))

        def worker():
            try:
                payload = ('ok', backend.list_exports())
            except Exception as e:
                logger.error("Listing SSH backups failed: %s", e)
                payload = ('error', str(e))
            GLib.idle_add(lambda: (_after(payload), False)[1])

        def _after(p):
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
                    _("No sshPilot backups were found in {} on that server.").format(remote_dir))
                return
            self._choose_backup_entry(
                entries, heading=_("Import from SSH Server"),
                on_chosen=lambda entry: self._ssh_import_download(backend, entry))

        threading.Thread(target=worker, daemon=True).start()

    def _ssh_import_download(self, backend, entry):
        """Download the chosen .spbk to a temp file, then hand off to the normal import path
        (which handles encryption prompt, mode selection, and apply)."""
        import tempfile
        from .bitwarden_backup_setup import progress_dialog
        cancelled = {'v': False}
        _set_status, close_spinner = progress_dialog(
            self, _("Import from SSH Server"), _("Downloading backup…"),
            on_cancel=lambda: cancelled.__setitem__('v', True))

        def worker():
            tmp_path = None
            try:
                fd, tmp_path = tempfile.mkstemp(suffix=".spbk")
                os.close(fd)
                backend.download(entry, tmp_path)
                payload = ('ok', tmp_path)
            except Exception as e:
                logger.error("Downloading SSH backup failed: %s", e)
                self._safe_unlink(tmp_path)   # download failed after mkstemp — drop the empty temp
                payload = ('error', str(e))
            GLib.idle_add(lambda: (_after(payload), False)[1])

        def _after(p):
            if cancelled['v']:
                if p[0] == 'ok':
                    self._safe_unlink(p[1])   # downloaded after cancel — don't leak it
                return
            close_spinner()
            if p[0] != 'ok':
                self._simple_dialog(_("Import Failed"), p[1])
                return
            tmp = p[1]
            # Reuse the .spbk import path — handles encryption prompt + mode dialog + apply —
            # and delete the temp once its manifest has been read (on_cleanup).
            self._import_spbk(tmp, on_cleanup=lambda: self._safe_unlink(tmp))

        threading.Thread(target=worker, daemon=True).start()

    def _begin_import(self, import_path: str):
        """Route an import by format: .spbk (decrypt as needed) vs legacy JSON."""
        try:
            from .backup_archive import is_spbk
            if is_spbk(import_path):
                self._import_spbk(import_path)
            else:
                self._show_import_mode_dialog(import_path)
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
        """Decrypt (if needed) a .spbk, then proceed to the import-mode dialog with its manifest.

        ``on_cleanup`` (optional) fires once the manifest is materialised (or on any terminal
        error) — used to delete a downloaded temp file that's no longer needed."""
        from .backup_archive import read_spbk, spbk_is_encrypted, SpbkError
        try:
            if spbk_is_encrypted(import_path):
                self._prompt_spbk_passphrase(import_path, on_cleanup=on_cleanup)
                return
            manifest = read_spbk(import_path, None)
            if on_cleanup:
                on_cleanup()
            self._show_import_mode_dialog(import_path, manifest=manifest)
        except SpbkError as e:
            if on_cleanup:
                on_cleanup()
            self._simple_dialog(_("Import Failed"), str(e))
        except Exception as e:
            if on_cleanup:
                on_cleanup()
            logger.error(f"Failed to read backup: {e}")
            self._simple_dialog(_("Import Failed"), str(e))

    def _prompt_spbk_passphrase(self, import_path: str, error: Optional[str] = None, on_cleanup=None):
        """Prompt for the backup passphrase; retry on a wrong passphrase.

        ``on_cleanup`` fires on success, on a fatal error, and on cancel — but NOT between
        wrong-passphrase retries (it's forwarded to the retry instead)."""
        from .backup_archive import read_spbk, SpbkPassphraseError, SpbkError
        dialog = Adw.MessageDialog(
            transient_for=self, modal=True, heading=_("Encrypted Backup"),
            body=error or _("Enter the passphrase used when this backup was created."))
        entry = Gtk.PasswordEntry(show_peek_icon=True)
        entry.set_property('activates-default', True)
        entry.set_size_request(BACKUP_DIALOG_MIN_WIDTH, -1)
        dialog.set_extra_child(entry)
        dialog.add_response('cancel', _('Cancel'))
        dialog.add_response('ok', _('Unlock'))
        dialog.set_response_appearance('ok', Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response('ok')
        dialog.set_close_response('cancel')

        def on_response(dlg, resp):
            if resp != 'ok':
                if on_cleanup:
                    on_cleanup()   # user cancelled — drop any downloaded temp
                return
            passphrase = entry.get_text() or ''
            try:
                manifest = read_spbk(import_path, passphrase)
            except SpbkPassphraseError:
                GLib.idle_add(lambda: (self._prompt_spbk_passphrase(
                    import_path, _("Wrong passphrase — try again."),
                    on_cleanup=on_cleanup), False)[1])
                return
            except SpbkError as e:
                if on_cleanup:
                    on_cleanup()
                self._simple_dialog(_("Import Failed"), str(e))
                return
            if on_cleanup:
                on_cleanup()
            self._show_import_mode_dialog(import_path, manifest=manifest)

        dialog.connect('response', on_response)
        dialog.present()
        GLib.idle_add(lambda: (entry.grab_focus(), False)[1])

    def _show_import_mode_dialog(self, import_path: str, manifest=None):
        """Show dialog to select import mode (replace or merge)."""
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
            mode_row.set_selected(0)
            mode_row.set_subtitle(
                _("All current settings will be replaced with the imported configuration."))
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

            restore_checks = {}
            if manifest is not None:
                from .backup_manager import BackupManager, BACKUP_OPTION_KEYS
                backup_mgr = BackupManager(self.config, self.connection_manager)
                all_requested = {key: True for key in BACKUP_OPTION_KEYS}
                included = backup_mgr._restore_options_for_manifest(
                    manifest, restore_options=all_requested)

                restore_group = Adw.PreferencesGroup(title=_("Restore"))
                labels = {
                    'app_settings': _("App settings and groups"),
                    'ssh_config': _("Connection profiles (SSH config)"),
                    'known_hosts': _("Known hosts"),
                    'secrets': _("Saved secrets (passwords and passphrases)"),
                    'private_keys': _("Private key files"),
                }
                for key in BACKUP_OPTION_KEYS:
                    row = Adw.SwitchRow(title=labels[key])
                    row.set_active(included.get(key, False))
                    row.set_sensitive(included.get(key, False))
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

            backup_dir = os.path.join(get_config_dir(), 'backups')
            notice_group = Adw.PreferencesGroup()
            notice_group.set_description(
                _("A backup will be created automatically before importing.\n"
                  "Backup location: {}").format(backup_dir))
            page.add(notice_group)

            def on_import(_btn):
                mode = 'replace' if mode_row.get_selected() == 0 else 'merge'
                if manifest is not None:
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
                            import_path, manifest=manifest), False)[1])
                        return
                    dialog.close()
                    will_replace_ssh = restore_options.get('ssh_config', False)
                    self._guard_default_mode_replace(
                        mode, will_replace_ssh,
                        lambda: self._perform_spbk_import(manifest, mode, restore_options))
                else:
                    # Legacy JSON: we can't see categories up front, so assume ssh_config.
                    dialog.close()
                    self._guard_default_mode_replace(
                        mode, True,
                        lambda: self._perform_import(import_path, mode))

            import_btn.connect('clicked', on_import)
            dialog.present(self)

        except Exception as e:
            logger.error(f"Failed to show import mode dialog: {e}")

    def _guard_default_mode_replace(self, mode: str, will_replace_ssh: bool, proceed):
        """Before a Replace that overwrites the GLOBAL ``~/.ssh/config`` (default mode), make the
        blast radius explicit — that file is shared with ssh/scp/git/rsync and everything else.
        In isolated mode (sshPilot's own config file) or for Merge, proceed without the prompt."""
        isolated = bool(getattr(self.connection_manager, 'isolated_mode', False))
        if mode != 'replace' or not will_replace_ssh or isolated:
            proceed()
            return
        ssh_path = getattr(self.connection_manager, 'ssh_config_path', '') \
            or os.path.expanduser('~/.ssh/config')
        warn = Adw.MessageDialog(
            transient_for=self, modal=True, heading=_("Replace your global SSH config?"),
            body=_("Replace mode will overwrite {} entirely. That file is shared with ssh, "
                   "scp, git and every other SSH tool on this machine — any Host blocks, "
                   "Include and Match rules not managed by sshPilot will be lost.\n\n"
                   "Choose Merge instead to add hosts without overwriting, or continue to "
                   "replace. A backup is saved first either way.").format(ssh_path))
        warn.add_response('cancel', _('Cancel'))
        warn.add_response('replace', _('Replace Anyway'))
        warn.set_response_appearance('replace', Adw.ResponseAppearance.DESTRUCTIVE)
        warn.set_close_response('cancel')

        def on_warn(dlg, resp):
            if resp == 'replace':
                proceed()
        warn.connect('response', on_warn)
        warn.present()

    def _run_after_vault_unlock_for_secrets(self, proceed, *, needed: bool,
                                            cancelled_heading: str):
        """Run ``proceed()`` once the session vault is unlocked when secrets are involved.

        If the vault is locked, prompt to unlock first. When unlock fails or the user cancels,
        show ``cancelled_heading`` and do **not** call ``proceed()`` — export/import must not
        silently continue with zero credentials restored or included."""
        if not needed:
            proceed()
            return
        try:
            from .secret_storage import get_secret_manager
            if not get_secret_manager().selected_needs_unlock():
                proceed()
                return
            from .secret_unlock_dialog import prompt_unlock

            def _on_done(unlocked):
                if unlocked:
                    proceed()
                    return
                self._simple_dialog(
                    cancelled_heading,
                    _("Your secret vault must be unlocked to include or restore saved "
                      "credentials. Unlock it and try again."))

            prompt_unlock(self, on_done=_on_done)
        except Exception:
            # If we cannot even verify the vault, abort rather than silently proceed with a
            # backup/restore that would be missing credentials.
            logger.warning("Pre-operation vault unlock check failed; aborting", exc_info=True)
            self._simple_dialog(
                cancelled_heading,
                _("Could not verify the secret vault, so the operation was cancelled to avoid "
                  "leaving saved credentials out. Try again."))

    def _perform_spbk_import(self, manifest, mode: str, restore_options=None):
        """Apply a decrypted .spbk manifest: config (replace/merge) + restore credentials.

        If the manifest carries credentials and the selected secret backend is a **locked
        session vault** (e.g. Bitwarden), unlock it first — import is aborted if unlock fails
        or the user cancels, so credentials are never silently skipped.  Applying a manifest
        may invoke the selected secret backend once per credential, so keep that work off the
        GTK main thread and show progress until the result is ready."""
        restore_options = restore_options or {}
        total = (
            len([c for c in (manifest.get('credentials') or [])
                 if c.get('secret') is not None])
            if restore_options.get('secrets', True) else 0
        )
        total_keys = (
            len(manifest.get('private_keys') or [])
            if restore_options.get('private_keys', False) else 0
        )

        def do_apply(*_args):
            from .bitwarden_backup_setup import progress_dialog

            _set_status, close_spinner = progress_dialog(
                self, _("Import Configuration"),
                _("Applying backup — this may take a while…"))

            def worker():
                try:
                    from .backup_manager import BackupManager
                    backup_mgr = BackupManager(self.config, self.connection_manager)
                    success, error, restored, restored_keys = (
                        backup_mgr.apply_imported_manifest(
                            manifest, mode=mode, create_backup=True,
                            restore_options=restore_options)
                    )
                    payload = (
                        'ok', success, error, restored, restored_keys,
                        getattr(backup_mgr, 'last_import_skipped_keys', 0),
                        getattr(backup_mgr, 'last_import_secrets_persisted', True),
                        getattr(backup_mgr, 'last_import_skipped_credentials', 0),
                        getattr(backup_mgr, 'last_merge_collisions', []) or [],
                        getattr(backup_mgr, 'last_merge_dropped_globals', 0),
                    )
                except Exception as e:
                    logger.error("Backup import failed: %s", e)
                    payload = ('error', str(e))
                GLib.idle_add(lambda: (_report(payload), False)[1])

            def _report(payload):
                close_spinner()
                if payload[0] == 'error':
                    self._simple_dialog(_("Import Failed"), payload[1])
                    return
                (_, success, error, restored, restored_keys, skipped_keys,
                 secrets_persisted, skipped_creds, merge_collisions,
                 dropped_globals) = payload
                if not success:
                    self._simple_dialog(
                        _("Import Failed"), error or _("Unknown error"))
                    return
                self._show_import_success(
                    restored, total, restored_keys, total_keys,
                    skipped_keys, secrets_persisted, skipped_creds,
                    merge_collisions=merge_collisions,
                    dropped_globals=dropped_globals)

            threading.Thread(target=worker, daemon=True).start()

        self._run_after_vault_unlock_for_secrets(
            do_apply,
            needed=total > 0,
            cancelled_heading=_("Import Cancelled"),
        )

    def _show_import_success(self, restored: int, total: int,
                             restored_keys: int = 0, total_keys: int = 0,
                             skipped_keys: int = 0, secrets_persisted: bool = True,
                             skipped_credentials: int = 0, merge_collisions=None,
                             dropped_globals: int = 0):
        # Keys that already existed were left untouched by design (never overwritten), so they
        # are NOT counted as failures. Genuine key failures are the remainder.
        failed_keys = max(0, total_keys - restored_keys - skipped_keys)
        # Secrets that already existed were likewise left untouched — not a failure either.
        failed_creds = max(0, total - restored - skipped_credentials)
        lines = []
        if total == 0 and total_keys == 0:
            lines.append(_("Backup imported successfully."))
        else:
            all_ok = (failed_creds == 0) and (failed_keys == 0)
            lines.append(_("Backup imported successfully.") if all_ok
                         else _("Backup imported, with some items skipped."))
            done = []
            if restored:
                done.append(_("{} credential(s)").format(restored))
            if restored_keys:
                done.append(_("{} private key(s)").format(restored_keys))
            if done:
                lines.append(_("Restored {}.").format(_(", ").join(done)))
            if skipped_credentials:
                lines.append(_("{} credential(s) already existed and were left untouched — "
                               "sshPilot never overwrites a saved secret.").format(
                                   skipped_credentials))
            if skipped_keys:
                lines.append(_("{} private key(s) already existed and were left untouched — "
                               "sshPilot never overwrites a private key.").format(skipped_keys))
            if failed_creds:
                if not secrets_persisted:
                    lines.append(_("{} credential(s) were not stored because the selected secret "
                                   "backend does not save secrets (“agent”). Choose a "
                                   "storage backend in Preferences ▸ Security & Credentials, "
                                   "then import again.").format(failed_creds))
                else:
                    lines.append(_("{} of {} credential(s) could not be restored — the selected "
                                   "secret backend may be locked or unavailable.").format(
                                       failed_creds, total))
            if failed_keys:
                lines.append(_("{} private key(s) could not be written (the target path may "
                               "not be writable).").format(failed_keys))
        if merge_collisions:
            names = ", ".join(" ".join(p) for p in merge_collisions)
            lines.append(_("{} imported host(s) shared a name with an existing host; the "
                           "conflicting names were left as-is: {}.").format(
                               len(merge_collisions), names))
        if dropped_globals:
            lines.append(_("{} global rule(s) from the backup (Host * / Match blocks) were not "
                           "imported — they affect every connection, so they are not merged "
                           "automatically.").format(dropped_globals))
        lines.append(_("Reload now to apply the imported configuration. Some settings may still "
                       "need a full restart of sshPilot to take effect."))
        body = "\n\n".join(lines)

        success_dialog = Adw.MessageDialog(
            transient_for=self, modal=True, heading=_("Import Successful"), body=body)
        success_dialog.add_response('ok', _('OK'))
        success_dialog.add_response('restart', _('Reload Now'))
        success_dialog.set_response_appearance('restart', Adw.ResponseAppearance.SUGGESTED)

        def on_success_response(dialog, response):
            if response == 'restart':
                try:
                    self.config.config_data = self.config.load_json_config()
                    if self.connection_manager:
                        self.connection_manager.load_ssh_config()
                    if self.group_manager:
                        self.group_manager._load_groups()
                    self.rebuild_connection_list()
                    self.toast_overlay.add_toast(Adw.Toast.new(_("Configuration reloaded")))
                except Exception as e:
                    logger.error(f"Failed to reload configuration: {e}")
            dialog.destroy()

        success_dialog.connect('response', on_success_response)
        success_dialog.present()

    def _perform_import(self, import_path: str, mode: str):
        """Perform the actual import operation"""
        try:
            from .backup_manager import BackupManager
            
            backup_mgr = BackupManager(self.config, self.connection_manager)
            success, error = backup_mgr.import_configuration(import_path, mode=mode, create_backup=True)
            
            if success:
                # Show success dialog with restart suggestion
                body = _("Configuration imported successfully.")
                ignored = getattr(backup_mgr, 'last_import_ignored_secrets', 0)
                if ignored:
                    body += "\n\n" + _("{} saved password(s)/key(s) in this .json file were not "
                                       "imported — legacy JSON backups can't restore secrets. "
                                       "Use an encrypted .spbk backup to include them.").format(
                                           ignored)
                body += "\n\n" + _("Reload now to apply it. Some settings may still need a full "
                                   "restart of sshPilot to take effect.")
                success_dialog = Adw.MessageDialog(
                    transient_for=self,
                    modal=True,
                    heading=_("Import Successful"),
                    body=body,
                )
                success_dialog.add_response('ok', _('OK'))
                success_dialog.add_response('restart', _('Reload Now'))
                success_dialog.set_response_appearance('restart', Adw.ResponseAppearance.SUGGESTED)
                
                def on_success_response(dialog, response):
                    if response == 'restart':
                        # Reload the connection list and config
                        try:
                            self.config.config_data = self.config.load_json_config()
                            if self.connection_manager:
                                self.connection_manager.load_ssh_config()
                            # Reload group manager to pick up imported groups and colors
                            if self.group_manager:
                                self.group_manager._load_groups()
                            self.rebuild_connection_list()
                            
                            # Show confirmation
                            self.toast_overlay.add_toast(Adw.Toast.new(_("Configuration reloaded")))
                        except Exception as e:
                            logger.error(f"Failed to reload configuration: {e}")
                    dialog.destroy()
                
                success_dialog.connect('response', on_success_response)
                success_dialog.present()
            else:
                # Show error dialog
                error_dialog = Adw.MessageDialog(
                    transient_for=self,
                    modal=True,
                    heading=_("Import Failed"),
                    body=_("Failed to import configuration:\n{}").format(error or "Unknown error")
                )
                error_dialog.add_response('ok', _('OK'))
                error_dialog.present()
                
        except Exception as e:
            logger.error(f"Import failed: {e}")
            error_dialog = Adw.MessageDialog(
                transient_for=self,
                modal=True,
                heading=_("Import Failed"),
                body=_("An error occurred during import:\n{}").format(str(e))
            )
            error_dialog.add_response('ok', _('OK'))
            error_dialog.present()

    # --- Group management dialogs (create / edit / rename tag / assign) -----

    def _group_form_dialog(self, *, title, label, confirm_label, on_confirm,
                           initial_name='', placeholder='', select_text=False,
                           with_color=True, initial_color=None):
        """Small Adw.Dialog form: name entry plus an optional color row.

        ``on_confirm(name, color)`` runs on the confirm button; return True to
        close the dialog, False to keep it open (e.g. validation failed).
        ``color`` is an RGBA string, or None when unset/cleared.
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

        def on_confirm_clicked(_button):
            name = entry.get_text().strip()
            color = None
            if color_button is not None:
                rgba_value = color_button.get_rgba()
                if color_selected and rgba_value.alpha > 0:
                    color = rgba_value.to_string()
            if on_confirm(name, color):
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
        """Handle create group action"""
        try:
            def create(name, color):
                if not name:
                    self._simple_dialog(_("Error"), _("Please enter a group name."))
                    return False
                self.group_manager.create_group(name, color=color)
                self.rebuild_connection_list()
                return True

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

            def save(name, color):
                if not name:
                    self._simple_dialog(_("Error"), _("Please enter a group name."))
                    return False
                self.group_manager.rename_group(group_id, name)
                self.group_manager.set_group_color(group_id, color)
                self.rebuild_connection_list()
                return True

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

            def save(name, _color):
                if not name:
                    self._simple_dialog(_("Error"), _("Please enter a tag name."))
                    return False
                if ',' in name:
                    # Tags are entered comma-separated in the connection
                    # dialog, so a comma in a tag name could not round-trip.
                    self._simple_dialog(_("Error"), _("Tag names cannot contain commas."))
                    return False
                if name != old_name:
                    from .tag_groups import migrate_expanded_state
                    self.config.rename_tag(old_name, name)
                    try:
                        state = self.config.get_setting('ui.tag_groups_expanded', {}) or {}
                        self.config.set_setting(
                            'ui.tag_groups_expanded',
                            migrate_expanded_state(state, old_key, name.casefold()),
                        )
                    except Exception:
                        logger.debug("Failed to migrate tag expansion state", exc_info=True)
                    self.rebuild_connection_list()
                return True

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

            def assign(nickname: str, target_group_id) -> None:
                if is_copy:
                    if target_group_id:
                        self.group_manager.copy_connection_to_group(nickname, target_group_id)
                else:
                    self.group_manager.move_connection(nickname, target_group_id)

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
                selected_row_ref = row
                selected_group_id = row.group_id
                row._check.set_visible(True)
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
                    row.add_suffix(check)
                    row._check = check
                    row.group_id = group['id']
                    row.connect('activated', select_group_row)
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

            def perform_move() -> bool:
                group_name = create_group_entry.get_text().strip()
                if group_name:
                    existing_group_id = find_existing_group_id(group_name)
                    if existing_group_id:
                        for nickname in connection_nicknames:
                            assign(nickname, existing_group_id)
                        self.rebuild_connection_list()
                        return True
                    try:
                        selected_color = None
                        rgba_value = color_button.get_rgba()
                        if color_selected and rgba_value.alpha > 0:
                            selected_color = rgba_value.to_string()
                        new_group_id = self.group_manager.create_group(group_name, color=selected_color)
                        for nickname in connection_nicknames:
                            assign(nickname, new_group_id)
                        self.rebuild_connection_list()
                        return True
                    except ValueError as e:
                        show_group_exists_error(str(e))
                        create_group_entry.set_text("")
                        reset_color_selection()
                        create_group_entry.grab_focus()
                        update_confirm_state()
                        return False

                if selected_group_id is not None:
                    for nickname in connection_nicknames:
                        assign(nickname, selected_group_id)
                    self.rebuild_connection_list()
                    return True
                return False

            def on_confirm(*_args):
                if has_valid_target() and perform_move():
                    dialog.close()

            confirm_button.connect('clicked', on_confirm)
            create_group_entry.connect('entry-activated', lambda _e: on_confirm())

            dialog.present(self)

        except Exception as e:
            logger.error(f"Failed to show move to group dialog: {e}")
