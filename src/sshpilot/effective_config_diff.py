"""Read-only window that shows, in plain language, how global SSH config
changes a connection.

The connection editor only shows a host's own ``Host`` block, but ``Host *``
wildcard blocks and ``Include``d files also apply at connect time. This window
lists, per setting, what the user configured versus what SSH actually uses —
split into "added by global config" and "overridden by global config" — so the
difference is legible without reading a diff. Purely informational; the only
action opens the existing SSH config editor.
"""

from __future__ import annotations

import logging
from gettext import gettext as _
from typing import Dict, List

from gi.repository import Gtk, Adw, GLib

try:
    from .shortcut_utils import install_esc_to_close
except Exception:  # pragma: no cover - helper is optional
    def install_esc_to_close(_window):
        return None

logger = logging.getLogger(__name__)


# Friendly names for the SSH directives users are most likely to see change.
# ssh -G lowercases keys; fall back to the canonical-ish keyword otherwise.
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


# Colors: what you set (blue), what SSH actually uses/adds (green), and a value
# your config set but a global rule discards (red, struck through).
_COLOR_YOURS = "#3584e4"
_COLOR_USES = "#26a269"
_COLOR_DROPPED = "#c01c28"


def _span(values: List[str], color: str) -> str:
    text = ', '.join(values) if values else _('(none)')
    return f'<span foreground="{color}">{GLib.markup_escape_text(text)}</span>'


class EffectiveConfigDiffWindow(Adw.Window):
    """Plain-language view of own-block vs. effective (global-applied) config."""

    __gtype_name__ = "SshPilotEffectiveConfigDiffWindow"

    def __init__(self, parent, host: str, changes: List[Dict[str, object]]) -> None:
        super().__init__()
        self._parent = parent
        self.set_transient_for(parent)
        # Non-modal on purpose: the "Edit SSH config…" button opens the editor
        # (transient for the main window); a modal grab here would block that
        # editor from receiving input or being closed.
        self.set_modal(False)
        self.set_title(_("Effective SSH Configuration"))
        self.set_default_size(640, 540)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()

        edit_button = Gtk.Button(label=_("Edit SSH config…"))
        edit_button.add_css_class("suggested-action")
        edit_button.set_tooltip_text(
            _("Open the SSH config editor to change the global rules")
        )
        edit_button.connect("clicked", self._on_edit_clicked)
        header.pack_start(edit_button)
        toolbar.add_top_bar(header)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.set_margin_top(18)
        content.set_margin_bottom(18)
        content.set_margin_start(18)
        content.set_margin_end(18)

        # Intro paragraph — normal body size (not the dim caption of a group description).
        intro = self._label(_(
            "Your global SSH configuration (for example a “Host *” block or an "
            "included file) changes what this connection actually uses. SSH "
            "applies the values below — not only what you entered in the editor."
        ), "body")
        content.append(intro)

        # Section heading (mirrors the default AdwPreferencesGroup title look).
        content.append(self._label(_("Settings changed by global config"), "heading"))

        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        listbox.add_css_class("boxed-list")

        for c in changes:
            # One row per changed setting: your value vs. what SSH actually uses.
            # Your value is red when a global rule discards it (overridden/
            # removed), blue when it is kept and only added to; SSH's value green.
            dropped = c.get('kind') in ('overridden', 'removed')
            yours = _span(list(c.get('own') or []),
                          _COLOR_DROPPED if dropped else _COLOR_YOURS)
            uses = _span(list(c.get('effective') or []), _COLOR_USES)

            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            box.set_margin_top(10)
            box.set_margin_bottom(10)
            box.set_margin_start(12)
            box.set_margin_end(12)
            box.append(self._label(_friendly_key(str(c['key'])), "title-4"))
            box.append(self._label(_("You set:"), "body"))
            box.append(self._value_label(yours))
            box.append(self._label(_("SSH uses:"), "body"))
            box.append(self._value_label(uses))

            row = Gtk.ListBoxRow()
            row.set_activatable(False)
            row.set_child(box)
            listbox.append(row)

        content.append(listbox)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        scrolled.set_child(content)

        toolbar.set_content(scrolled)
        self.set_content(toolbar)
        install_esc_to_close(self)

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
        """A monospace, colour-coded value (selectable so paths can be copied)."""
        label = Gtk.Label()
        label.set_markup(markup)
        label.set_xalign(0.0)
        label.set_wrap(True)
        label.set_selectable(True)
        label.add_css_class("monospace")
        return label

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
        logger.warning("Could not launch SSH config editor from diff window")
