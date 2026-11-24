"""What overrides a connection's own settings, shown in the connection editor.

Two things can decide an option before the connection's own Host block does.

**Settings ▸ SSH Options.**

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

**An earlier block of the SSH configuration.** OpenSSH keeps the first value
it obtains, so a ``Host *`` or ``Match`` block placed before the host's own
block decides every option it sets. The app launches exactly ``ssh <alias>``
and does not repeat the Host block on the command line, so that is also what
a session uses. The daemon reports those lines
(``EffectiveConfigComparison.earlier``); a field that sets something else is
marked with the value that is used and where it comes from. The value stays
editable and is saved: the file is the user's, and the app never moves
blocks around in it.
"""

from __future__ import annotations

import logging
import re
import threading
import types
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
    ("key_only_row", "IdentitiesOnly"),
    ("add_keys_to_agent_row", "AddKeysToAgent"),
    ("identity_agent_row", "IdentityAgent"),
    ("exit_on_forward_failure_row", "ExitOnForwardFailure"),
    ("port_forwarding_only_row", "SessionType"),
    # The authentication section: the order, and each method's switch.
    ("auth_order_row", "PreferredAuthentications"),
    ("auth_gssapi_row", "GSSAPIAuthentication"),
    ("auth_hostbased_row", "HostbasedAuthentication"),
    ("auth_publickey_row", "PubkeyAuthentication"),
    ("auth_kbdinteractive_row", "KbdInteractiveAuthentication"),
    ("auth_password_row", "PasswordAuthentication"),
)

#: Rows of the authentication section. Each reports this host's own value
#: through ``row._sshpilot_own_value()`` and, when the block sets nothing,
#: already shows the value OpenSSH resolves -- so, like a text row, it needs
#: no mark of its own when an earlier block supplies that value.
AUTH_ROWS = frozenset({
    "auth_order_row", "auth_gssapi_row", "auth_hostbased_row",
    "auth_publickey_row", "auth_kbdinteractive_row", "auth_password_row",
})

#: Rows that are a text field. Left empty, such a row shows the value OpenSSH
#: resolves, dimmed (see ``_load_inherited_values_async``), so it needs no
#: mark of its own when an earlier block supplies that value.
TEXT_ROWS = frozenset({
    "hostname_row", "username_row", "port_row", "proxy_jump_row",
    "proxy_command_row", "pkcs11_provider_row", "security_key_provider_row",
    "local_command_row", "remote_command_row",
})

#: What a switch that is off stands for. A switch cannot look "inherited":
#: off reads as this value, so an earlier block that sets another one has to
#: be pointed out even though the connection sets nothing itself.
SWITCH_OFF_VALUE = {
    "x11_row": "no",
    "exit_on_forward_failure_row": "no",
    "port_forwarding_only_row": "default",
}

#: A switch that is off, on a host whose block spells the option out anyway
#: (``ForwardX11 no``): the directive and the value the block then holds.
SWITCH_OFF_AUTHORED = {
    "x11_row": ("forwardx11", "no"),
}


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


_YES_NO_LIKE = frozenset({"yes", "no", "ask", "confirm", "none"})


def _normalized(keyword: str, value: str) -> str:
    """Two spellings of one value compare equal, as they do to OpenSSH.

    The daemon's ``normalized_directive_value`` applies the same rule to the
    upgrade notice; the frontend keeps its own copy of this pure helper.
    """
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
        "connection. The value here is saved, but the app does not use it. "
        "To change it, close this dialog and open Settings ▸ SSH Options."
    ).format(keyword=forced_keyword, value=value)


def earlier_map(comparison) -> Dict[str, object]:
    """``EffectiveConfigComparison.earlier`` as lower-case keyword → entry."""
    found: Dict[str, object] = {}
    for entry in getattr(comparison, "earlier", ()) or ():
        key = str(getattr(entry, "key", "") or "").lower()
        if key:
            found.setdefault(key, entry)
    return found


def _display_path(path: str) -> str:
    import os

    home = os.path.expanduser("~")
    if home and (path == home or path.startswith(home + os.sep)):
        return "~" + path[len(home):]
    return path


def earlier_note(keyword: str, own: Optional[str], earlier: Dict[str, object]) -> Optional[str]:
    """The note for a field an earlier block of the configuration beats.

    None when no earlier block sets *keyword*, when the field sets nothing
    (*own* empty: the earlier value is simply the inherited one), or when
    both hold the same value.
    """
    entry = earlier.get(keyword.strip().lower())
    if entry is None or own is None or not str(own).strip():
        return None
    value = str(getattr(entry, "value", "") or "")
    if _normalized(keyword, own) == _normalized(keyword, value):
        return None
    block = str(getattr(entry, "block", "") or "").strip()
    if block:
        template = _(
            "Not used: “{block}” in {file} (line {line}) already sets "
            "{keyword} to “{value}”, and OpenSSH keeps the first value it "
            "finds. The value here is saved, but connections use “{value}”."
        )
    else:
        template = _(
            "Not used: line {line} of {file}, above every Host block, already "
            "sets {keyword} to “{value}”, and OpenSSH keeps the first value "
            "it finds. The value here is saved, but connections use “{value}”."
        )
    return template.format(
        block=block,
        file=_display_path(str(getattr(entry, "file", "") or "")),
        line=int(getattr(entry, "line", 0) or 0),
        keyword=keyword,
        value=value,
    )


def earlier_decides_note(keyword: str, entry) -> str:
    """The note for a field that sets nothing while an earlier block does."""
    block = str(getattr(entry, "block", "") or "").strip()
    if block:
        template = _(
            "“{block}” in {file} (line {line}) sets {keyword} to “{value}” "
            "for this host, so connections use that value."
        )
    else:
        template = _(
            "Line {line} of {file}, above every Host block, sets {keyword} "
            "to “{value}” for this host, so connections use that value."
        )
    return template.format(
        block=block,
        file=_display_path(str(getattr(entry, "file", "") or "")),
        line=int(getattr(entry, "line", 0) or 0),
        keyword=keyword,
        value=str(getattr(entry, "value", "") or ""),
    )


def earlier_row_state(
    row_name: str,
    keyword: str,
    own: Optional[str],
    earlier: Dict[str, object],
) -> Tuple[Optional[str], Optional[str]]:
    """``(state, note)`` for one dialog row against the earlier blocks.

    ``OVERRIDDEN`` when the row sets a value an earlier block beats;
    ``GLOBAL`` when the row sets nothing, cannot show an inherited value
    (a switch, a selector) and an earlier block decides something other than
    what it displays; ``(None, None)`` otherwise.
    """
    entry = earlier.get(keyword.lower())
    if own is not None and str(own).strip():
        note = earlier_note(keyword, own, earlier)
        if note:
            return OVERRIDDEN, note
        if row_name == "local_command_row":
            # The block writes ``PermitLocalCommand yes`` next to the command.
            note = earlier_note("PermitLocalCommand", "yes", earlier)
            if note:
                return OVERRIDDEN, note
        return None, None
    if entry is None or row_name in TEXT_ROWS or row_name in AUTH_ROWS:
        return None, None
    shown = SWITCH_OFF_VALUE.get(row_name)
    value = str(getattr(entry, "value", "") or "")
    if shown is not None and _normalized(keyword, value) == shown:
        return None, None
    return GLOBAL, earlier_decides_note(keyword, entry)


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
        "The value here is saved, but the app does not use it. To change it, "
        "close this dialog and open Settings ▸ SSH Options."
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
        self._earlier_directives = {}
        self._override_fetch_serial = 0
        self._load_forced_options_async()
        self._load_earlier_directives_async()

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

    def _load_earlier_directives_async(self) -> None:
        """Ask which options an earlier block decides for this host.

        A saved host is asked about by id: what precedes its own block. A
        new connection has no block yet; it is asked about by the alias in
        the form, since its block will be appended after everything else.
        The alias is watched, as the answer depends on which ``Host``
        patterns it matches.
        """
        parent = getattr(self, "parent_window", None)
        client = getattr(parent, "client", None)
        fallback_hook = getattr(self, "_auth_policy_set_fallback", None)
        # The same moments call for OpenSSH's answer about the pending block.
        refresh = getattr(self, "_schedule_effective_refresh", None)
        if callable(refresh):
            refresh()

        def builtin_fallback() -> None:
            # Nothing can say what the host inherits (yet): the
            # authentication section starts from OpenSSH's built-in order,
            # so it stays editable. A later answer replaces it.
            if callable(fallback_hook):
                fallback_hook({})

        if not (
            callable(getattr(parent, "_daemon_ready", None))
            and parent._daemon_ready()
        ):
            builtin_fallback()
            return
        connection = getattr(self, "connection", None)
        editing = connection is not None and getattr(self, "is_editing", False)
        alias = ""
        if editing:
            if not callable(getattr(client, "get_effective_config", None)):
                builtin_fallback()
                return
        else:
            if not callable(getattr(client, "get_earlier_directives", None)):
                builtin_fallback()
                return
            self._watch_alias_for_earlier_directives()
            row = getattr(self, "nickname_row", None)
            alias = row.get_text().strip() if row is not None else ""
        self._earlier_fetch_serial = serial = (
            getattr(self, "_earlier_fetch_serial", 0) + 1
        )
        if not editing and not alias:
            # No alias yet, so no block can be matched.
            builtin_fallback()
            self._set_earlier_directives({})
            return

        auth_host = (
            str(getattr(connection, "nickname", "") or "") if editing else alias
        )

        def apply(earlier, inherited_auth):
            if serial != getattr(self, "_earlier_fetch_serial", 0):
                return False
            if callable(fallback_hook):
                # None: the lookup failed. Shown as unknown, not as a default.
                fallback_hook(inherited_auth)
            if earlier is not None:
                self._set_earlier_directives(earlier)
            return False

        def worker():
            earlier = None
            # A daemon without the lookup: nothing known to be inherited.
            inherited_auth = None if (
                auth_host and callable(getattr(client, "get_inherited_auth", None))
            ) else {}
            # What the authentication section shows for a value reset after
            # loading, or for a host not saved yet: the lines it would
            # inherit without its own.
            if auth_host and callable(getattr(client, "get_inherited_auth", None)):
                try:
                    inherited_auth = {
                        str(d.key).lower(): str(d.value)
                        for d in client.get_inherited_auth(auth_host)
                    }
                except Exception:
                    logger.debug("Inherited authentication unavailable", exc_info=True)
            try:
                if editing:
                    from .api.connection_identity import connection_id_for

                    result = client.get_effective_config(connection_id_for(connection))
                    # The earlier lines come from the files, so they are
                    # reported even when OpenSSH rejects the configuration
                    # and the comparison itself is unavailable.
                    if getattr(result, "available", False) or getattr(
                        result, "earlier", None
                    ):
                        earlier = earlier_map(result)
                else:
                    earlier = earlier_map(
                        types.SimpleNamespace(
                            earlier=client.get_earlier_directives(alias)
                        )
                    )
            except Exception:
                logger.debug("Earlier SSH directives unavailable", exc_info=True)
            GLib.idle_add(apply, earlier, inherited_auth)

        threading.Thread(target=worker, daemon=True).start()

    def _watch_alias_for_earlier_directives(self) -> None:
        """Ask again, shortly after the alias of a new connection changes."""
        if getattr(self, "_earlier_alias_watched", False):
            return
        row = getattr(self, "nickname_row", None)
        if row is None:
            return
        self._earlier_alias_watched = True

        def fire():
            self._earlier_alias_timer = 0
            if not getattr(self, "is_editing", False):
                self._load_earlier_directives_async()
            return False

        def on_changed(*_args):
            if getattr(self, "is_editing", False):
                return
            pending = getattr(self, "_earlier_alias_timer", 0)
            if pending:
                GLib.source_remove(pending)
            self._earlier_alias_timer = GLib.timeout_add(350, fire)

        try:
            row.connect("changed", on_changed)
        except Exception:
            logger.debug("Cannot watch the alias row", exc_info=True)

    # -- presenting -------------------------------------------------------

    def _set_forced_options(self, forced: Dict[str, str]) -> None:
        self._forced_options = dict(forced or {})
        hook = getattr(self, "_auth_policy_set_forced", None)
        if callable(hook):
            hook(self._forced_options)
        self._apply_override_notes()

    def _set_earlier_directives(self, earlier: Dict[str, object]) -> None:
        self._earlier_directives = dict(earlier or {})
        hook = getattr(self, "_auth_policy_set_earlier", None)
        if callable(hook):
            hook(self._earlier_directives)
        self._apply_override_notes()

    def _apply_override_notes(self, *_args) -> None:
        """Bring every marker, the Advanced tab and the banner up to date."""
        forced = getattr(self, "_forced_options", None) or {}
        earlier = getattr(self, "_earlier_directives", None) or {}
        if forced or earlier:
            self._watch_rows_for_overrides()
        for row_name, keyword in OVERRIDABLE_ROWS:
            row = getattr(self, row_name, None)
            if row is None:
                continue
            try:
                own = self._override_row_value(row_name, row)
                state, note = override_state(keyword, own, forced)
                if state is None:
                    # Settings come first on the command line; only when they
                    # leave the option alone does the file's order decide.
                    state, note = earlier_row_state(row_name, keyword, own, earlier)
                self._mark_row_override(row_name, row, state, note)
            except Exception:
                logger.debug("Could not mark %s as overridden", row_name, exc_info=True)
        self._note_auth_preview()
        option_list = getattr(getattr(self, "advanced_tab", None), "option_list", None)
        refresh = getattr(option_list, "refresh_notes", None)
        if callable(refresh):
            try:
                refresh()
            except Exception:
                logger.debug("Could not refresh option notes", exc_info=True)
        self._sync_override_banner()

    def _note_auth_preview(self) -> None:
        """Give the "OpenSSH will try" line the reasons it differs.

        The method rows carry their marks inside a folded list; the line
        below it is what shows while the list is closed, so it repeats the
        notes of the authentication rows that are set here and not used.
        """
        section = getattr(self, "auth_policy", None)
        set_notes = getattr(section, "set_preview_notes", None)
        if not callable(set_notes):
            return
        notes = []
        for row_name, _keyword in OVERRIDABLE_ROWS:
            if row_name not in AUTH_ROWS:
                continue
            row = getattr(self, row_name, None)
            if getattr(row, "_sshpilot_override_state", None) != OVERRIDDEN:
                continue
            note = getattr(row, "_sshpilot_override_note", "") or ""
            if note:
                # Each note is about its own row ("Not used: ..."): away
                # from the row it needs the row's name to say what is not.
                notes.append((row.get_title(), note))
        try:
            set_notes(notes)
        except Exception:
            logger.debug("Could not note the authentication preview", exc_info=True)

    def _watch_rows_for_overrides(self) -> None:
        """Re-mark as the user edits: the state depends on what a row holds."""
        if getattr(self, "_override_watchers_connected", False):
            return
        self._override_watchers_connected = True
        # The authentication section re-marks through its own change callback.
        names = [
            name for name, _keyword in OVERRIDABLE_ROWS if name not in AUTH_ROWS
        ]
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
        own_value = getattr(row, "_sshpilot_own_value", None)
        if callable(own_value):
            return own_value()
        if row_name == "port_forwarding_only_row":
            return "none" if row.get_active() else None
        if row_name in SWITCH_OFF_AUTHORED:
            if row.get_active():
                return "yes"
            # Off, but the saved block spells the option out: that line is
            # kept on save, and an earlier block can beat it like any other.
            directive, off_value = SWITCH_OFF_AUTHORED[row_name]
            authored = getattr(
                getattr(self, "connection", None), "authored_directives", None
            ) or ()
            if getattr(self, "is_editing", False) and directive in authored:
                return off_value
            return None
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
                "dialog-error-symbolic" if overridden else "dialog-information-symbolic"
            )
            # The informational marker is the only sign that another block
            # decides the value, so it takes the warning colour, not a dim one.
            # Error stays reserved for a value set here and never used.
            for css, wanted in (("error", overridden), ("warning", not overridden)):
                if wanted:
                    icon.add_css_class(css)
                else:
                    icon.remove_css_class(css)
            icon.set_visible(bool(state))
            icon.set_tooltip_text(note or None)
        row._sshpilot_override_state = state
        # A value that is never used is drawn like a refused one (Adwaita's
        # error style), though it is still saved.
        self._set_row_override_error(row, state == OVERRIDDEN)
        self._set_row_override_note(row, note or "")
        # "Edit to set it for this host" is false while the option is forced:
        # the override note replaces it, and it returns with the override gone.
        if row_name in (getattr(self, "_inherited_row_handlers", None) or {}):
            self._set_row_inherited_note(
                row, "" if note else inherited_note_text()
            )

    def _advanced_option_note(
        self, keyword: str, value: str
    ) -> Optional[Tuple[str, bool]]:
        """``(note, unused)`` for an Advanced-tab option, or None.

        *unused* is False only for an option Settings add to rather than
        replace: both values are used there.
        """
        forced = getattr(self, "_forced_options", None) or {}
        lowered = keyword.strip().lower()
        if lowered in forced:
            note = advanced_option_note(keyword, value, forced)
            unused = lowered not in EXTRA_OPTIONS_ADDED_NOT_REPLACED
        else:
            note = earlier_note(
                keyword, value, getattr(self, "_earlier_directives", None) or {}
            )
            unused = True
        return (note, unused) if note else None

    def _overridden_field_counts(self) -> Tuple[int, int]:
        """``(by Settings, by an earlier block)``: fields set here and not used."""
        forced = getattr(self, "_forced_options", None) or {}
        earlier = getattr(self, "_earlier_directives", None) or {}
        by_settings = by_earlier = 0
        if not forced and not earlier:
            return 0, 0
        # Only what this connection sets and loses: a field that simply
        # takes the global value is not a conflict.
        for row_name, keyword in OVERRIDABLE_ROWS:
            row = getattr(self, row_name, None)
            if getattr(row, "_sshpilot_override_state", None) != OVERRIDDEN:
                continue
            if suppressing_option(keyword, forced) is not None:
                by_settings += 1
            else:
                by_earlier += 1
        tab = getattr(self, "advanced_tab", None)
        entries = tab.get_config_entries() if tab is not None else ()
        for keyword, value in entries:
            lowered = keyword.strip().lower()
            if lowered in forced:
                if lowered in EXTRA_OPTIONS_ADDED_NOT_REPLACED:
                    continue
                if advanced_option_note(keyword, value, forced):
                    by_settings += 1
            elif earlier_note(keyword, value, earlier):
                by_earlier += 1
        return by_settings, by_earlier

    def _overridden_field_count(self) -> int:
        return sum(self._overridden_field_counts())

    # -- the SSH configuration button -------------------------------------
    #
    # One way from this dialog into the configuration as a whole: Advanced
    # Configuration, at this host's block. It shows what the two buttons
    # that stood here did (what SSH uses for the host, and the file's
    # text), and the blocks that decide it besides.

    def _setup_config_buttons(self) -> None:
        """Wire "Advanced Configuration" at the dialog's foot."""
        advanced = getattr(self, "advanced_config_button", None)
        if advanced is not None:
            # A sidebar row (connection_dialog.blp), not a button.
            advanced.connect("activated", self._on_advanced_config_clicked)
        self._sync_config_buttons()

    def _sync_config_buttons(self) -> None:
        """The button is about SSH: a plugin protocol has no SSH configuration."""
        try:
            ssh = self._selected_protocol_id() == "ssh"
        except Exception:
            ssh = True
        advanced = getattr(self, "advanced_config_button", None)
        if advanced is not None:
            advanced.set_visible(ssh)

    def _on_advanced_config_clicked(self, _button) -> None:
        """Open Advanced Configuration above this dialog, at the host's own
        block when there is one, and re-mark the fields after each save
        there: what was typed here stays, and this dialog still saves only
        what it changed."""
        parent = getattr(self, "parent_window", None)
        opener = getattr(parent, "open_advanced_config", None)
        if not callable(opener):
            return
        connection = getattr(self, "connection", None)
        editing = bool(getattr(self, "is_editing", False)) and connection is not None
        opener(
            host=(getattr(connection, "nickname", "") or "") if editing else "",
            file=(getattr(connection, "source", "") or "") if editing else "",
            over=self,
            on_saved=self._on_ssh_config_saved,
        )

    def _on_ssh_config_saved(self) -> None:
        """The file changed under this dialog: ask again what decides each field.

        Only the marks and the inherited values are refreshed. What the user
        typed stays, and a save still writes only the fields changed here.
        """
        names = ["_load_earlier_directives_async"]
        if getattr(self, "is_editing", False):
            # A saved host shows inherited values dimmed in its empty fields.
            # (A search-box target fills them as its own values instead,
            # once, when the dialog opens; that is not repeated here.)
            names.append("_load_inherited_values_async")
        for name in names:
            reload = getattr(self, name, None)
            if callable(reload):
                try:
                    reload()
                except Exception:
                    logger.debug("Could not refresh after an SSH config save", exc_info=True)

    def _sync_override_banner(self) -> None:
        self._sync_config_buttons()
        try:
            ssh = self._selected_protocol_id() == "ssh"
        except Exception:
            ssh = True
        by_settings, by_earlier = self._overridden_field_counts() if ssh else (0, 0)
        show = by_settings + by_earlier > 0
        banner = getattr(self, "_override_banner", None)
        if banner is None:
            if not show:
                return
            banner = Adw.Banner()
            try:
                banner.set_use_markup(False)
            except Exception:
                pass
            # Solid error colours (style.css): a muted grey banner was easy to
            # miss above the form.
            banner.add_css_class("error")
            self._override_banner = banner
            try:
                self.content_mount.prepend(banner)
            except Exception:
                logger.debug("Could not mount the override banner", exc_info=True)
        if show:
            # No button: this dialog is modal, so a Settings window opened
            # from it could not be used until the dialog is closed.
            if by_settings and by_earlier:
                title = _(
                    "Settings ▸ SSH Options and an earlier block of your SSH "
                    "configuration override some settings of this "
                    "connection. They are marked below."
                )
            elif by_earlier:
                title = _(
                    "An earlier block of your SSH configuration overrides "
                    "some settings of this connection. They are marked below."
                )
            else:
                title = _(
                    "Settings ▸ SSH Options override some settings of this "
                    "connection. They are marked below."
                )
            banner.set_title(title)
        banner.set_revealed(show)
        # The marked fields may be on pages other than the one shown.
        sync_marks = getattr(self, "_sync_sidebar_override_marks", None)
        if callable(sync_marks):
            sync_marks()
