"""What Settings ▸ SSH Options override, shown in the connection editor.

Everything on that Settings page is sent ahead of a connection's own options,
and OpenSSH keeps the first value it obtains. A field in this dialog can
therefore hold a value the app never uses. The dialog says so, next to the
field, and never does more than that: the value stays editable and is still
saved, because plain ``ssh`` outside the app reads it from the Host block and
it is back in force as soon as the override is removed.

The daemon is the only source of *what* is forced
(``GlobalSshOverrides.forced_options``): it alone knows the legacy-algorithm
and multiplexing options, and it composes the list the way a launch does.
This mixin only presents it.
"""

from __future__ import annotations

import logging
import re
import threading
from gettext import gettext as _
from typing import Dict, Optional, Tuple

try:
    from gi.repository import Adw, GLib, Gtk
except (ImportError, AttributeError, ValueError):  # pragma: no cover - no GTK
    Adw = GLib = Gtk = None  # type: ignore[assignment]

from .core.settings import EXTRA_OPTIONS_ADDED_NOT_REPLACED, forced_options

logger = logging.getLogger(__name__)

#: Dialog rows whose value is one OpenSSH option: (row attribute, keyword).
#: IdentityFile, CertificateFile and the forwards are absent on purpose:
#: OpenSSH collects those, so an override adds to them and nothing is lost.
OVERRIDABLE_ROWS = (
    ("hostname_row", "HostName"),
    ("username_row", "User"),
    ("port_row", "Port"),
    ("proxy_jump_row", "ProxyJump"),
    ("proxy_command_row", "ProxyCommand"),
    ("pkcs11_provider_row", "PKCS11Provider"),
    ("security_key_provider_row", "SecurityKeyProvider"),
    ("local_command_row", "LocalCommand"),
    ("remote_command_row", "RemoteCommand"),
    ("forward_agent_row", "ForwardAgent"),
    ("x11_row", "ForwardX11"),
    ("pubkey_auth_row", "PubkeyAuthentication"),
    ("key_only_row", "IdentitiesOnly"),
    ("add_keys_to_agent_row", "AddKeysToAgent"),
    ("identity_agent_row", "IdentityAgent"),
    ("exit_on_forward_failure_row", "ExitOnForwardFailure"),
)


def inherited_note_text() -> str:
    """The note on a row that shows a value the Host block did not author."""
    return _("Inherited from your SSH configuration. Edit to set it for this host.")


def forced_map(snapshot) -> Dict[str, str]:
    """``GlobalSshOverrides.forced_options`` as lower-case keyword → value."""
    args = []
    for option in getattr(snapshot, "forced_options", ()) or ():
        args.extend(["-o", str(option)])
    return forced_options(args)


#: The row is set here, and Settings win: the value is not used.
OVERRIDDEN = "overridden"
#: The row is empty, or holds what Settings force: the global value applies.
GLOBAL = "global"

_YES_NO_LIKE = frozenset({"yes", "no", "ask", "confirm", "none"})


def suppressing_option(
    keyword: str, forced: Dict[str, str]
) -> Optional[Tuple[str, str]]:
    """``(keyword, value)`` of the forced option that decides *keyword*.

    Usually the keyword itself. ``ProxyJump`` and ``ProxyCommand`` compete
    for one slot: whichever OpenSSH obtains first wins, and the forced ones
    always come before the connection's own. *forced* keeps Settings order
    (see :func:`forced_options`), so the winner is decided here, once, for
    both fields.
    """
    wanted = keyword.lower()
    if wanted in _PROXY_KEYWORDS:
        return _forced_proxy(wanted, forced)
    if wanted in forced:
        return keyword, forced[wanted]
    return None


_PROXY_KEYWORDS = frozenset({"proxyjump", "proxycommand"})


def _forced_proxy(wanted: str, forced: Dict[str, str]) -> Optional[Tuple[str, str]]:
    """The forced proxy option that decides the *wanted* proxy field.

    Walks the forced options in order, as OpenSSH reads them. A
    ``ProxyCommand`` (even ``none``) takes the slot. A ``ProxyJump`` takes it
    too, except ``ProxyJump none``: that only stops later jump hosts, and a
    later ``ProxyCommand`` still applies (``ssh -G`` confirms both).
    """
    jump_none = None
    for name, value in forced.items():
        if name == "proxycommand":
            return "ProxyCommand", value
        if name == "proxyjump":
            if value.strip().lower() != "none":
                return "ProxyJump", value
            jump_none = value
    if wanted == "proxyjump" and jump_none is not None:
        return "ProxyJump", jump_none
    return None


def _normalized(keyword: str, value: str) -> str:
    value = str(value).strip()
    if keyword.lower() == "proxyjump":
        return ",".join(part for part in re.split(r"[\s,]+", value) if part)
    if keyword.lower() == "hostname" or value.lower() in _YES_NO_LIKE:
        return value.lower()
    return value


def override_state(
    keyword: str, own: Optional[str], forced: Dict[str, str]
) -> Tuple[Optional[str], Optional[str]]:
    """``(state, note)`` for a dialog field, or ``(None, None)``.

    *own* is what this connection sets for *keyword*, or None when it sets
    nothing (an empty row, or a value shown only because it is inherited).
    """
    suppressing = suppressing_option(keyword, forced)
    if suppressing is None:
        return None, None
    forced_keyword, value = suppressing
    same_option = forced_keyword.lower() == keyword.lower()
    if own is None or not str(own).strip():
        if not same_option:
            # An empty jump-hosts row loses nothing to a forced ProxyCommand.
            return None, None
        return GLOBAL, _global_note(forced_keyword, value)
    if same_option and _normalized(keyword, own) == _normalized(keyword, value):
        return GLOBAL, _global_note(forced_keyword, value)
    return OVERRIDDEN, _(
        "Settings ▸ SSH Options set {keyword} to “{value}” for every "
        "connection. The value here is saved, but the app does not use it."
    ).format(keyword=forced_keyword, value=value)


def _global_note(keyword: str, value: str) -> str:
    return _(
        "Settings ▸ SSH Options set {keyword} to “{value}” for every "
        "connection, so this connection uses that value."
    ).format(keyword=keyword, value=value)


def advanced_option_note(
    keyword: str, value: str, forced: Dict[str, str]
) -> Optional[str]:
    """What to say under an Advanced-tab option, or None.

    Nothing when Settings force the very value the row holds: the row then
    loses nothing.
    """
    forced_value = forced.get(keyword.strip().lower())
    if forced_value is None:
        return None
    if keyword.strip().lower() in EXTRA_OPTIONS_ADDED_NOT_REPLACED:
        return _(
            "Settings ▸ SSH Options also add “{value}” to every connection. "
            "Both are used."
        ).format(value=forced_value)
    if forced_value.strip().lower() == value.strip().lower():
        return None
    return _(
        "Settings ▸ SSH Options set this to “{value}” for every connection. "
        "The value here is saved, but the app does not use it."
    ).format(value=forced_value)


class ConnectionDialogOverridesMixin:
    """Marks the dialog fields that Settings ▸ SSH Options override."""

    # -- fetching ---------------------------------------------------------

    def _start_override_awareness(self) -> None:
        """Load what is forced, once.

        The dialog is modal, so Settings cannot be changed while it is open
        and one answer holds for its lifetime.
        """
        self._forced_options = {}
        self._override_fetch_serial = 0
        self._load_forced_options_async()

    def _load_forced_options_async(self) -> None:
        parent = getattr(self, "parent_window", None)
        client = getattr(parent, "client", None)
        if not (
            callable(getattr(parent, "_daemon_ready", None))
            and parent._daemon_ready()
            and callable(getattr(client, "get_global_ssh_overrides", None))
        ):
            return
        self._override_fetch_serial = serial = (
            getattr(self, "_override_fetch_serial", 0) + 1
        )

        def apply(forced):
            # A later request supersedes this one; a daemon that cannot answer
            # (older build, capability missing) leaves the last answer alone.
            if forced is None or serial != getattr(self, "_override_fetch_serial", 0):
                return False
            self._set_forced_options(forced)
            return False

        def worker():
            forced = None
            try:
                forced = forced_map(client.get_global_ssh_overrides())
            except Exception:
                logger.debug("Global SSH overrides unavailable", exc_info=True)
            GLib.idle_add(apply, forced)

        threading.Thread(target=worker, daemon=True).start()

    # -- presenting -------------------------------------------------------

    def _set_forced_options(self, forced: Dict[str, str]) -> None:
        self._forced_options = dict(forced or {})
        self._apply_override_notes()

    def _apply_override_notes(self, *_args) -> None:
        """Bring every marker, the Advanced tab and the banner up to date."""
        forced = getattr(self, "_forced_options", None) or {}
        if forced:
            self._watch_rows_for_overrides()
        for row_name, keyword in OVERRIDABLE_ROWS:
            row = getattr(self, row_name, None)
            if row is None:
                continue
            try:
                state, note = override_state(
                    keyword, self._override_row_value(row_name, row), forced
                )
                self._mark_row_override(row_name, row, state, note)
            except Exception:
                logger.debug("Could not mark %s as overridden", row_name, exc_info=True)
        option_list = getattr(getattr(self, "advanced_tab", None), "option_list", None)
        refresh = getattr(option_list, "refresh_notes", None)
        if callable(refresh):
            try:
                refresh()
            except Exception:
                logger.debug("Could not refresh option notes", exc_info=True)
        self._sync_override_banner()

    def _watch_rows_for_overrides(self) -> None:
        """Re-mark as the user edits: the state depends on what a row holds."""
        if getattr(self, "_override_watchers_connected", False):
            return
        self._override_watchers_connected = True
        names = [name for name, _keyword in OVERRIDABLE_ROWS]
        names += ["forward_agent_value_row", "identity_agent_value_row"]
        for name in names:
            row = getattr(self, name, None)
            if row is None:
                continue
            # By type: a "notify::" on a property the row lacks connects
            # without complaint and never fires.
            if hasattr(row, "get_active"):
                signal = "notify::active"
            elif hasattr(row, "get_selected"):
                signal = "notify::selected"
            else:
                signal = "changed"
            try:
                row.connect(signal, self._apply_override_notes)
            except Exception:
                logger.debug("Cannot watch %s", name, exc_info=True)

    def _override_row_value(self, row_name: str, row) -> Optional[str]:
        """What this connection itself sets through *row*, or None.

        A row showing an inherited value sets nothing: the value comes from
        elsewhere. Switches set their option only when on, because that is
        when the Host block gets the line.
        """
        if row_name in (getattr(self, "_inherited_row_handlers", None) or {}):
            return None
        if row_name == "x11_row":
            return "yes" if row.get_active() else None
        if row_name == "key_only_row":
            return "yes" if row.get_active() and row.get_sensitive() else None
        if row_name == "pubkey_auth_row":
            return "no" if row.get_active() else None
        if row_name == "exit_on_forward_failure_row":
            return "yes" if row.get_active() else None
        if row_name == "forward_agent_row":
            from .connection_dialog import forward_agent_text_from_fields

            return forward_agent_text_from_fields(
                **self._selected_forward_agent_fields()
            ) or None
        if row_name == "add_keys_to_agent_row":
            return self._selected_add_keys_to_agent() or None
        if row_name == "identity_agent_row":
            return self._selected_identity_agent() or None
        text = row.get_text().strip()
        if row_name == "port_row" and text == "22":
            # Port 22 is written only when the block authored it.
            authored = getattr(getattr(self, "connection", None), "authored_directives", None)
            adopted = getattr(self, "_adopted_inherited_fields", None) or ()
            if "port" not in (authored or ()) and "port" not in adopted:
                return None
        return text or None

    def _mark_row_override(
        self, row_name: str, row, state: Optional[str], note: Optional[str]
    ) -> None:
        icon = getattr(row, "_sshpilot_override_icon", None)
        if state and icon is None:
            icon = Gtk.Image()
            icon.set_valign(Gtk.Align.CENTER)
            row.add_suffix(icon)
            row._sshpilot_override_icon = icon
        if icon is not None:
            overridden = state == OVERRIDDEN
            icon.set_from_icon_name(
                "dialog-warning-symbolic" if overridden else "dialog-information-symbolic"
            )
            for css, wanted in (("warning", overridden), ("dim-label", not overridden)):
                if wanted:
                    icon.add_css_class(css)
                else:
                    icon.remove_css_class(css)
            icon.set_visible(bool(state))
            icon.set_tooltip_text(note or None)
        row._sshpilot_override_state = state
        self._set_row_override_note(row, note or "")
        # "Edit to set it for this host" is false while the option is forced:
        # the override note replaces it, and it returns with the override gone.
        if row_name in (getattr(self, "_inherited_row_handlers", None) or {}):
            self._set_row_inherited_note(
                row, "" if note else inherited_note_text()
            )

    def _advanced_option_note(self, keyword: str, value: str) -> Optional[str]:
        return advanced_option_note(
            keyword, value, getattr(self, "_forced_options", None) or {}
        )

    def _overridden_field_count(self) -> int:
        forced = getattr(self, "_forced_options", None) or {}
        if not forced:
            return 0
        # Only what this connection sets and loses: a field that simply
        # takes the global value is not a conflict.
        count = sum(
            1
            for row_name, _keyword in OVERRIDABLE_ROWS
            if getattr(getattr(self, row_name, None), "_sshpilot_override_state", None)
            == OVERRIDDEN
        )
        tab = getattr(self, "advanced_tab", None)
        entries = tab.get_config_entries() if tab is not None else ()
        for keyword, value in entries:
            if keyword.strip().lower() in EXTRA_OPTIONS_ADDED_NOT_REPLACED:
                continue
            if advanced_option_note(keyword, value, forced):
                count += 1
        return count

    def _sync_override_banner(self) -> None:
        try:
            ssh = self._selected_protocol_id() == "ssh"
        except Exception:
            ssh = True
        show = ssh and self._overridden_field_count() > 0
        banner = getattr(self, "_override_banner", None)
        if banner is None:
            if not show:
                return
            banner = Adw.Banner()
            try:
                banner.set_use_markup(False)
            except Exception:
                pass
            # No button: this dialog is modal, so a Settings window opened
            # from it could not be used until the dialog is closed.
            banner.set_title(
                _("Settings ▸ SSH Options override some settings of this "
                  "connection. They are marked below.")
            )
            # Solid error colours (style.css): a muted grey banner was easy to
            # miss above the form.
            banner.add_css_class("error")
            self._override_banner = banner
            try:
                self.content_mount.prepend(banner)
            except Exception:
                logger.debug("Could not mount the override banner", exc_info=True)
        banner.set_revealed(show)
