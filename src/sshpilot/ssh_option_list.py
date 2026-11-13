"""An editable list of SSH options: keyword, value, remove; plus "Add Option".

Used wherever the app lets someone add options it has no dedicated control
for: Settings ▸ SSH Options (sent with every connection) and the connection
dialog's Advanced tab (written to that host's block). One component, so the
two cannot drift apart again.

* The keyword is a text field, not a fixed dropdown. Known OpenSSH keywords
  are suggested as the user types (and the arrow on the field lists them
  all), but any keyword can be typed: a hard-coded list goes stale with every
  OpenSSH release, and a keyword missing from it could not be entered or even
  kept.
* A row is checked when the user finishes editing it (focus leaves, or Enter),
  never on each keystroke: a half-typed option is not an error yet. The caller
  supplies the check; :func:`check_ssh_option` is the usual one and asks
  OpenSSH itself to parse the line.
* Rows loaded with :meth:`SshOptionList.set_options` are trusted until edited.
  They are what is already saved; refusing to hand them back would silently
  delete someone's configuration.
* A refused edit never changes what the list reports. Each row remembers the
  last value that was accepted, and keeps reporting it while the row shows an
  error, so a caller that saves on ``changed`` cannot lose a saved option to
  a typo. Only the remove button (or emptying both fields) takes one away.
"""
from __future__ import annotations

from gettext import gettext as _
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, GObject, Gtk  # noqa: E402

from .core.settings import (  # noqa: E402
    ExtraOptionError,
    ExtraOptionProblem,
    normalize_extra_option,
)

#: OpenSSH client keywords offered in the keyword menu. A convenience, not a
#: gate: anything else can still be typed, so a keyword missing here is never
#: unusable. Config-file structure (``Host``, ``Match``, ``Include``) is left
#: out because it is not an option.
OPENSSH_KEYWORDS = (
    'AddKeysToAgent', 'AddressFamily', 'BatchMode', 'BindAddress', 'BindInterface',
    'CanonicalDomains', 'CanonicalizeFallbackLocal', 'CanonicalizeHostname',
    'CanonicalizeMaxDots', 'CanonicalizePermittedCNAMEs', 'CASignatureAlgorithms',
    'CertificateFile', 'ChannelTimeout', 'CheckHostIP', 'Ciphers',
    'ClearAllForwardings', 'Compression', 'ConnectionAttempts', 'ConnectTimeout',
    'ControlMaster', 'ControlPath', 'ControlPersist', 'DynamicForward',
    'EnableEscapeCommandline', 'EnableSSHKeysign', 'EscapeChar',
    'ExitOnForwardFailure', 'FingerprintHash', 'ForkAfterAuthentication',
    'ForwardAgent', 'ForwardX11', 'ForwardX11Timeout', 'ForwardX11Trusted',
    'GatewayPorts', 'GlobalKnownHostsFile', 'GSSAPIAuthentication',
    'GSSAPIClientIdentity', 'GSSAPIDelegateCredentials', 'GSSAPIKeyExchange',
    'GSSAPIRenewalForcesRekey', 'GSSAPIServerIdentity', 'GSSAPITrustDns',
    'HashKnownHosts', 'HostbasedAcceptedAlgorithms', 'HostbasedAuthentication',
    'HostKeyAlgorithms', 'HostKeyAlias', 'HostName', 'IdentitiesOnly',
    'IdentityAgent', 'IdentityFile', 'IgnoreUnknown', 'IPQoS',
    'KbdInteractiveAuthentication', 'KbdInteractiveDevices', 'KexAlgorithms',
    'KnownHostsCommand', 'LocalCommand', 'LocalForward', 'LogLevel', 'MACs',
    'NoHostAuthenticationForLocalhost', 'NumberOfPasswordPrompts',
    'ObscureKeystrokeTiming', 'PasswordAuthentication', 'PermitLocalCommand',
    'PermitRemoteOpen', 'PKCS11Provider', 'Port', 'PreferredAuthentications',
    'ProxyCommand', 'ProxyJump', 'ProxyUseFdpass', 'PubkeyAcceptedAlgorithms',
    'PubkeyAuthentication', 'RekeyLimit', 'RemoteCommand', 'RemoteForward',
    'RequestTTY', 'RequiredRSASize', 'RevokedHostKeys', 'SecurityKeyProvider',
    'SendEnv', 'ServerAliveCountMax', 'ServerAliveInterval', 'SessionType',
    'SetEnv', 'StdinNull', 'StreamLocalBindMask', 'StreamLocalBindUnlink',
    'StrictHostKeyChecking', 'SyslogFacility', 'Tag', 'TCPKeepAlive', 'Tunnel',
    'TunnelDevice', 'UpdateHostKeys', 'User', 'UserKnownHostsFile',
    'VerifyHostKeyDNS', 'VisualHostKey', 'XAuthLocation',
)

Option = Tuple[str, str]
Validator = Callable[[str, str], Optional[str]]


def check_ssh_option(
    keyword: str,
    value: str,
    *,
    reserved: Iterable[str],
    reserved_message: str,
    parse: Optional[Callable[[str], Optional[str]]] = None,
) -> Optional[str]:
    """Return why ``keyword value`` cannot be used, or ``None`` when it can.

    *reserved* holds lower-case keywords that have their own control where the
    list is shown; *reserved_message* words that refusal (``{keyword}`` is
    filled in). *parse* checks SSH config text and returns an error line; it
    defaults to asking the installed OpenSSH, which only parses and never
    connects.
    """
    try:
        normalize_extra_option(f"{keyword} {value}", reserved=frozenset(reserved))
    except ExtraOptionError as error:
        if error.problem is ExtraOptionProblem.HAS_OWN_SETTING:
            return reserved_message.format(keyword=error.keyword)
        if error.problem is ExtraOptionProblem.NOT_AN_OPTION:
            return _("“{keyword}” is not an option.").format(keyword=error.keyword)
        return _("This is not a valid option.")
    if parse is None:
        from .ssh_config_utils import validate_ssh_config_text as parse
    rejected = parse(f"Host *\n    {keyword} {value}\n")
    if rejected:
        return _("OpenSSH did not accept this option: {error}").format(error=rejected)
    return None


def options_from_strings(options: Iterable[str]) -> List[Option]:
    """``["Keyword=value", ...]`` (the stored form) as keyword/value pairs."""
    pairs = []
    for option in options or ():
        keyword, _sep, value = str(option).partition("=")
        pairs.append((keyword, value))
    return pairs


def strings_from_options(options: Iterable[Option]) -> Tuple[str, ...]:
    """Keyword/value pairs as the stored ``Keyword=value`` strings."""
    return tuple(f"{keyword}={value}" for keyword, value in options)


class _OptionRow(Gtk.ListBoxRow):
    """One keyword/value pair with its remove button and error line."""

    def __init__(self) -> None:
        super().__init__()
        self.set_activatable(False)
        self.set_selectable(False)
        #: Edited since it was last checked (or loaded).
        self.dirty = False
        #: Why this row is refused, or None.
        self.error: Optional[str] = None
        #: The last keyword/value accepted for this row (loaded or checked).
        self.good: Optional[Option] = None
        #: Keyword suggestions, created the first time they are needed.
        self.suggestions: Optional[Gtk.Popover] = None
        self.suggestion_list: Optional[Gtk.ListBox] = None

        self.keyword_entry = Gtk.Entry()
        self.keyword_entry.set_placeholder_text(_("Keyword"))
        self.keyword_entry.set_width_chars(22)
        self.keyword_entry.set_icon_from_icon_name(
            Gtk.EntryIconPosition.SECONDARY, "pan-down-symbolic"
        )
        self.keyword_entry.set_icon_tooltip_text(
            Gtk.EntryIconPosition.SECONDARY, _("Choose a keyword")
        )
        self.keyword_entry.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Keyword")]
        )

        self.value_entry = Gtk.Entry()
        self.value_entry.set_placeholder_text(_("Value"))
        self.value_entry.set_hexpand(True)
        self.value_entry.update_property([Gtk.AccessibleProperty.LABEL], [_("Value")])

        self.remove_button = Gtk.Button(icon_name="user-trash-symbolic")
        self.remove_button.add_css_class("flat")
        self.remove_button.set_valign(Gtk.Align.CENTER)
        self.remove_button.set_tooltip_text(_("Remove"))

        self.fields = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.fields.append(self.keyword_entry)
        self.fields.append(self.value_entry)
        self.fields.append(self.remove_button)

        self.error_label = Gtk.Label(xalign=0)
        self.error_label.set_wrap(True)
        self.error_label.add_css_class("error")
        self.error_label.add_css_class("caption")
        self.error_label.set_visible(False)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(12)
        box.set_margin_end(8)
        box.append(self.fields)
        box.append(self.error_label)

        # A remark about an accepted option. Unlike the error it refuses nothing.
        self.note_label = Gtk.Label(xalign=0)
        self.note_label.set_wrap(True)
        self.note_label.add_css_class("dim-label")
        self.note_label.add_css_class("caption")
        self.note_label.set_visible(False)
        box.append(self.note_label)
        self.set_child(box)

    def pair(self) -> Option:
        return (
            self.keyword_entry.get_text().strip(),
            self.value_entry.get_text().strip(),
        )

    def show_error(self, error: Optional[str]) -> None:
        self.error = error or None
        self.error_label.set_label(self.error or "")
        self.error_label.set_visible(bool(self.error))
        for entry in (self.keyword_entry, self.value_entry):
            if self.error:
                entry.add_css_class("error")
            else:
                entry.remove_css_class("error")
        if self.error:
            self.show_note(None)

    def show_note(self, note: Optional[str], *, unused: bool = False) -> None:
        """Show a remark under the row; *unused* says the value is never used.

        An unused value is drawn in Adwaita's error style, like a refused
        one, though nothing is refused: the option is still saved.
        """
        unused = bool(note) and unused
        self.note_label.set_label(note or "")
        self.note_label.set_visible(bool(note))
        if unused:
            self.note_label.remove_css_class("dim-label")
            self.note_label.add_css_class("error")
        else:
            self.note_label.remove_css_class("error")
            self.note_label.add_css_class("dim-label")
        if not self.error:
            if unused:
                self.value_entry.add_css_class("error")
            else:
                self.value_entry.remove_css_class("error")


class SshOptionList(Gtk.Box):
    """Boxed list of option rows ending in an "Add Option" row.

    ``changed`` fires when the set of usable options changes (a row was
    finished, loaded rows aside, or removed). ``edited`` fires on every
    keystroke, for callers that mirror the rows live.
    """

    __gtype_name__ = "SshPilotSshOptionList"

    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "edited": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(
        self,
        *,
        keywords: Sequence[str] = (),
        validate: Optional[Validator] = None,
        note: Optional[Validator] = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        #: Like *validate*, but its text is shown as a remark under an accepted
        #: row and refuses nothing.
        self._note = note
        self._keywords = tuple(keywords)
        self._canonical = {name.lower(): name for name in self._keywords}
        self._validate = validate
        self._rows: List[_OptionRow] = []
        self._last: Tuple[Option, ...] = ()
        self._loading = False

        self._listbox = Gtk.ListBox()
        self._listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self._listbox.add_css_class("boxed-list")
        self.add_row = Adw.ButtonRow(title=_("Add Option"))
        self.add_row.set_start_icon_name("list-add-symbolic")
        self.add_row.connect("activated", self._on_add_activated)
        self._listbox.append(self.add_row)
        self.append(self._listbox)

    # -- reading and loading --------------------------------------------------

    def rows(self) -> Tuple[_OptionRow, ...]:
        return tuple(self._rows)

    def get_options(self) -> List[Option]:
        """The options the rows stand for, in order.

        A row that is refused, or half filled in, stands for the last value it
        was accepted with (nothing, if it never was). A row with both fields
        empty stands for nothing.
        """
        options = []
        for row in self._rows:
            keyword, value = row.pair()
            if not keyword and not value:
                continue
            if keyword and value and row.error is None:
                options.append((keyword, value))
            elif row.good is not None:
                options.append(row.good)
        return options

    def set_options(self, options: Iterable[Option]) -> None:
        """Show *options* as the saved state. Emits nothing; rows are trusted."""
        self._loading = True
        try:
            for row in list(self._rows):
                self._drop(row)
            for keyword, value in options or ():
                row = self._new_row()
                row.keyword_entry.set_text(str(keyword))
                row.value_entry.set_text(str(value))
                row.dirty = False
                row.good = row.pair() if all(row.pair()) else None
                self._update_note(row)
        finally:
            self._loading = False
        self._last = tuple(self.get_options())

    def commit_all(self) -> bool:
        """Check every edited row now. True when no row is refused."""
        for row in self._rows:
            self._check(row)
        self._emit_if_changed()
        return not self.has_errors()

    def has_errors(self) -> bool:
        return any(row.error for row in self._rows)

    def refresh_notes(self) -> None:
        """Ask *note* again for every row: what it depends on has changed."""
        for row in self._rows:
            self._update_note(row)

    def add_empty_row(self) -> _OptionRow:
        row = self._new_row()
        row.keyword_entry.grab_focus()
        return row

    # -- rows -----------------------------------------------------------------

    def _new_row(self) -> _OptionRow:
        row = _OptionRow()
        self._rows.append(row)
        self._listbox.insert(row, len(self._rows) - 1)  # above "Add Option"
        row.keyword_entry.connect("changed", self._on_entry_changed, row)
        row.keyword_entry.connect("changed", self._on_keyword_typed, row)
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_keyword_key, row)
        row.keyword_entry.add_controller(keys)
        row.value_entry.connect("changed", self._on_entry_changed, row)
        row.keyword_entry.connect(
            "activate", lambda _e, r=row: r.value_entry.grab_focus()
        )
        row.keyword_entry.connect("icon-press", self._on_keyword_menu, row)
        row.value_entry.connect("activate", self._on_value_activated, row)
        row.remove_button.connect("clicked", lambda _b, r=row: self.remove(r))
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", lambda _c, r=row: self._finish(r))
        keyword_focus = Gtk.EventControllerFocus()
        keyword_focus.connect("leave", lambda _c, r=row: self._hide_suggestions(r))
        row.keyword_entry.add_controller(keyword_focus)
        row.fields.add_controller(focus)
        return row

    def _drop(self, row: _OptionRow) -> None:
        if row.suggestions is not None:
            row.suggestions.unparent()
            row.suggestions = None
        self._rows.remove(row)
        self._listbox.remove(row)

    def remove(self, row: _OptionRow) -> None:
        if row not in self._rows:
            return
        self._drop(row)
        self.emit("edited")
        self._emit_if_changed()

    def _on_add_activated(self, _row) -> None:
        self.add_empty_row()

    def _on_entry_changed(self, _entry, row: _OptionRow) -> None:
        if self._loading:
            return
        row.dirty = True
        self.emit("edited")

    def _on_value_activated(self, _entry, row: _OptionRow) -> None:
        self._finish(row)
        if row is self._rows[-1] and all(row.pair()) and row.error is None:
            self.add_empty_row()

    def _check(self, row: _OptionRow) -> None:
        if not row.dirty:
            return
        row.dirty = False
        keyword, value = row.pair()
        known = self._canonical.get(keyword.lower())
        if known and known != keyword:
            # Spell a known keyword the way OpenSSH documents it.
            self._loading = True
            try:
                row.keyword_entry.set_text(known)
            finally:
                self._loading = False
            keyword = known
        if not keyword and not value:
            row.show_error(None)  # an empty row is simply not an option
        elif not keyword or not value:
            # Half an option. Saying so beats leaving it out without a word.
            row.show_error(_("Enter both a keyword and a value."))
        else:
            row.show_error(
                self._validate(keyword, value) if self._validate is not None else None
            )
            if row.error is None:
                row.good = (keyword, value)
        self._update_note(row)

    def _update_note(self, row: _OptionRow) -> None:
        keyword, value = row.pair()
        if self._note is None or row.error or not keyword or not value:
            row.show_note(None)
            return
        # A note is text, or ``(text, unused)`` when the remark means the
        # value is never used.
        note = self._note(keyword, value)
        if isinstance(note, tuple):
            text, unused = note
            row.show_note(text, unused=bool(unused))
        else:
            row.show_note(note)

    def _finish(self, row: _OptionRow) -> None:
        if row not in self._rows:
            return
        self._check(row)
        self._emit_if_changed()

    def _emit_if_changed(self) -> None:
        current = tuple(self.get_options())
        if current != self._last:
            self._last = current
            self.emit("changed")

    # -- keyword suggestions --------------------------------------------------

    def keyword_matches(self, text: str) -> List[str]:
        """Known keywords for what has been typed: prefix matches first."""
        typed = text.strip().lower()
        if not typed:
            return []
        starts = [name for name in self._keywords if name.lower().startswith(typed)]
        inside = [
            name for name in self._keywords
            if typed in name.lower() and name not in starts
        ]
        matches = starts + inside
        if len(matches) == 1 and matches[0].lower() == typed:
            return []  # already typed in full: nothing left to suggest
        return matches

    def _on_keyword_typed(self, entry, row: _OptionRow) -> None:
        if self._loading:
            return
        # Only while the user is typing here, never for text set from code.
        if not entry.get_delegate().has_focus():
            return
        self._show_suggestions(row, self.keyword_matches(entry.get_text()))

    def _on_keyword_menu(self, entry, _position, row: _OptionRow) -> None:
        """The arrow lists every keyword, or the matches for what is typed."""
        if self.suggestions_visible(row):
            self._hide_suggestions(row)
            return
        entry.grab_focus_without_selecting()
        self._show_suggestions(
            row, self.keyword_matches(entry.get_text()) or list(self._keywords)
        )

    def suggestions_visible(self, row: _OptionRow) -> bool:
        return row.suggestions is not None and row.suggestions.get_visible()

    def shown_suggestions(self, row: _OptionRow) -> List[str]:
        if not self.suggestions_visible(row):
            return []
        names = []
        item = row.suggestion_list.get_first_child()
        while item is not None:
            names.append(item.get_child().get_label())
            item = item.get_next_sibling()
        return names

    def _show_suggestions(self, row: _OptionRow, names: Sequence[str]) -> None:
        if not names or not row.keyword_entry.get_mapped():
            self._hide_suggestions(row)
            return
        if row.suggestions is None:
            # Never takes the focus: typing carries on in the entry while the
            # list follows it.
            popover = Gtk.Popover()
            popover.set_autohide(False)
            popover.set_has_arrow(False)
            popover.set_can_focus(False)
            popover.set_position(Gtk.PositionType.BOTTOM)
            popover.set_halign(Gtk.Align.START)
            popover.add_css_class("menu")
            names_box = Gtk.ListBox()
            names_box.set_selection_mode(Gtk.SelectionMode.SINGLE)
            names_box.set_can_focus(False)
            names_box.connect(
                "row-activated",
                lambda _box, item, r=row: self._accept_suggestion(
                    r, item.get_child().get_label()
                ),
            )
            scroller = Gtk.ScrolledWindow()
            scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            scroller.set_propagate_natural_height(True)
            scroller.set_max_content_height(220)
            scroller.set_min_content_width(240)
            scroller.set_child(names_box)
            popover.set_child(scroller)
            popover.set_parent(row.keyword_entry)
            row.suggestions, row.suggestion_list = popover, names_box
        row.suggestion_list.remove_all()
        for name in names:
            label = Gtk.Label(label=name, xalign=0)
            label.set_margin_top(6)
            label.set_margin_bottom(6)
            label.set_margin_start(8)
            label.set_margin_end(8)
            item = Gtk.ListBoxRow()
            item.set_can_focus(False)
            item.set_child(label)
            row.suggestion_list.append(item)
        row.suggestion_list.unselect_all()
        row.suggestions.popup()

    def _hide_suggestions(self, row: _OptionRow) -> None:
        if row.suggestions is not None:
            row.suggestions.popdown()

    def _accept_suggestion(self, row: _OptionRow, name: str) -> None:
        self._hide_suggestions(row)
        row.keyword_entry.set_text(name)
        row.keyword_entry.set_position(-1)
        self._hide_suggestions(row)  # setting the text may have reopened it
        row.value_entry.grab_focus()

    def _on_keyword_key(self, _controller, keyval, _keycode, _state, row) -> bool:
        """Down/Up walk the suggestions, Enter or Tab take the selected one."""
        if not self.suggestions_visible(row):
            return False
        names_box = row.suggestion_list
        selected = names_box.get_selected_row()
        if keyval in (Gdk.KEY_Down, Gdk.KEY_Up):
            step = 1 if keyval == Gdk.KEY_Down else -1
            index = (selected.get_index() if selected is not None else -1 if step > 0 else 0) + step
            target = names_box.get_row_at_index(index)
            if target is not None:
                names_box.select_row(target)
                # Keep the selection in view without moving the focus.
                adjustment = row.suggestions.get_child().get_vadjustment()
                height = max(target.get_height(), 1)
                top = index * height
                if top < adjustment.get_value():
                    adjustment.set_value(top)
                elif top + height > adjustment.get_value() + adjustment.get_page_size():
                    adjustment.set_value(top + height - adjustment.get_page_size())
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_Tab, Gdk.KEY_ISO_Enter):
            if selected is not None:
                self._accept_suggestion(row, selected.get_child().get_label())
                return True
            self._hide_suggestions(row)
            return False
        if keyval == Gdk.KEY_Escape:
            self._hide_suggestions(row)
            return True
        return False


__all__ = [
    "OPENSSH_KEYWORDS",
    "SshOptionList",
    "check_ssh_option",
    "options_from_strings",
    "strings_from_options",
]
