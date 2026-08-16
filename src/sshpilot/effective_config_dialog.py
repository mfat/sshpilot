"""Unified effective-SSH-config viewer.

The default summary explains each changed setting in plain language. An optional
full comparison shows daemon-returned authored and effective values side by
side. Both the post-save warning and connection context menu use this window.
"""

from __future__ import annotations

import difflib
import logging
from gettext import gettext as _
from typing import Dict, List, Optional, Tuple

from gi.repository import Gtk, Adw, GLib, Pango

try:
    from .shortcut_utils import install_esc_to_close
except Exception:  # pragma: no cover - helper is optional
    def install_esc_to_close(_window):
        return None

logger = logging.getLogger(__name__)

_COLOR_REMOVED = "#c01c28"   # in the host block, dropped/overridden by globals
_COLOR_ADDED = "#26a269"     # what SSH actually uses (added/overridden by globals)
_COLOR_YOURS = "#3584e4"

_FRIENDLY_KEYS = {
    'identityfile': _('SSH key (IdentityFile)'),
    'certificatefile': _('Certificate (CertificateFile)'),
    'user': _('Username'),
    'hostname': _('Host name'),
    'port': _('Port'),
    'proxyjump': _('Proxy jump'),
    'proxycommand': _('Proxy command'),
    'forwardagent': _('Forward agent'),
    'identitiesonly': _('Only use the selected key(s)'),
    'identityagent': _('Identity agent'),
    'pubkeyauthentication': _('Public-key authentication'),
    'preferredauthentications': _('Preferred authentications'),
    'stricthostkeychecking': _('Strict host key checking'),
    'addkeystoagent': _('Add keys to agent'),
    'requesttty': _('Request TTY'),
}


def _friendly_key(key: str) -> str:
    return _FRIENDLY_KEYS.get(key.lower(), key)


def _span(values: List[str], color: str) -> str:
    text = ', '.join(values) if values else _('(none)')
    return f'<span foreground="{color}">{GLib.markup_escape_text(text)}</span>'


def _diff_rows(own: List[str], full: List[str],
               full_mode: bool) -> List[Tuple[str, str, str]]:
    """Align own vs. full into (left, right, kind) rows via SequenceMatcher.

    kind ∈ {equal, replace, delete, insert}. In changes-only mode equal runs are
    normally dropped, but unchanged values of a changed multi-value directive
    are retained so the relevant setting remains complete on both sides.
    """
    opcodes = difflib.SequenceMatcher(None, own, full).get_opcodes()

    def _key(line: str) -> str:
        return line.split(None, 1)[0] if line else ''

    changed_keys = set()
    if not full_mode:
        for tag, i1, i2, j1, j2 in opcodes:
            if tag == 'equal':
                continue
            changed_keys.update(_key(line) for line in own[i1:i2])
            changed_keys.update(_key(line) for line in full[j1:j2])
        changed_keys.discard('')

    rows: List[Tuple[str, str, str]] = []
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == 'equal':
            for k in range(i2 - i1):
                if full_mode or _key(own[i1 + k]) in changed_keys:
                    rows.append((own[i1 + k], full[j1 + k], 'equal'))
        elif tag == 'replace':
            left, right = own[i1:i2], full[j1:j2]
            for k in range(max(len(left), len(right))):
                rows.append((left[k] if k < len(left) else '',
                             right[k] if k < len(right) else '', 'replace'))
        elif tag == 'delete':
            for k in range(i1, i2):
                rows.append((own[k], '', 'delete'))
        elif tag == 'insert':
            for k in range(j1, j2):
                rows.append(('', full[k], 'insert'))
    return rows


class EffectiveConfigDialog(Adw.Window):
    """Summary and full views of host-block vs. effective SSH configuration."""

    __gtype_name__ = "SshPilotEffectiveConfigDialog"

    @classmethod
    def for_result(cls, parent, host: str, result: dict):
        """Open the viewer with an already-computed post-save result."""
        dialog = cls(parent, host=host, result=result)
        dialog.present()
        return dialog

    def __init__(self, parent, *, host: str, own_block: str = '',
                 root_config: Optional[str] = None, is_new: bool = False,
                 result: Optional[dict] = None) -> None:
        super().__init__()
        self._parent = parent
        self._host = host
        self._own_lines: List[str] = []
        self._full_lines: List[str] = []
        self._changes: List[Dict[str, object]] = []
        self._computed = False

        self.set_transient_for(parent)
        # Non-modal: the Edit button opens the SSH config editor (transient for
        # the main window); a modal grab here would block it.
        self.set_modal(False)
        self.set_title(_("Effective SSH configuration"))
        self.set_default_size(820, 560)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()

        edit_button = Gtk.Button(label=_("Edit SSH config…"))
        edit_button.set_tooltip_text(_("Open the SSH config editor to change global rules"))
        edit_button.connect("clicked", self._on_edit_clicked)
        header.pack_start(edit_button)

        self._full_toggle = Gtk.ToggleButton(label=_("Show full configuration"))
        self._full_toggle.set_tooltip_text(
            _("Show the complete host-block and effective SSH configurations"))
        self._full_toggle.connect("toggled", self._on_view_toggled)
        header.pack_end(self._full_toggle)
        toolbar.add_top_bar(header)

        self._body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._body.set_vexpand(True)
        toolbar.set_content(self._body)
        self.set_content(toolbar)

        install_esc_to_close(self)

        if result is not None:
            self._on_computed(result)
        else:
            self._on_computed(None)

    # ---- computation -------------------------------------------------------

    def _on_computed(self, result):
        self._computed = True
        if result:
            self._own_lines = list(result.get('own') or [])
            self._full_lines = list(result.get('full') or [])
            self._changes = list(result.get('changes') or [])
        self._render()
        return False

    # ---- rendering ---------------------------------------------------------

    def _on_view_toggled(self, button) -> None:
        if button.get_active():
            button.set_label(_("Show differences only"))
            button.set_tooltip_text(
                _("Return to the summary of settings that differ"))
        else:
            button.set_label(_("Show full configuration"))
            button.set_tooltip_text(
                _("Show the complete host-block and effective SSH configurations"))
        self._render()

    def _clear_body(self):
        child = self._body.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._body.remove(child)
            child = nxt

    def _show_spinner(self):
        self._clear_body()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_valign(Gtk.Align.CENTER)
        box.set_vexpand(True)
        spinner = Gtk.Spinner()
        spinner.set_size_request(32, 32)
        spinner.start()
        box.append(spinner)
        box.append(Gtk.Label(label=_("Resolving effective configuration…")))
        self._body.append(box)

    def _placeholder(self, text: str):
        self._clear_body()
        label = Gtk.Label(label=text)
        label.set_wrap(True)
        label.set_justify(Gtk.Justification.CENTER)
        label.add_css_class("dim-label")
        label.set_valign(Gtk.Align.CENTER)
        label.set_vexpand(True)
        label.set_margin_start(24)
        label.set_margin_end(24)
        self._body.append(label)

    def _render(self):
        if not self._computed:
            return
        if not self._full_lines:
            self._placeholder(_(
                "Couldn't resolve the effective configuration.\n"
                "The ssh binary may be unavailable, or the connection isn't saved yet."
            ))
            return

        if self._full_toggle.get_active():
            self._render_full_comparison()
        else:
            self._render_summary()

    def _render_summary(self):
        if not self._changes:
            self._placeholder(_(
                "No differences — what this connection's Host block resolves to "
                "matches what SSH will actually use. Choose “Show full "
                "configuration” to inspect every setting."
            ))
            return

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.set_margin_top(18)
        content.set_margin_bottom(18)
        content.set_margin_start(18)
        content.set_margin_end(18)
        content.append(self._label(_(
            "What this connection's Host block resolves to differs from what "
            "SSH will actually use. SSH applies the effective values shown below."
        ), "body"))
        content.append(self._label(
            _("Settings that differ"), "heading"))

        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        listbox.add_css_class("boxed-list")
        for change in self._changes:
            dropped = change.get('kind') in ('overridden', 'removed')
            yours = _span(
                list(change.get('own') or []),
                _COLOR_REMOVED if dropped else _COLOR_YOURS,
            )
            effective = _span(
                list(change.get('effective') or []), _COLOR_ADDED)

            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            box.set_margin_top(10)
            box.set_margin_bottom(10)
            box.set_margin_start(12)
            box.set_margin_end(12)
            box.append(self._label(
                _friendly_key(str(change.get('key') or '')), "title-4"))
            box.append(self._label(_("Connection resolves to:"), "body"))
            box.append(self._value_label(yours))
            box.append(self._label(_("SSH uses:"), "body"))
            box.append(self._value_label(effective))

            row = Gtk.ListBoxRow()
            row.set_activatable(False)
            row.set_child(box)
            listbox.append(row)
        content.append(listbox)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        scrolled.set_child(content)
        self._clear_body()
        self._body.append(scrolled)

    def _render_full_comparison(self):
        rows = _diff_rows(self._own_lines, self._full_lines, full_mode=True)
        # Highlight only the differences the summary reports. Both columns are
        # The daemon reports only the authored/effective comparison, so the
        # renderer can highlight changed directives without knowing filesystem
        # paths or reproducing OpenSSH resolution.
        changed_keys = {str(c.get('key') or '') for c in self._changes}

        def _line_key(line: str) -> str:
            return line.split(None, 1)[0].lower() if line else ''

        grid = Gtk.Grid(column_homogeneous=True, column_spacing=18, row_spacing=3)
        grid.set_margin_top(12)
        grid.set_margin_bottom(12)
        grid.set_margin_start(12)
        grid.set_margin_end(12)
        grid.attach(self._heading(_("Connection resolves to")), 0, 0, 1, 1)
        grid.attach(self._heading(_("SSH will use")), 1, 0, 1, 1)

        for r, (left, right, kind) in enumerate(rows, start=1):
            reported = (_line_key(left) in changed_keys
                        or _line_key(right) in changed_keys)
            left_color = (_COLOR_REMOVED
                          if reported and kind in ('delete', 'replace') else None)
            right_color = (_COLOR_ADDED
                           if reported and kind in ('insert', 'replace') else None)
            grid.attach(self._cell(left, left_color), 0, r, 1, 1)
            grid.attach(self._cell(right, right_color), 1, r, 1, 1)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        scrolled.set_child(grid)
        self._clear_body()
        self._body.append(scrolled)

    @staticmethod
    def _label(text: str, *css_classes: str) -> Gtk.Label:
        label = Gtk.Label(label=text)
        label.set_xalign(0.0)
        label.set_wrap(True)
        for css in css_classes:
            label.add_css_class(css)
        return label

    @staticmethod
    def _value_label(markup: str) -> Gtk.Label:
        label = Gtk.Label()
        label.set_markup(markup)
        label.set_xalign(0.0)
        label.set_wrap(True)
        label.set_selectable(True)
        label.add_css_class("monospace")
        return label

    @staticmethod
    def _heading(text: str) -> Gtk.Label:
        label = Gtk.Label(label=text)
        label.set_xalign(0.0)
        label.add_css_class("heading")
        return label

    @staticmethod
    def _cell(text: str, color: Optional[str]) -> Gtk.Label:
        label = Gtk.Label()
        label.set_xalign(0.0)
        label.set_wrap(True)
        # Effective values can contain long comma-separated algorithm lists.
        # WORD wrapping treats each as one enormous token and makes the window
        # grow horizontally when full comparison is enabled. Permit character
        # breaks and cap the label's natural width so the existing window size
        # and centered placement remain stable.
        label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        label.set_max_width_chars(48)
        label.set_hexpand(True)
        label.set_selectable(True)
        label.add_css_class("monospace")
        if not text:
            label.set_text("")
        elif color:
            label.set_markup(f'<span foreground="{color}">{GLib.markup_escape_text(text)}</span>')
        else:
            label.set_text(text)
        return label

    # ---- actions -----------------------------------------------------------

    def _on_edit_clicked(self, _button) -> None:
        app = None
        try:
            app = self._parent.get_application() if self._parent else None
        except Exception:
            app = None
        if app is None:
            try:
                app = self.get_application()
            except Exception:
                app = None
        if app is not None:
            try:
                app.activate_action("edit-ssh-config")
                return
            except Exception:
                logger.debug("edit-ssh-config action failed", exc_info=True)
        logger.warning("Could not launch SSH config editor from effective-config dialog")
