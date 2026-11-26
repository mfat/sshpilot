"""What decides a connection's settings, shown in the connection editor.

The editor asks OpenSSH one question per edit
(``connection_dialog_effective``): what the host would use if the form were
saved as it stands. The answer (:class:`~sshpilot.connection_dialog_answer.EditorAnswer`)
says, for every option, the value in force, where it comes from, and how
each line of the host's own block fares. This mixin puts that on the
fields, as Advanced Configuration puts it on its lines:

* a value set here that an earlier block decides first, or that Settings ▸
  SSH Options override, is struck through, tinted, and tagged "No effect"
  or "Overridden", with what OpenSSH uses instead and where it comes from;
* a line OpenSSH rejects is red and tagged "Rejected";
* a field that sets nothing names the block that decides its value.

The value stays editable and is saved: plain ``ssh`` outside the app reads
it from the Host block, the file is the user's, and the app never moves
blocks around in it.
"""

from __future__ import annotations

import logging
import re
from gettext import gettext as _
from typing import Optional, Tuple

try:
    from gi.repository import Adw, GLib, Gtk
except (ImportError, AttributeError, ValueError):  # pragma: no cover - no GTK
    Adw = GLib = Gtk = None  # type: ignore[assignment]

from .core.settings import EXTRA_OPTIONS_ADDED_NOT_REPLACED

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

def inherited_note_text() -> str:
    """The note on a row that shows a value the Host block did not author."""
    return _("Inherited from your SSH configuration. Edit to set it for this host.")


#: The row is set here and the value is not used (no effect, or Settings
#: override it): the sidebar and the banner count it.
OVERRIDDEN = "overridden"
#: The row sets nothing and another block decides what it stands for.
GLOBAL = "global"
#: OpenSSH rejects the row's line.
REJECTED = "rejected"


_YES_NO_LIKE = frozenset({"yes", "no", "ask", "confirm", "none"})


def _normalized(keyword: str, value: str) -> str:
    """Two spellings of one value compare equal, as they do to OpenSSH."""
    value = str(value).strip()
    if keyword.lower() == "proxyjump":
        return ",".join(part for part in re.split(r"[\s,]+", value) if part)
    if keyword.lower() == "hostname" or value.lower() in _YES_NO_LIKE:
        return value.lower()
    return value


def row_mark(
    row_name: str, keyword: str, answer, *, empty: bool = False
) -> Tuple[Optional[str], str, str, str]:
    """``(kind, tag, note, style)`` for one editor row from OpenSSH's answer.

    *kind* is :data:`OVERRIDDEN`, :data:`REJECTED`, :data:`GLOBAL` or None;
    *tag* the short text beside the row (Advanced Configuration's), *note*
    what OpenSSH uses instead and from where, *style* the tag's colour
    (Advanced Configuration's for the same state). *empty*: a text row
    the user left empty, which shows the value it gets in its tag.
    """
    from .connection_dialog_answer import NO_EFFECT, OVERRIDDEN as SETTINGS, REJECTED as BAD
    from .gtk import advanced_config_messages as messages
    from .api.models.connections import SshConfigDraftLineState as State

    if answer is None:
        return None, "", "", ""
    state, note = answer.line_state(keyword)
    if row_name == "local_command_row" and state in (None, "used"):
        # The block writes ``PermitLocalCommand yes`` next to the command,
        # and an earlier ``PermitLocalCommand no`` beats it.
        extra, extra_note = answer.line_state("PermitLocalCommand")
        if extra in (NO_EFFECT, SETTINGS, BAD):
            # Translators: {keyword} is an SSH option name, {note} what
            # OpenSSH does with it, e.g. "PermitLocalCommand: OpenSSH uses no
            # from line 4 (Host *)".
            state, note = extra, _("{keyword}: {note}").format(
                keyword="PermitLocalCommand", note=extra_note
            )
    if state == BAD:
        return REJECTED, _("Rejected"), note, "error"
    if state == NO_EFFECT:
        return OVERRIDDEN, _("No effect"), note, messages.STATE_STYLES[State.SHADOWED]
    if state == SETTINGS:
        return (
            OVERRIDDEN, messages.state_label(State.FORCED), note,
            messages.STATE_STYLES[State.FORCED],
        )
    if state is not None or not answer.available:
        return None, "", "", ""
    # The row sets nothing. Text rows and the authentication section show
    # the inherited value themselves; a switch or a selector shows a state
    # of its own, so the block that decides another one is named.
    used = answer.used(keyword)
    # An entry row cannot show a placeholder (libadwaita draws it over the
    # title), so an empty text row's tag says what it gets.
    if empty and used and row_name in TEXT_ROWS:
        # Translators: an empty field and what OpenSSH uses for it, e.g.
        # "Not set (22, OpenSSH default)" or "Not set (alice, from Host *)".
        tag = _("Not set ({value}, {source})").format(
            value=used, source=answer.source_text(keyword)
        )
        return GLOBAL, tag, answer.inherited_note(keyword), "muted"
    if not used or answer.is_default(keyword) or row_name in AUTH_ROWS:
        return None, "", "", ""
    shown = SWITCH_OFF_VALUE.get(row_name)
    if shown is not None and _normalized(keyword, used) == shown:
        return None, "", "", ""
    return GLOBAL, answer.source_text(keyword), answer.inherited_note(keyword), "muted"


def added_by_settings_note(keyword: str, answer) -> Optional[str]:
    """For an Extra option Settings ▸ SSH Options add to rather than
    replace: both values are used, which is worth saying."""
    if answer is None or keyword.strip().lower() not in EXTRA_OPTIONS_ADDED_NOT_REPLACED:
        return None
    value = answer.forced.get(keyword.strip().lower())
    if value is None:
        return None
    return _(
        "Settings ▸ SSH Options also add “{value}” to every connection. "
        "Both are used."
    ).format(value=value)


class ConnectionDialogOverridesMixin:
    """Marks the editor's fields from OpenSSH's answer for the edit."""

    # -- starting ---------------------------------------------------------

    def _start_override_awareness(self) -> None:
        """Watch the fields and ask OpenSSH about the form as loaded.

        Every change asks again (``_schedule_effective_refresh``): what a
        field holds decides which lines the block would have.
        """
        self._editor_answer = None
        self._watch_rows_for_overrides()
        parent = getattr(self, "parent_window", None)
        if not (
            callable(getattr(parent, "_daemon_ready", None))
            and parent._daemon_ready()
        ):
            # Nothing can say what the host inherits (yet): the
            # authentication section starts from OpenSSH's built-in order,
            # so it stays editable. A later answer replaces it.
            fallback = getattr(self, "_auth_policy_set_fallback", None)
            if callable(fallback):
                fallback({})
        refresh = getattr(self, "_schedule_effective_refresh", None)
        if callable(refresh):
            refresh(0)

    def _on_override_row_changed(self, *_args) -> None:
        self._apply_override_notes()
        refresh = getattr(self, "_schedule_effective_refresh", None)
        if callable(refresh):
            refresh()

    def _watch_rows_for_overrides(self) -> None:
        """Ask again as the user edits: the lines depend on what a row holds."""
        if getattr(self, "_override_watchers_connected", False):
            return
        self._override_watchers_connected = True
        # The authentication section asks through its own change callback.
        names = [
            name for name, _keyword in OVERRIDABLE_ROWS if name not in AUTH_ROWS
        ]
        names += [
            "forward_agent_value_row", "identity_agent_value_row",
            "nickname_row", "certificate_row",
        ]
        for name in dict.fromkeys(names):
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
                row.connect(signal, self._on_override_row_changed)
            except Exception:
                logger.debug("Cannot watch %s", name, exc_info=True)

    # -- presenting -------------------------------------------------------

    def _apply_override_notes(self, *_args) -> None:
        """Bring every mark, the Extra page and the banner up to date."""
        answer = getattr(self, "_editor_answer", None)
        for row_name, keyword in OVERRIDABLE_ROWS:
            row = getattr(self, row_name, None)
            if row is None:
                continue
            try:
                empty = row_name in TEXT_ROWS and not row.get_text().strip()
                kind, tag, note, style = row_mark(
                    row_name, keyword, answer, empty=empty
                )
                self._mark_row_override(row_name, row, kind, tag, note, style)
            except Exception:
                logger.debug("Could not mark %s", row_name, exc_info=True)
        self._note_auth_preview()
        tab = getattr(self, "advanced_tab", None)
        option_list = getattr(tab, "option_list", None)
        refresh = getattr(option_list, "refresh_notes", None)
        if callable(refresh):
            try:
                refresh()
            except Exception:
                logger.debug("Could not refresh option notes", exc_info=True)
        inherited = getattr(tab, "show_inherited", None)
        if callable(inherited):
            try:
                inherited(answer)
            except Exception:
                logger.debug("Could not list inherited options", exc_info=True)
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
                notes.append((row.get_title(), note))
        try:
            set_notes(notes)
        except Exception:
            logger.debug("Could not note the authentication preview", exc_info=True)

    def _mark_row_override(
        self, row_name: str, row, kind: Optional[str], tag: str, note: str,
        style: str = "muted",
    ) -> None:
        """Advanced Configuration's marks on one row: a tag beside it, the
        value struck through and tinted while it is not used, red while
        OpenSSH rejects it."""
        from .advanced_config_widgets import (
            ensure_shared_css,
            pill,
            set_pill,
            struck_attributes,
        )

        label = getattr(row, "_sshpilot_override_pill", None)
        if kind and label is None:
            ensure_shared_css()
            label = pill()
            row.add_suffix(label)
            row._sshpilot_override_pill = label
        if label is not None:
            set_pill(label, tag if kind else "", style or "muted", note)
        unused = kind == OVERRIDDEN
        row._sshpilot_override_state = kind
        for css, wanted in (("warning", unused), ("advcfg-unused", unused)):
            try:
                if wanted:
                    row.add_css_class(css)
                else:
                    row.remove_css_class(css)
            except Exception:
                pass
        # GtkText ignores CSS text-decoration: an entry's text is struck
        # with a Pango attribute.
        delegate = getattr(row, "get_delegate", None)
        text = delegate() if callable(delegate) else None
        if text is not None and getattr(text, "_sshpilot_struck", False) != unused:
            text._sshpilot_struck = unused
            try:
                text.set_attributes(struck_attributes(unused))
            except Exception:
                pass
        self._set_row_override_error(row, kind == REJECTED)
        self._set_row_override_note(row, note or "")
        # "Edit to set it for this host" is false while Settings force the
        # option; the note replaces it, and it returns with the override gone.
        if row_name in (getattr(self, "_inherited_row_handlers", None) or {}):
            self._set_row_inherited_note(
                row, note if kind else inherited_note_text()
            )

    def _advanced_option_note(
        self, keyword: str, value: str
    ) -> Optional[Tuple[str, bool]]:
        """``(note, unused)`` for an Extra Options row, or None.

        *unused* is False only for an option Settings add to rather than
        replace: both values are used there.
        """
        from .connection_dialog_answer import NO_EFFECT, OVERRIDDEN as SETTINGS, REJECTED as BAD

        answer = getattr(self, "_editor_answer", None)
        added = added_by_settings_note(keyword, answer)
        if added:
            return added, False
        if answer is None:
            return None
        state, note = answer.line_state_of(keyword, value)
        if state in (NO_EFFECT, SETTINGS, BAD) and note:
            return note, True
        return None

    def _overridden_field_counts(self) -> Tuple[int, int]:
        """``(by Settings, by an earlier block)``: lines set here, not used."""
        answer = getattr(self, "_editor_answer", None)
        if answer is None:
            return 0, 0
        return answer.unused_count()

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
        """The file changed under this dialog: ask OpenSSH again.

        Only the marks and the inherited values are refreshed. What the user
        typed stays, and a save still writes only the fields changed here.
        """
        refresh = getattr(self, "_schedule_effective_refresh", None)
        if callable(refresh):
            refresh(0)

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
