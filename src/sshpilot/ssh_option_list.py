"""An editable list of SSH options: keyword, value, remove; plus "Add Option".

Used wherever the app lets someone add options it has no dedicated control
for: Settings ▸ SSH Options (sent with every connection) and the connection
dialog's Advanced tab (written to that host's block). One component, so the
two cannot drift apart again.

* The keyword is a text field, not a fixed dropdown. A menu on the field
  offers the known OpenSSH keywords, but any keyword can be typed: a hard-coded
  list goes stale with every OpenSSH release, and a keyword missing from it
  could not be entered or even kept.
* A row is checked when the user finishes editing it (focus leaves, or Enter),
  never on each keystroke: a half-typed option is not an error yet. The caller
  supplies the check; :func:`check_ssh_option` is the usual one and asks
  OpenSSH itself to parse the line.
* Rows loaded with :meth:`SshOptionList.set_options` are trusted until edited.
  They are what is already saved; refusing to hand them back would silently
  delete someone's configuration.
"""
from __future__ import annotations

from gettext import gettext as _
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GObject, Gtk  # noqa: E402

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
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
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
        """Complete rows that are not refused, in order."""
        return [
            row.pair() for row in self._rows
            if all(row.pair()) and row.error is None
        ]

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
        row.value_entry.connect("changed", self._on_entry_changed, row)
        row.keyword_entry.connect(
            "activate", lambda _e, r=row: r.value_entry.grab_focus()
        )
        row.keyword_entry.connect("icon-press", self._on_keyword_menu, row)
        row.value_entry.connect("activate", self._on_value_activated, row)
        row.remove_button.connect("clicked", lambda _b, r=row: self.remove(r))
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", lambda _c, r=row: self._finish(r))
        row.fields.add_controller(focus)
        return row

    def _drop(self, row: _OptionRow) -> None:
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
        if keyword and value and self._validate is not None:
            row.show_error(self._validate(keyword, value))
        else:
            row.show_error(None)  # incomplete: not an option yet, not an error

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

    # -- keyword menu ---------------------------------------------------------

    def _on_keyword_menu(self, entry, _position, row: _OptionRow) -> None:
        if not self._keywords:
            return
        popover = Gtk.Popover()
        popover.set_parent(entry)
        popover.connect("closed", lambda p: p.unparent())
        search = Gtk.SearchEntry()
        search.set_placeholder_text(_("Search keywords"))
        names = Gtk.ListBox()
        names.set_selection_mode(Gtk.SelectionMode.NONE)
        for name in self._keywords:
            label = Gtk.Label(label=name, xalign=0)
            label.set_margin_top(6)
            label.set_margin_bottom(6)
            label.set_margin_start(8)
            label.set_margin_end(8)
            names.append(label)
        names.set_filter_func(
            lambda item: search.get_text().strip().lower()
            in item.get_child().get_label().lower()
        )
        search.connect("search-changed", lambda _s: names.invalidate_filter())

        def _chosen(_box, item):
            entry.set_text(item.get_child().get_label())
            popover.popdown()
            row.value_entry.grab_focus()

        names.connect("row-activated", _chosen)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_min_content_height(240)
        scroller.set_min_content_width(260)
        scroller.set_child(names)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.append(search)
        box.append(scroller)
        popover.set_child(box)
        popover.popup()
        search.grab_focus()


__all__ = [
    "OPENSSH_KEYWORDS",
    "SshOptionList",
    "check_ssh_option",
    "options_from_strings",
    "strings_from_options",
]
