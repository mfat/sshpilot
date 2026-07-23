"""Unified effective-SSH-config viewer.

The default summary explains each changed setting in plain language. An optional
full comparison shows the host block and the effective ``ssh -G`` output side by
side. Both the post-save warning and connection context menu use this window.
"""

from __future__ import annotations

import difflib
import logging
import os
import tempfile
import threading
from gettext import gettext as _
from typing import Dict, List, Optional, Tuple

from gi.repository import Gtk, Adw, GLib

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


def connection_config_data(connection) -> dict:
    """Build the format_ssh_config_entry input for a connection's own block.

    Shared by the viewer launcher and the background effective-config checker so
    both diff the exact same host block.
    """
    return {
        'nickname': getattr(connection, 'nickname', '') or getattr(connection, 'host', ''),
        'hostname': getattr(connection, 'hostname', '') or getattr(connection, 'host', ''),
        'username': getattr(connection, 'username', ''),
        'port': getattr(connection, 'port', 22),
        'auth_method': getattr(connection, 'auth_method', 0),
        'key_select_mode': getattr(connection, 'key_select_mode', 0),
        'keyfile': getattr(connection, 'keyfile', ''),
        'identity_files': getattr(connection, 'identity_files', None) or [],
        'certificate': getattr(connection, 'certificate', ''),
        'certificate_files': getattr(connection, 'certificate_files', None) or [],
        'x11_forwarding': getattr(connection, 'x11_forwarding', False),
        'proxy_jump': getattr(connection, 'proxy_jump', []) or [],
        'forward_agent': getattr(connection, 'forward_agent', False),
        'local_command': getattr(connection, 'local_command', ''),
        'remote_command': getattr(connection, 'remote_command', ''),
        'forwarding_rules': getattr(connection, 'forwarding_rules', []) or [],
        'extra_ssh_config': getattr(connection, 'extra_ssh_config', ''),
        # Agent / hardware-key sources and auth flags the writer also emits —
        # omitting them makes the rebuilt block differ from the real one.
        'identity_agent': getattr(connection, 'identity_agent', '') or '',
        'add_keys_to_agent': getattr(connection, 'add_keys_to_agent', '') or '',
        'pkcs11_provider': getattr(connection, 'pkcs11_provider', '') or '',
        'security_key_provider': getattr(connection, 'security_key_provider', '') or '',
        'pubkey_auth_no': getattr(connection, 'pubkey_auth_no', False),
        'pre_command': getattr(connection, 'pre_command', '') or '',
        'password': getattr(connection, 'password', '') or '',
    }


def saved_connection_block(connection_manager, connection, *,
                           host: Optional[str] = None,
                           fallback_data: Optional[dict] = None) -> str:
    """Return a saved connection's authored Host block.

    Reading the stanza back from its source file makes the saved block the
    canonical side of the comparison. Reformat from connection data only when
    the stanza cannot be read (for example while creating a new connection).
    """
    nickname = host or getattr(connection, 'nickname', '') or ''
    try:
        # Every concrete stanza for the alias (ssh merges repeated Host blocks),
        # not just the first — otherwise later directives read as global additions.
        lines = connection_manager.collect_host_block_lines(nickname)
        if lines:
            return '\n'.join(lines)
    except Exception:
        logger.debug("Could not read saved Host block for %s", nickname,
                     exc_info=True)

    data = (
        fallback_data
        if fallback_data is not None
        else connection_config_data(connection)
    )
    return connection_manager.format_ssh_config_entry(data)


def _compute(host: str, own_block: str, root_config: Optional[str],
             is_new: bool) -> Optional[dict]:
    """Resolve own-block vs. effective config (blocking; run off the main thread).

    For a saved host the real root config already contains its block, so we diff
    against it directly. For a not-yet-saved host we synthesize a config that is
    the current block followed by ``Include <root>``, so ssh still applies the
    root's ``Host *`` globals without a stale saved block skewing the result.
    """
    from .ssh_config_utils import diff_effective_config

    tmp_path = None
    try:
        full_config = root_config
        if is_new and root_config:
            fd, tmp_path = tempfile.mkstemp(prefix='.sshpilot-eff-', suffix='.conf')
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(f"{own_block}\nInclude {root_config}\n")
            full_config = tmp_path
        return diff_effective_config(host, full_config, own_block)
    except Exception:
        logger.debug("effective-config computation failed", exc_info=True)
        return None
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


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
    def for_connection(cls, parent, connection, connection_manager):
        """Open the viewer for a saved connection (e.g. from the sidebar menu).

        Builds the host's own block from the connection object and resolves the
        root config the real connection uses, then presents the two-pane viewer.
        """
        host = getattr(connection, 'nickname', '') or getattr(connection, 'host', '')
        own_block = saved_connection_block(connection_manager, connection)
        try:
            root_config = connection._resolve_config_override_path()
        except Exception:
            root_config = None
        dialog = cls(parent, host=host,
                     own_block=own_block, root_config=root_config, is_new=False)
        dialog.present()
        return dialog

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

        self._full_toggle = Gtk.ToggleButton(label=_("Show full comparison"))
        self._full_toggle.set_tooltip_text(
            _("Show the complete host-block and effective SSH configurations"))
        self._full_toggle.connect("toggled", lambda _b: self._render())
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
            self._show_spinner()
            threading.Thread(
                target=self._work, args=(host, own_block, root_config, is_new),
                name="effcfg-dialog", daemon=True,
            ).start()

    # ---- computation -------------------------------------------------------

    def _work(self, host, own_block, root_config, is_new):
        result = _compute(host, own_block, root_config, is_new)
        GLib.idle_add(self._on_computed, result)

    def _on_computed(self, result):
        self._computed = True
        if result:
            self._own_lines = list(result.get('own') or [])
            self._full_lines = list(result.get('full') or [])
            self._changes = list(result.get('changes') or [])
        self._render()
        return False

    # ---- rendering ---------------------------------------------------------

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
                "No differences — your global SSH configuration does not change "
                "this host. Toggle “Show full comparison” to inspect every setting."
            ))
            return

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.set_margin_top(18)
        content.set_margin_bottom(18)
        content.set_margin_start(18)
        content.set_margin_end(18)
        content.append(self._label(_(
            "Your global SSH configuration changes what this connection actually "
            "uses. SSH applies the effective values shown below."
        ), "body"))
        content.append(self._label(
            _("Settings changed by global config"), "heading"))

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
            box.append(self._label(_("You set:"), "body"))
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
        grid = Gtk.Grid(column_homogeneous=True, column_spacing=18, row_spacing=3)
        grid.set_margin_top(12)
        grid.set_margin_bottom(12)
        grid.set_margin_start(12)
        grid.set_margin_end(12)
        grid.attach(self._heading(_("Host block")), 0, 0, 1, 1)
        grid.attach(self._heading(_("Effective (SSH)")), 1, 0, 1, 1)

        for r, (left, right, kind) in enumerate(rows, start=1):
            left_color = _COLOR_REMOVED if kind in ('delete', 'replace') else None
            right_color = _COLOR_ADDED if kind in ('insert', 'replace') else None
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
