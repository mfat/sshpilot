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
from dataclasses import replace
import re
from gettext import gettext as _
from typing import Optional, Tuple

try:
    from gi.repository import Adw, GLib, Gtk
except (ImportError, AttributeError, ValueError):  # pragma: no cover - no GTK
    Adw = GLib = Gtk = None  # type: ignore[assignment]

from .core.settings import EXTRA_OPTIONS_ADDED_NOT_REPLACED
from .ssh_config_answer import NOTHING, LineVerdict

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
    # Multiplexing: Share connection is the choice ControlMaster makes; the
    # rows under it set the socket path and how long the master stays up.
    ("multiplex_row", "ControlMaster"),
    ("multiplex_path_row", "ControlPath"),
    ("multiplex_persist_row", "ControlPersist"),
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

def inherited_note_text(source_note: str = "") -> str:
    """The note on a row that shows a value the Host block did not author;
    *source_note* says where it comes from (``EditorAnswer.inherited_note``)."""
    if source_note:
        return "\n".join((source_note, _("Edit to set it for this host.")))
    return _("Inherited from your SSH configuration. Edit to set it for this host.")


#: The row is set here and the value is not used (no effect, or Settings
#: override it): the sidebar and the banner count it.
OVERRIDDEN = "overridden"
#: The row sets nothing and another block decides what it stands for.
GLOBAL = "global"
#: OpenSSH rejects the row's line.
REJECTED = "rejected"
#: The row is not an SSH option, and what it holds goes unused because of
#: one (the saved password, with no password login tried): marked like a
#: value with no effect, but not counted, since the option's row is.
UNUSED = "unused"

#: Besides its own switch, what decides whether a method is tried.
_ORDER_KEYWORD = "PreferredAuthentications"


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
) -> Tuple[Optional[str], "LineVerdict"]:
    """``(kind, verdict)`` for one editor row from OpenSSH's answer.

    *kind* is :data:`OVERRIDDEN`, :data:`REJECTED`, :data:`GLOBAL` or None.
    For a line the row writes, *verdict* is the line's
    :class:`~sshpilot.ssh_config_answer.LineVerdict`, the one Advanced
    Configuration shows for that line (tag, colour, tooltip, note, link).
    For a row that sets nothing, it names where the value comes from.
    *empty*: a text row the user left empty, whose tag says what it gets.
    """
    if answer is None:
        return None, NOTHING
    verdict = answer.line_verdict(keyword)
    if row_name == "local_command_row" and not verdict.tag:
        # The block writes ``PermitLocalCommand yes`` next to the command,
        # and an earlier ``PermitLocalCommand no`` beats it.
        extra = answer.line_verdict("PermitLocalCommand")
        if extra.tag:
            # Translators: {keyword} is an SSH option name, {note} what
            # OpenSSH does with it, e.g. "PermitLocalCommand: OpenSSH uses no
            # from line 4 (Host *)".
            verdict = replace(extra, note=_("{keyword}: {note}").format(
                keyword="PermitLocalCommand", note=extra.note
            ))
    if verdict.state == REJECTED:  # the row's kind is named as the line's state
        return REJECTED, verdict
    if verdict.unused:
        return OVERRIDDEN, verdict
    if verdict.state is not None or not answer.available:
        return None, NOTHING
    # The row sets nothing. Text rows and the authentication section show
    # the inherited value themselves; a switch or a selector shows a state
    # of its own, so the block that decides another one is named.
    used = answer.used(keyword)
    note = answer.inherited_note(keyword)
    # An entry row cannot show a placeholder (libadwaita draws it over the
    # title), so an empty text row's tag says what it gets.
    if empty and used and row_name in TEXT_ROWS:
        # Translators: an empty field and what OpenSSH uses for it, e.g.
        # "Not set (22, OpenSSH default)" or "Not set (alice, from Host *)".
        tag = _("Not set ({value}, {source})").format(
            value=used, source=answer.source_text(keyword)
        )
        return GLOBAL, LineVerdict(tag=tag, tooltip=note, note=note)
    if not used or answer.is_default(keyword) or row_name in AUTH_ROWS:
        return None, NOTHING
    shown = SWITCH_OFF_VALUE.get(row_name)
    if shown is not None and _normalized(keyword, used) == shown:
        return None, NOTHING
    return GLOBAL, LineVerdict(tag=answer.source_text(keyword), tooltip=note, note=note)


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
        password = getattr(self, "password_row", None)
        if password is not None:
            # Not an SSH option: only its own mark depends on it.
            try:
                password.connect("changed", lambda *_a: self._mark_unused_password())
            except Exception:
                logger.debug("Cannot watch the password", exc_info=True)
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
                kind, verdict = row_mark(row_name, keyword, answer, empty=empty)
                self._mark_row_override(row_name, row, kind, verdict)
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
        """Tell the authentication section what decides each method a row
        does not control, in Advanced Configuration's words, and give the
        "OpenSSH will try" line (shown while the list is closed) the same
        reasons. Then mark the saved password when it cannot be asked for.
        """
        section = getattr(self, "auth_policy", None)
        answer = getattr(self, "_editor_answer", None)
        set_reasons = getattr(section, "set_reasons", None)
        if callable(set_reasons):
            from .connection_dialog_auth import METHOD_KEYWORDS

            reasons = {}
            if answer is not None and answer.available:
                order = self._deciding_note(answer, _ORDER_KEYWORD)
                for method, keyword in METHOD_KEYWORDS:
                    reasons[method] = {
                        "switch": self._deciding_note(answer, keyword),
                        "order": order,
                    }
            try:
                set_reasons(reasons)
                section.set_preview_notes(section.overruled_notes())
            except Exception:
                logger.debug("Could not note the authentication section", exc_info=True)
        self._mark_unused_password(section)

    @staticmethod
    def _deciding_note(answer, keyword: str) -> str:
        """What OpenSSH uses for *keyword* and from where: the note on the
        host's own line when that line goes unused, else on the value it
        inherits. Empty when the host's own line is the one used."""
        verdict = answer.line_verdict(keyword)
        if verdict.unused:
            return verdict.note
        if verdict.state is not None:
            return ""
        return answer.inherited_note(keyword)

    def _mark_unused_password(self, section=None) -> None:
        """"No effect" on the saved password while OpenSSH tries neither
        password nor keyboard-interactive login, which are what ask for it."""
        row = getattr(self, "password_row", None)
        if row is None:
            return
        section = section or getattr(self, "auth_policy", None)
        policy = getattr(section, "policy", None)
        will_try = None
        try:
            will_try = policy.will_try() if policy is not None else None
        except Exception:
            logger.debug("Could not read the login order", exc_info=True)
        unused = (
            will_try is not None
            and bool(row.get_text())
            and not {"password", "keyboard-interactive"} & set(will_try)
        )
        if unused == (getattr(row, "_sshpilot_override_state", None) == UNUSED):
            return
        from .gtk import advanced_config_messages as messages
        from .api.models.connections import SshConfigDraftLineState as State

        try:
            if unused:
                note = _(
                    "OpenSSH tries neither password nor keyboard-interactive "
                    "login for this host, so it never asks for this password."
                )
                self._mark_row_override("password_row", row, UNUSED, LineVerdict(
                    tag=_("No effect"), style=messages.STATE_STYLES[State.SHADOWED],
                    tooltip=note, note=note,
                ))
            else:
                self._mark_row_override("password_row", row, None, NOTHING)
        except Exception:
            logger.debug("Could not mark the password", exc_info=True)

    def _mark_row_override(self, row_name: str, row, kind: Optional[str], verdict) -> None:
        """Show *verdict* (a :class:`~sshpilot.ssh_config_answer.LineVerdict`)
        on one row as Advanced Configuration shows it on a line: the tag
        beside it (a "No effect" one links to the line used instead), the
        typed value struck through and tinted while it is not used, red
        while OpenSSH rejects it."""
        from .advanced_config_widgets import mark_unused, show_verdict, verdict_pill

        label = getattr(row, "_sshpilot_override_pill", None)
        if kind and label is None:
            label = verdict_pill(self._reveal_config_line)
            row.add_suffix(label)
            row._sshpilot_override_pill = label
            # Beside the value, ahead of the row's own buttons.
            before = getattr(row, "_sshpilot_pill_before", None)
            box = label.get_parent()
            if before is not None and box is not None and before.get_parent() is box:
                box.reorder_child_after(label, before.get_prev_sibling())
        if label is not None:
            show_verdict(label, verdict if kind else NOTHING)
        unused = kind in (OVERRIDDEN, UNUSED)
        row._sshpilot_override_state = kind
        note = verdict.note if kind else ""
        try:
            mark_unused(row, unused)
        except Exception:
            logger.debug("Could not mark %s", row_name, exc_info=True)
        self._set_row_override_error(row, kind == REJECTED)
        self._set_row_override_note(row, note)
        # "Edit to set it for this host" is false while Settings force the
        # option; the note replaces it, and it returns with the override gone.
        if row_name in (getattr(self, "_inherited_row_handlers", None) or {}):
            self._set_row_inherited_note(
                row, note if kind else self._inherited_advice(row_name)
            )

    def _inherited_advice(self, row_name: str) -> str:
        """The tooltip of a row showing an inherited value: where it comes
        from, and that editing it sets it for this host."""
        keyword = dict(OVERRIDABLE_ROWS).get(row_name)
        answer = getattr(self, "_editor_answer", None)
        source = ""
        if keyword and answer is not None:
            try:
                source = answer.inherited_note(keyword)
            except Exception:
                source = ""
        return inherited_note_text(source)

    def _advanced_option_note(
        self, keyword: str, value: str
    ) -> Optional[Tuple[str, bool]]:
        """``(note, unused)`` for an Extra Options row, or None.

        *unused* is False only for an option Settings add to rather than
        replace: both values are used there.
        """
        answer = getattr(self, "_editor_answer", None)
        added = added_by_settings_note(keyword, answer)
        if added:
            return added, False
        if answer is None:
            return None
        verdict = answer.line_verdict_of(keyword, value)
        if verdict.tag and verdict.note:
            return verdict.note, True
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

    def _reveal_config_line(self, target) -> None:
        """A "No effect" tag was clicked: open Advanced Configuration at the
        line used instead, as the same tag does in Advanced Configuration."""
        parent = getattr(self, "parent_window", None)
        opener = getattr(parent, "open_advanced_config", None)
        if not callable(opener):
            return
        file, line = target
        connection = getattr(self, "connection", None)
        host = (getattr(connection, "nickname", "") or "") if connection is not None else ""
        opener(
            host=host, line=line, file=file or "",
            over=self, on_saved=self._on_ssh_config_saved,
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
            # A plain banner: nothing is wrong, the connection works; the
            # fields are marked. The button shows the lines that decide.
            banner.set_button_label(_("Advanced Configuration"))
            banner.connect("button-clicked", self._on_advanced_config_clicked)
            self._override_banner = banner
            try:
                self.content_mount.prepend(banner)
            except Exception:
                logger.debug("Could not mount the override banner", exc_info=True)
        if show:
            # Advanced Configuration's own sentences, counts and all: the
            # pages holding the marked fields are flagged in the sidebar.
            from .gtk import advanced_config_messages as messages

            parts = []
            if by_earlier:
                parts.append(messages.precedence_problem(by_earlier))
            if by_settings:
                parts.append(messages.settings_problem(by_settings))
            title = " ".join(parts)
            banner.set_title(title)
        banner.set_revealed(show)
        # The marked fields may be on pages other than the one shown.
        sync_marks = getattr(self, "_sync_sidebar_override_marks", None)
        if callable(sync_marks):
            sync_marks()
