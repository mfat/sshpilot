"""Welcome page widget for sshPilot."""

import gi
import os
import re
import shlex
import logging

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')

from gi.repository import Gtk, Adw, Gdk, Gio, GLib

from gettext import gettext as _, ngettext

from .connection_manager import Connection
from .platform_utils import is_macos, get_ssh_dir

logger = logging.getLogger(__name__)


SSH_OPTIONS_EXPECTING_ARGUMENT = {
    "-b",
    "-B",
    "-c",
    "-D",
    "-E",
    "-e",
    "-F",
    "-I",
    "-J",
    "-L",
    "-l",
    "-m",
    "-O",
    "-o",
    "-p",
    "-Q",
    "-R",
    "-S",
    "-W",
    "-w",
}


# An address token is either an IPv6 literal in brackets [::1] or any
# sequence of characters that contains no unbracketed colon (hostname/IPv4).
_ADDR = r'(?:\[[^\]]*\]|[^\[\]:]+)'
_PORT = r'\d+'

# Matches [bind_addr:]port:host:hostport with full bracket-awareness.
_FORWARD_RE = re.compile(
    r'^(?:(' + _ADDR + r'):)?'   # optional bind_addr:
    r'(' + _PORT + r')'           # listen_port
    r':(' + _ADDR + r')'          # remote host
    r':(' + _PORT + r')$'         # remote port
)

# Matches [bind_addr:]port for -D (dynamic SOCKS proxy).
_DYNAMIC_RE = re.compile(
    r'^(?:(' + _ADDR + r'):)?'   # optional bind_addr:
    r'(' + _PORT + r')$'          # port
)


def _parse_forward_spec(spec: str, fwd_type: str):
    """Parse -L/-R spec [bind_addr:]port:host:hostport into a forwarding_rules dict.

    Handles IPv6 literals in brackets (e.g. [::1]:8080:localhost:80).
    Returns None if the spec cannot be parsed; callers should preserve the raw
    spec in unparsed_args so the rule is not silently lost.
    """
    m = _FORWARD_RE.match(spec)
    if not m:
        return None
    bind_addr, listen_port, remote_host, remote_port = m.groups()
    rule = {'type': fwd_type, 'enabled': True,
            'listen_addr': bind_addr or 'localhost', 'listen_port': int(listen_port)}
    if fwd_type == 'local':
        rule['remote_host'] = remote_host
        rule['remote_port'] = int(remote_port)
    else:
        rule['local_host'] = remote_host
        rule['local_port'] = int(remote_port)
    return rule


def _parse_dynamic_spec(spec: str):
    """Parse -D spec [bind_addr:]port into a forwarding_rules dict.

    Handles IPv6 bind addresses in brackets (e.g. [::1]:1080).
    Returns None if the spec cannot be parsed.
    """
    m = _DYNAMIC_RE.match(spec)
    if not m:
        return None
    bind_addr, port = m.groups()
    return {'type': 'dynamic', 'enabled': True,
            'listen_addr': bind_addr or 'localhost', 'listen_port': int(port)}


def _append_extra_config(data: dict, line: str) -> None:
    """Append a SSH-config-syntax 'Key value' line to extra_ssh_config."""
    existing = data.get("extra_ssh_config", "")
    data["extra_ssh_config"] = (existing + "\n" + line).lstrip("\n")


class WelcomePage(Gtk.Box):
    """Welcome page shown when no tabs are open — carousel-based layout."""

    # Ordered list of all carousel page definitions
    _PAGE_DEFS = [
        ('connections',       _('Connections'),        'network-server-symbolic'),
        ('quick-actions',     _('Quick Actions'),      'grid-large-symbolic'),
        ('keyboard-shortcuts',_('Keyboard Shortcuts'), 'preferences-desktop-keyboard-symbolic'),
        ('getting-started',   _('Getting Started'),    'help-browser-symbolic'),
        ('connection-groups', _('Connection Groups'),  'folder-symbolic'),
        ('tips-tricks',       _('Tips & Tricks'),      'dialog-information-symbolic'),
        ('recent-connections',_('Recent Connections'), 'document-open-recent-symbolic'),
        ('key-manager',       _('SSH Keys'),           'dialog-password-symbolic'),
        ('whats-new',         _('What\'s New'),        'software-update-available-symbolic'),
    ]

    def __init__(self, window) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self.connection_manager = window.connection_manager
        self.config = window.config
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_can_focus(False)

        # Track pin toggle buttons keyed by page_id
        self._pin_buttons: dict = {}

        # ── Header (fixed, not scrollable) ──────────────────────────────────
        from sshpilot import icon_utils
        header_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        header_box.set_halign(Gtk.Align.CENTER)
        header_box.set_margin_top(20)
        header_box.set_margin_bottom(8)
        header_box.set_can_focus(False)

        app_icon = icon_utils.new_image_from_icon_name('io.github.mfat.sshpilot')
        app_icon.set_pixel_size(56)
        app_icon.set_can_focus(False)
        header_box.append(app_icon)

        title_label = Gtk.Label(label=_('Welcome to SSH Pilot'))
        title_label.add_css_class('title-2')
        title_label.set_halign(Gtk.Align.CENTER)
        title_label.set_can_focus(False)
        header_box.append(title_label)

        self.append(header_box)

        # ── Carousel indicator dots ──────────────────────────────────────────
        self.carousel = Adw.Carousel()
        self.carousel.set_allow_scroll_wheel(False)  # avoid conflict with page scroll
        self.carousel.set_allow_long_swipes(True)
        self.carousel.set_spacing(16)
        self.carousel.set_hexpand(True)
        self.carousel.set_vexpand(True)
        self.carousel.set_can_focus(False)

        dots = Adw.CarouselIndicatorDots()
        dots.set_carousel(self.carousel)
        dots.set_margin_top(4)
        dots.set_margin_bottom(6)
        self.append(dots)
        self.append(self.carousel)

        # Determine page order based on pinned page
        pinned_id = self.config.get_setting('ui.startpage_pinned_page', None)
        page_order = list(self._PAGE_DEFS)
        if pinned_id:
            pinned_def = next((p for p in page_order if p[0] == pinned_id), None)
            if pinned_def:
                page_order.remove(pinned_def)
                page_order.insert(0, pinned_def)

        current_shortcuts = self._get_safe_current_shortcuts()

        self._page_widgets: dict = {}
        for page_id, page_title, page_icon in page_order:
            page_widget = self._build_page(page_id, page_title, page_icon, current_shortcuts)
            self._page_widgets[page_id] = page_widget
            self.carousel.append(page_widget)

        self._sync_pin_buttons()

    # ── Page factory ────────────────────────────────────────────────────────

    def _make_page_frame(self, page_id: str, title: str, icon_name: str):
        """Return (scroll_widget, inner_content_box) for a carousel page.

        Each page is a ScrolledWindow so it can scroll independently.
        The visible card frame sits inside the scroll area.
        """
        from sshpilot import icon_utils

        # Outer scrolled window — fills the carousel slot entirely
        page_scroll = Gtk.ScrolledWindow()
        page_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        page_scroll.set_hexpand(True)
        page_scroll.set_vexpand(True)
        page_scroll.set_can_focus(False)
        page_scroll.set_margin_start(12)
        page_scroll.set_margin_end(12)
        page_scroll.set_margin_top(4)
        page_scroll.set_margin_bottom(12)

        # Card frame centred with a reasonable max width
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        card.add_css_class('card')
        card.set_hexpand(True)
        card.set_vexpand(False)
        card.set_valign(Gtk.Align.START)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(900)
        clamp.set_tightening_threshold(600)
        clamp.set_child(card)
        clamp.set_hexpand(True)
        page_scroll.set_child(clamp)

        # Title bar inside the card
        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title_row.set_margin_start(16)
        title_row.set_margin_end(8)
        title_row.set_margin_top(14)
        title_row.set_margin_bottom(8)

        page_icon_widget = icon_utils.new_image_from_icon_name(icon_name)
        page_icon_widget.set_pixel_size(18)
        page_icon_widget.set_can_focus(False)
        title_row.append(page_icon_widget)

        page_title_label = Gtk.Label(label=title)
        page_title_label.add_css_class('title-4')
        page_title_label.set_halign(Gtk.Align.START)
        page_title_label.set_hexpand(True)
        page_title_label.set_can_focus(False)
        title_row.append(page_title_label)

        pin_btn = Gtk.ToggleButton()
        pin_btn.add_css_class('flat')
        pin_btn.set_valign(Gtk.Align.CENTER)
        pin_btn.set_tooltip_text(_('Pin this page as startup view'))
        pin_icon = icon_utils.new_image_from_icon_name('non-starred-symbolic')
        pin_icon.set_can_focus(False)
        pin_btn.set_child(pin_icon)
        pin_btn.connect('toggled', self._on_pin_toggled, page_id)
        title_row.append(pin_btn)
        self._pin_buttons[page_id] = (pin_btn, pin_icon)

        card.append(title_row)

        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        sep.set_can_focus(False)
        card.append(sep)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        inner.set_margin_start(16)
        inner.set_margin_end(16)
        inner.set_margin_top(16)
        inner.set_margin_bottom(20)
        card.append(inner)

        return page_scroll, inner

    def _build_page(self, page_id: str, title: str, icon_name: str, shortcuts: dict) -> Gtk.Widget:
        builders = {
            'connections': self._build_connections_page,
            'quick-actions': self._build_quick_actions_page,
            'keyboard-shortcuts': self._build_keyboard_shortcuts_page,
            'getting-started': self._build_getting_started_page,
            'connection-groups': self._build_connection_groups_page,
            'tips-tricks': self._build_tips_page,
            'recent-connections': self._build_recent_connections_page,
            'key-manager': self._build_key_manager_page,
            'whats-new': self._build_whats_new_page,
        }
        builder = builders.get(page_id)
        if builder:
            return builder(title, icon_name, shortcuts)
        outer, inner = self._make_page_frame(page_id, title, icon_name)
        inner.append(Gtk.Label(label=_('Coming soon')))
        return outer

    # ── Individual page builders ─────────────────────────────────────────────

    def _build_connections_page(self, title: str, icon_name: str, shortcuts: dict) -> Gtk.Widget:
        """Page 0: the classic action rows."""
        from sshpilot import icon_utils
        outer, inner = self._make_page_frame('connections', title, icon_name)

        getting_started_group = Adw.PreferencesGroup()
        getting_started_group.set_can_focus(False)
        inner.append(getting_started_group)

        def _row(row_title, icon, accel_key, on_activate):
            row = Adw.ActionRow()
            row.set_title(row_title)
            row.set_activatable(True)
            row.set_can_focus(False)
            img = icon_utils.new_image_from_icon_name(icon)
            img.set_can_focus(False)
            row.add_prefix(img)
            accel = self._get_action_accel_display(shortcuts, accel_key)
            if accel:
                lbl = Gtk.Label(label=accel)
                lbl.add_css_class('dim-label')
                lbl.set_can_focus(False)
                row.add_suffix(lbl)
            chevron = icon_utils.new_image_from_icon_name('pan-end-symbolic')
            chevron.set_can_focus(False)
            row.add_suffix(chevron)
            row.connect('activated', lambda *_: on_activate())
            return row

        getting_started_group.add(_row(
            _('Quick Connect'), 'network-server-symbolic', 'quick-connect',
            lambda: self.on_quick_connect_clicked(None)))
        getting_started_group.add(_row(
            _('Add a New Connection'), 'list-add-symbolic', 'new-connection',
            lambda: self.window.get_application().activate_action('new-connection')))
        getting_started_group.add(_row(
            _('View and Edit SSH Config'), 'document-edit-symbolic', 'edit-ssh-config',
            lambda: self.window.get_application().activate_action('edit-ssh-config')))
        getting_started_group.add(_row(
            _('Open Local Terminal'), 'utilities-terminal-symbolic', 'local-terminal',
            lambda: self.window.terminal_manager.show_local_terminal()))

        help_group = Adw.PreferencesGroup()
        help_group.set_can_focus(False)
        inner.append(help_group)

        help_group.add(_row(
            _('Keyboard Shortcuts'), 'preferences-desktop-keyboard-symbolic', 'shortcuts',
            lambda: self.window.show_shortcuts_window()))
        help_group.add(_row(
            _('Online Documentation'), 'help-browser-symbolic', 'help',
            lambda: self.open_online_help()))

        return outer

    def _build_quick_actions_page(self, title: str, icon_name: str, shortcuts: dict) -> Gtk.Widget:
        """Page 1: non-duplicate menu action buttons."""
        from sshpilot import icon_utils
        outer, inner = self._make_page_frame('quick-actions', title, icon_name)

        group = Adw.PreferencesGroup()
        group.set_can_focus(False)
        inner.append(group)

        def _action_row(row_title, icon, on_activate, subtitle=''):
            row = Adw.ActionRow()
            row.set_title(row_title)
            if subtitle:
                row.set_subtitle(subtitle)
            row.set_activatable(True)
            row.set_can_focus(False)
            img = icon_utils.new_image_from_icon_name(icon)
            img.set_can_focus(False)
            row.add_prefix(img)
            chevron = icon_utils.new_image_from_icon_name('pan-end-symbolic')
            chevron.set_can_focus(False)
            row.add_suffix(chevron)
            row.connect('activated', lambda *_: on_activate())
            return row

        group.add(_action_row(
            _('Copy Key to Server'), 'dialog-password-symbolic',
            lambda: self.window.get_application().activate_action('new-key'),
            _('Install your SSH public key on a remote host')))
        group.add(_action_row(
            _('Known Hosts Editor'), 'security-high-symbolic',
            lambda: self.window.activate_action('edit-known-hosts'),
            _('View and manage ~/.ssh/known_hosts')))
        group.add(_action_row(
            _('Broadcast Command'), 'network-transmit-receive-symbolic',
            lambda: self.window.get_application().activate_action('broadcast-command'),
            _('Send a command to all open terminals')))
        group.add(_action_row(
            _('Export Configuration'), 'document-save-symbolic',
            lambda: self.window.activate_action('export-config'),
            _('Back up your connections and settings')))
        group.add(_action_row(
            _('Import Configuration'), 'document-send-symbolic',
            lambda: self.window.activate_action('import-config'),
            _('Restore connections from a backup')))
        group.add(_action_row(
            _('Preferences'), 'preferences-system-symbolic',
            lambda: self.window.get_application().activate_action('preferences'),
            _('Customize SSH Pilot settings')))

        return outer

    def _build_keyboard_shortcuts_page(self, title: str, icon_name: str, shortcuts: dict) -> Gtk.Widget:
        """Page 2: keyboard shortcuts reference."""
        outer, inner = self._make_page_frame('keyboard-shortcuts', title, icon_name)
        from sshpilot import icon_utils

        shortcut_sections = [
            (_('General'), [
                ('quit',            _('Quit')),
                ('preferences',     _('Preferences')),
                ('shortcuts',       _('Keyboard Shortcuts')),
                ('help',            _('Documentation')),
            ]),
            (_('Connections'), [
                ('new-connection',  _('New Connection')),
                ('quick-connect',   _('Quick Connect')),
                ('edit-ssh-config', _('SSH Config Editor')),
                ('new-key',         _('Copy Key to Server')),
            ]),
            (_('Terminal'), [
                ('local-terminal',  _('Local Terminal')),
                ('broadcast-command', _('Broadcast Command')),
            ]),
        ]

        for section_title, items in shortcut_sections:
            group = Adw.PreferencesGroup()
            group.set_title(section_title)
            group.set_can_focus(False)
            inner.append(group)

            for action_name, action_label in items:
                accel = self._get_action_accel_display(shortcuts, action_name)
                row = Adw.ActionRow()
                row.set_title(action_label)
                row.set_can_focus(False)
                if accel:
                    accel_lbl = Gtk.Label(label=accel)
                    accel_lbl.add_css_class('dim-label')
                    accel_lbl.add_css_class('monospace')
                    accel_lbl.set_can_focus(False)
                    row.add_suffix(accel_lbl)
                else:
                    na_lbl = Gtk.Label(label=_('—'))
                    na_lbl.add_css_class('dim-label')
                    na_lbl.set_can_focus(False)
                    row.add_suffix(na_lbl)
                group.add(row)

        return outer

    def _build_getting_started_page(self, title: str, icon_name: str, shortcuts: dict) -> Gtk.Widget:
        """Page 3: getting started guide."""
        from sshpilot import icon_utils
        outer, inner = self._make_page_frame('getting-started', title, icon_name)

        group = Adw.PreferencesGroup()
        group.set_can_focus(False)
        inner.append(group)

        steps = [
            ('list-add-symbolic',
             _('Add a Connection'),
             _('Click + in the sidebar or choose New Connection from the menu')),
            ('network-server-symbolic',
             _('Connect'),
             _('Double-click a connection or press Enter to open a terminal tab')),
            ('input-keyboard-symbolic',
             _('Quick Connect'),
             _('Use Quick Connect (Ctrl+Alt+C) for one-off SSH commands')),
            ('folder-symbolic',
             _('Organise with Groups'),
             _('Right-click the sidebar to create groups and drag connections into them')),
            ('folder-remote-symbolic',
             _('Remote File Manager'),
             _('Right-click an active connection to open the built-in SFTP file manager')),
        ]

        for i, (step_icon, step_title, step_desc) in enumerate(steps, start=1):
            row = Adw.ActionRow()
            row.set_title(f'{i}. {step_title}')
            row.set_subtitle(step_desc)
            row.set_can_focus(False)
            img = icon_utils.new_image_from_icon_name(step_icon)
            img.set_can_focus(False)
            row.add_prefix(img)
            group.add(row)

        return outer

    def _build_connection_groups_page(self, title: str, icon_name: str, shortcuts: dict) -> Gtk.Widget:
        """Page 4: connection groups overview."""
        from sshpilot import icon_utils
        outer, inner = self._make_page_frame('connection-groups', title, icon_name)

        try:
            all_groups = self.window.group_manager.get_all_groups()
        except Exception:
            all_groups = []

        if not all_groups:
            empty_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            empty_box.set_halign(Gtk.Align.CENTER)
            empty_box.set_valign(Gtk.Align.CENTER)
            empty_box.set_vexpand(True)
            empty_icon = icon_utils.new_image_from_icon_name('folder-symbolic')
            empty_icon.set_pixel_size(48)
            empty_icon.add_css_class('dim-label')
            empty_box.append(empty_icon)
            empty_lbl = Gtk.Label(label=_('No groups yet'))
            empty_lbl.add_css_class('title-3')
            empty_lbl.add_css_class('dim-label')
            empty_box.append(empty_lbl)
            sub_lbl = Gtk.Label(label=_('Create groups to organise your connections'))
            sub_lbl.add_css_class('dim-label')
            sub_lbl.set_wrap(True)
            sub_lbl.set_justify(Gtk.Justification.CENTER)
            empty_box.append(sub_lbl)
            create_btn = Gtk.Button(label=_('Create Group'))
            create_btn.add_css_class('suggested-action')
            create_btn.add_css_class('pill')
            create_btn.set_halign(Gtk.Align.CENTER)
            create_btn.connect('clicked', lambda *_: self.window.activate_action('create-group'))
            empty_box.append(create_btn)
            inner.append(empty_box)
        else:
            group = Adw.PreferencesGroup()
            group.set_can_focus(False)
            inner.append(group)

            for group_info in all_groups:
                gname = group_info.get('name', _('Unnamed Group'))
                gid = group_info.get('id', '')
                try:
                    gm = self.window.group_manager
                    conn_count = sum(1 for v in gm.connections.values() if v == gid)
                except Exception:
                    conn_count = 0

                row = Adw.ActionRow()
                row.set_title(gname)
                row.set_subtitle(
                    ngettext('%d connection', '%d connections', conn_count) % conn_count
                    if conn_count else _('Empty group'))
                row.set_can_focus(False)
                gicon = icon_utils.new_image_from_icon_name('folder-symbolic')
                gicon.set_can_focus(False)
                row.add_prefix(gicon)
                group.add(row)

        return outer

    def _build_tips_page(self, title: str, icon_name: str, shortcuts: dict) -> Gtk.Widget:
        """Page 5: tips & tricks."""
        from sshpilot import icon_utils
        outer, inner = self._make_page_frame('tips-tricks', title, icon_name)

        group = Adw.PreferencesGroup()
        group.set_can_focus(False)
        inner.append(group)

        tips = [
            ('view-list-symbolic',
             _('Drag to Reorder'),
             _('Drag connections in the sidebar to rearrange them')),
            ('open-menu-symbolic',
             _('Right-Click Menu'),
             _('Right-click any connection for connect, edit, delete and more')),
            ('view-dual-symbolic',
             _('Split View'),
             _('Open a second terminal side-by-side using the split view button')),
            ('network-transmit-receive-symbolic',
             _('Broadcast to All Terminals'),
             _('Use Broadcast Command to send the same input to every open terminal')),
            ('document-save-symbolic',
             _('Back Up Your Config'),
             _('Export your connections via the Import/Export menu item')),
            ('tag-symbolic',
             _('Color-Code Groups'),
             _('Assign a colour to each group for quick visual identification')),
        ]

        for tip_icon, tip_title, tip_desc in tips:
            row = Adw.ActionRow()
            row.set_title(tip_title)
            row.set_subtitle(tip_desc)
            row.set_can_focus(False)
            img = icon_utils.new_image_from_icon_name(tip_icon)
            img.set_can_focus(False)
            row.add_prefix(img)
            group.add(row)

        return outer

    def _build_recent_connections_page(self, title: str, icon_name: str, shortcuts: dict) -> Gtk.Widget:
        """Page 6: recently used connections."""
        from sshpilot import icon_utils
        outer, inner = self._make_page_frame('recent-connections', title, icon_name)

        try:
            meta_all = self.config.get_setting('connections_meta', {})
            all_connections = self.connection_manager.get_connections()
            conn_map = {c.nickname: c for c in all_connections}

            recent = []
            for nickname, meta in meta_all.items():
                if isinstance(meta, dict) and 'last_connected' in meta:
                    conn = conn_map.get(nickname)
                    if conn:
                        recent.append((meta['last_connected'], nickname, conn))

            recent.sort(key=lambda x: x[0], reverse=True)
            recent = recent[:8]
        except Exception:
            recent = []

        if not recent:
            empty_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            empty_box.set_halign(Gtk.Align.CENTER)
            empty_icon = icon_utils.new_image_from_icon_name('document-open-recent-symbolic')
            empty_icon.set_pixel_size(48)
            empty_icon.add_css_class('dim-label')
            empty_box.append(empty_icon)
            empty_lbl = Gtk.Label(label=_('No recent connections'))
            empty_lbl.add_css_class('dim-label')
            empty_box.append(empty_lbl)
            inner.append(empty_box)
        else:
            group = Adw.PreferencesGroup()
            group.set_can_focus(False)
            inner.append(group)

            for _ts, nickname, conn in recent:
                row = Adw.ActionRow()
                row.set_title(nickname)
                host = getattr(conn, 'host', '') or getattr(conn, 'hostname', '')
                if host and host != nickname:
                    row.set_subtitle(host)
                row.set_activatable(True)
                row.set_can_focus(False)
                img = icon_utils.new_image_from_icon_name('network-server-symbolic')
                img.set_can_focus(False)
                row.add_prefix(img)
                chevron = icon_utils.new_image_from_icon_name('pan-end-symbolic')
                chevron.set_can_focus(False)
                row.add_suffix(chevron)
                row.connect('activated', lambda *_, c=conn: self.window.terminal_manager.connect_to_host(c))
                group.add(row)

        return outer

    def _build_key_manager_page(self, title: str, icon_name: str, shortcuts: dict) -> Gtk.Widget:
        """Page 7: SSH key overview."""
        from sshpilot import icon_utils
        outer, inner = self._make_page_frame('key-manager', title, icon_name)

        try:
            from .platform_utils import get_ssh_dir
            ssh_dir = get_ssh_dir()
            key_files = []
            if ssh_dir and os.path.isdir(ssh_dir):
                for fname in sorted(os.listdir(ssh_dir)):
                    fpath = os.path.join(ssh_dir, fname)
                    if (os.path.isfile(fpath)
                            and not fname.endswith('.pub')
                            and not fname.startswith('.')
                            and fname not in ('known_hosts', 'config', 'authorized_keys')):
                        pub_path = fpath + '.pub'
                        key_files.append((fname, os.path.exists(pub_path)))
        except Exception:
            key_files = []

        group = Adw.PreferencesGroup()
        group.set_title(_('Keys in ~/.ssh'))
        group.set_can_focus(False)
        inner.append(group)

        if key_files:
            for key_name, has_pub in key_files[:8]:
                row = Adw.ActionRow()
                row.set_title(key_name)
                row.set_subtitle(_('Public key present') if has_pub else _('No public key found'))
                row.set_can_focus(False)
                img = icon_utils.new_image_from_icon_name('dialog-password-symbolic')
                img.set_can_focus(False)
                row.add_prefix(img)
                group.add(row)
        else:
            row = Adw.ActionRow()
            row.set_title(_('No keys found'))
            row.set_subtitle(_('No private keys found in ~/.ssh'))
            row.set_can_focus(False)
            group.add(row)

        copy_key_btn = Gtk.Button(label=_('Copy Key to Server…'))
        copy_key_btn.add_css_class('suggested-action')
        copy_key_btn.add_css_class('pill')
        copy_key_btn.set_halign(Gtk.Align.CENTER)
        copy_key_btn.set_margin_top(8)
        copy_key_btn.connect('clicked',
            lambda *_: self.window.get_application().activate_action('new-key'))
        inner.append(copy_key_btn)

        return outer

    def _build_whats_new_page(self, title: str, icon_name: str, shortcuts: dict) -> Gtk.Widget:
        """Page 8: what's new / version info."""
        from sshpilot import icon_utils
        from sshpilot import __version__
        outer, inner = self._make_page_frame('whats-new', title, icon_name)

        version_row = Adw.ActionRow()
        version_row.set_title(_('Current Version'))
        version_row.set_subtitle(f'SSH Pilot {__version__}')
        version_row.set_can_focus(False)
        img = icon_utils.new_image_from_icon_name('software-update-available-symbolic')
        img.set_can_focus(False)
        version_row.add_prefix(img)

        group = Adw.PreferencesGroup()
        group.set_title(_('Recent Highlights'))
        group.set_can_focus(False)
        group.add(version_row)
        inner.append(group)

        highlights = [
            ('view-dual-symbolic',
             _('Split View Terminals'),
             _('Open two terminals side by side in the same window')),
            ('folder-remote-symbolic',
             _('Built-in SFTP File Manager'),
             _('Transfer files without leaving the app')),
            ('tag-symbolic',
             _('Connection Groups'),
             _('Organise connections with colour-coded groups')),
            ('preferences-desktop-keyboard-symbolic',
             _('Customisable Shortcuts'),
             _('Rebind any keyboard shortcut from Preferences')),
            ('network-transmit-receive-symbolic',
             _('Broadcast Command'),
             _('Type once, send to all open terminals simultaneously')),
        ]

        hl_group = Adw.PreferencesGroup()
        hl_group.set_can_focus(False)
        inner.append(hl_group)

        for hl_icon, hl_title, hl_desc in highlights:
            row = Adw.ActionRow()
            row.set_title(hl_title)
            row.set_subtitle(hl_desc)
            row.set_can_focus(False)
            img = icon_utils.new_image_from_icon_name(hl_icon)
            img.set_can_focus(False)
            row.add_prefix(img)
            hl_group.add(row)

        changelog_btn = Gtk.Button(label=_('View Full Changelog'))
        changelog_btn.add_css_class('pill')
        changelog_btn.set_halign(Gtk.Align.CENTER)
        changelog_btn.set_margin_top(8)
        changelog_btn.connect('clicked', lambda *_: self.open_online_help())
        inner.append(changelog_btn)

        return outer

    # ── Pin mechanism ────────────────────────────────────────────────────────

    def _on_pin_toggled(self, btn: Gtk.ToggleButton, page_id: str):
        from sshpilot import icon_utils
        if btn.get_active():
            self.config.set_setting('ui.startpage_pinned_page', page_id)
        else:
            current = self.config.get_setting('ui.startpage_pinned_page', None)
            if current == page_id:
                self.config.set_setting('ui.startpage_pinned_page', None)
        self._sync_pin_buttons()

    def _sync_pin_buttons(self):
        from sshpilot import icon_utils
        pinned_id = self.config.get_setting('ui.startpage_pinned_page', None)
        for pid, (btn, icon_widget) in self._pin_buttons.items():
            is_pinned = (pid == pinned_id)
            # Block signal to avoid recursion
            btn.handler_block_by_func(self._on_pin_toggled)
            btn.set_active(is_pinned)
            btn.handler_unblock_by_func(self._on_pin_toggled)
            new_icon_name = 'starred-symbolic' if is_pinned else 'non-starred-symbolic'
            icon_widget.set_from_icon_name(new_icon_name)
            btn.set_tooltip_text(
                _('Unpin this page') if is_pinned else _('Pin this page as startup view'))

    def show_sidebar_hint(self):
        """Show a hint about using the sidebar to manage connections"""
        toast = Adw.Toast.new(_('Use the sidebar to add and manage your SSH connections'))
        toast.set_timeout(3)
        if hasattr(self.window, 'add_toast'):
            self.window.add_toast(toast)
        else:
            # Fallback for older API
            overlay = self.window.get_child()
            if isinstance(overlay, Adw.ToastOverlay):
                overlay.add_toast(toast)

    def _get_safe_current_shortcuts(self):
        """Safely get current shortcuts including user customizations from the app.
        Returns a dict: { action_name: [accel, ...] }
        """
        shortcuts = {}
        try:
            app = self.window.get_application()
            if not app:
                return shortcuts

            # Get default registered shortcuts if available
            if hasattr(app, 'get_registered_shortcut_defaults'):
                defaults = app.get_registered_shortcut_defaults()
                if isinstance(defaults, dict):
                    shortcuts.update(defaults)

            # Apply user overrides from config if available
            if hasattr(app, 'config') and app.config:
                for action_name in list(shortcuts.keys()):
                    try:
                        override = app.config.get_shortcut_override(action_name)
                        if override is not None:
                            if override:
                                shortcuts[action_name] = override
                            else:
                                shortcuts.pop(action_name, None)
                    except Exception:
                        continue
        except Exception:
            pass
        return shortcuts

    def _format_accelerator_display(self, accel: str) -> str:
        """Convert a GTK accelerator like '<primary><Shift>comma' to a
        platform-friendly display like '⌘+⇧+,' or 'Ctrl+Shift+,'.
        Robust against mixed case/order like '<shift><ctrl>u'.
        """
        if not accel:
            return ''
        import re
        mac = is_macos()
        primary = '⌘' if mac else 'Ctrl'
        shift_lbl = '⇧' if mac else 'Shift'
        alt_lbl = '⌥' if mac else 'Alt'

        s = accel.strip()
        # Extract all <token> in order
        tokens = [m.group(1).lower() for m in re.finditer(r'<([^>]+)>', s)]
        # Remove all <...> to get the key part
        key = re.sub(r'<[^>]+>', '', s).strip()

        # Map tokens to normalized set
        mods = set()
        for t in tokens:
            if t in ('primary', 'meta', 'cmd', 'command'):  # treat as primary
                mods.add('primary')
            elif t in ('ctrl', 'control'):
                mods.add('primary')  # normalize to primary for display
            elif t == 'shift':
                mods.add('shift')
            elif t == 'alt':
                mods.add('alt')

        # Build parts in consistent order
        parts = []
        if 'primary' in mods:
            parts.append(primary)
        if 'shift' in mods:
            parts.append(shift_lbl)
        if 'alt' in mods:
            parts.append(alt_lbl)

        # Map common key names
        key_map = {
            'comma': ',',
            'slash': '/',
            'backslash': '\\',
            'period': '.',
            'space': 'Space',
        }
        key_disp = key_map.get(key.lower(), key)
        if len(key_disp) == 1:
            key_disp = key_disp.upper()
        if key_disp:
            parts.append(key_disp)

        disp = '+'.join(parts)
        # Fallback safety: strip any lingering angle brackets
        disp = re.sub(r'<[^>]+>', '', disp)
        return disp

    def _get_action_accel_display(self, shortcuts: dict, action_name: str) -> str:
        """Get the first accelerator for an action and format it for display."""
        try:
            accels = shortcuts.get(action_name)
            if not accels:
                return ''
            # If it's a list, use the first; if it's a string, use directly
            accel = accels[0] if isinstance(accels, (list, tuple)) else accels
            return self._format_accelerator_display(accel)
        except Exception:
            return ''


    # Quick connect handlers


    def on_quick_connect_clicked(self, button):
        """Open quick connect dialog"""
        dialog = QuickConnectDialog(self.window)
        dialog.present()

    def open_online_help(self):
        """Open online help documentation"""
        logger.debug("open_online_help called")
        import webbrowser
        try:
            webbrowser.open('https://github.com/mfat/sshpilot/wiki')
            logger.debug("Successfully opened browser")
        except Exception as e:
            logger.error(f"Failed to open browser: {e}")
            # Fallback: show a dialog with the URL
            try:
                dialog = Adw.MessageDialog(
                    transient_for=self.window,
                    modal=True,
                    heading=_("Online Help"),
                    body=_("Visit the SSH Pilot documentation at:\nhttps://github.com/mfat/sshpilot/wiki")
                )
                dialog.add_response("ok", _("OK"))
                dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
                dialog.set_default_response("ok")
                dialog.set_close_response("ok")
                dialog.present()
                logger.debug("Showed fallback help dialog")
            except Exception as e2:
                logger.error(f"Failed to show help dialog: {e2}", exc_info=True)
    
    def _parse_ssh_command(self, command_text):
        """Parse SSH command text and extract connection parameters"""
        try:
            raw_command = command_text.strip()

            # Handle simple user@host format (backward compatibility)
            if not raw_command.startswith('ssh') and '@' in raw_command and ' ' not in raw_command:
                username, host = raw_command.split('@', 1)
                return {
                    "nickname": host,
                    "host": host,
                    "username": username,
                    "port": 22,
                    "auth_method": 0,  # Default to key-based auth
                    "key_select_mode": 0,  # Try all keys
                    "quick_connect_command": "",
                    "unparsed_args": [],
                }

            quick_connect_command = raw_command if raw_command.startswith('ssh') else ""
            working_text = raw_command

            # Parse full SSH command
            # Remove 'ssh' prefix if present
            if working_text.startswith('ssh '):
                working_text = working_text[4:]
            elif working_text.startswith('ssh'):
                working_text = working_text[3:]

            # Use shlex to properly parse the command with quoted arguments
            try:
                args = shlex.split(working_text)
            except ValueError:
                # If shlex fails, fall back to simple split
                args = working_text.split()

            connection_data = {
                "nickname": "",
                "host": "",
                "username": "",
                "port": 22,
                "auth_method": 0,
                "key_select_mode": 0,
                "keyfile": "",
                "certificate": "",
                "x11_forwarding": False,
                "forwarding_rules": [],
                "proxy_jump": [],
                "forward_agent": False,
                "extra_ssh_config": "",
                "quick_connect_command": quick_connect_command,
                "unparsed_args": [],
            }

            i = 0
            while i < len(args):
                arg = args[i]

                if arg == '-p' and i + 1 < len(args):
                    try:
                        connection_data["port"] = int(args[i + 1])
                        i += 2
                        continue
                    except ValueError:
                        pass
                elif arg == '-i' and i + 1 < len(args):
                    connection_data["keyfile"] = args[i + 1]
                    connection_data["key_select_mode"] = 2
                    i += 2
                    continue
                elif arg == '-o' and i + 1 < len(args):
                    option = args[i + 1]
                    parsed = option.split('=', 1)
                    if len(parsed) == 2:
                        key, value = parsed
                        key_lower = key.lower()
                        value = value.strip()
                        if key_lower == 'user':
                            connection_data["username"] = value
                        elif key_lower == 'port':
                            try:
                                connection_data["port"] = int(value)
                            except ValueError:
                                pass
                        elif key_lower == 'identityfile':
                            connection_data["keyfile"] = value
                            connection_data["key_select_mode"] = 2
                        elif key_lower == 'identitiesonly':
                            if value.lower() in ('yes', 'true', '1', 'on'):
                                connection_data["key_select_mode"] = 1
                            elif value.lower() in ('no', 'false', '0', 'off') and connection_data.get("keyfile"):
                                connection_data["key_select_mode"] = 2
                        elif key_lower == 'forwardagent':
                            connection_data["forward_agent"] = value.lower() in ('yes', 'true', '1', 'on')
                        else:
                            _append_extra_config(connection_data, f"{key} {value}")
                    i += 2
                    continue
                elif arg.startswith('-o') and '=' in arg[2:]:
                    key, value = arg[2:].split('=', 1)
                    key_lower = key.lower()
                    value = value.strip()
                    if key_lower == 'identityfile':
                        connection_data["keyfile"] = value
                        connection_data["key_select_mode"] = 2
                    elif key_lower == 'identitiesonly':
                        if value.lower() in ('yes', 'true', '1', 'on'):
                            connection_data["key_select_mode"] = 1
                        elif value.lower() in ('no', 'false', '0', 'off') and connection_data.get("keyfile"):
                            connection_data["key_select_mode"] = 2
                    elif key_lower == 'user':
                        connection_data["username"] = value
                    elif key_lower == 'port':
                        try:
                            connection_data["port"] = int(value)
                        except ValueError:
                            pass
                    elif key_lower == 'forwardagent':
                        connection_data["forward_agent"] = value.lower() in ('yes', 'true', '1', 'on')
                    else:
                        _append_extra_config(connection_data, f"{key} {value}")
                    i += 1
                    continue
                elif arg == '-X':
                    connection_data["x11_forwarding"] = True
                    i += 1
                    continue
                elif arg == '-A':
                    connection_data["forward_agent"] = True
                    i += 1
                    continue
                elif arg == '-C':
                    _append_extra_config(connection_data, "Compression yes")
                    i += 1
                    continue
                elif arg == '-4':
                    _append_extra_config(connection_data, "AddressFamily inet")
                    i += 1
                    continue
                elif arg == '-6':
                    _append_extra_config(connection_data, "AddressFamily inet6")
                    i += 1
                    continue
                elif arg == '-J' and i + 1 < len(args):
                    connection_data["proxy_jump"] = [
                        h.strip() for h in args[i + 1].split(',') if h.strip()
                    ]
                    i += 2
                    continue
                elif arg == '-L' and i + 1 < len(args):
                    rule = _parse_forward_spec(args[i + 1], 'local')
                    if rule:
                        connection_data["forwarding_rules"].append(rule)
                    i += 2
                    continue
                elif arg == '-R' and i + 1 < len(args):
                    rule = _parse_forward_spec(args[i + 1], 'remote')
                    if rule:
                        connection_data["forwarding_rules"].append(rule)
                    i += 2
                    continue
                elif arg == '-D' and i + 1 < len(args):
                    rule = _parse_dynamic_spec(args[i + 1])
                    if rule:
                        connection_data["forwarding_rules"].append(rule)
                    i += 2
                    continue
                elif arg.startswith('-p'):
                    try:
                        connection_data["port"] = int(arg[2:])
                        i += 1
                        continue
                    except ValueError:
                        pass
                elif arg.startswith('-i'):
                    connection_data["keyfile"] = arg[2:]
                    connection_data["key_select_mode"] = 2
                    i += 1
                    continue
                elif not arg.startswith('-'):
                    if not connection_data["host"]:
                        if '@' in arg:
                            username, host = arg.split('@', 1)
                            connection_data["username"] = username
                            connection_data["host"] = host
                            connection_data["nickname"] = host
                        else:
                            connection_data["host"] = arg
                            connection_data["nickname"] = arg
                    else:
                        connection_data["unparsed_args"].append(arg)
                    i += 1
                else:
                    option_key = arg
                    attached_value = ""
                    if option_key.startswith('--'):
                        option_key, _sep, attached_value = option_key.partition('=')
                    elif option_key.startswith('-') and len(option_key) > 2:
                        option_key, attached_value = option_key[:2], option_key[2:]

                    if option_key in SSH_OPTIONS_EXPECTING_ARGUMENT:
                        if attached_value:
                            i += 1
                        elif i + 1 < len(args) and not args[i + 1].startswith('-'):
                            i += 2
                        else:
                            i += 1
                    else:
                        i += 1
                    continue
            

            # Validate that we have at least a host
            if not connection_data["host"]:
                return None

            if connection_data.get("keyfile") and connection_data.get("key_select_mode", 0) == 0:
                connection_data["key_select_mode"] = 2

            return connection_data

        except Exception:
            # If parsing fails, try simple fallback
            if '@' in command_text:
                try:
                    username, host = command_text.split('@', 1)
                    return {
                        "nickname": host,
                        "host": host,
                        "username": username,
                        "port": 22,
                        "auth_method": 0,
                        "key_select_mode": 0,
                        "quick_connect_command": "",
                        "unparsed_args": [],
                    }
                except Exception:
                    pass
            return None


class QuickConnectDialog(Adw.MessageDialog):
    """Modal dialog for quick SSH connection"""

    def __init__(self, parent_window):
        super().__init__()

        self.parent_window = parent_window
        self._selected_keyfile = ""

        self.set_modal(True)
        self.set_transient_for(parent_window)
        self.set_title(_("Quick Connect"))

        content_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content_area.set_margin_top(12)
        content_area.set_margin_bottom(12)
        content_area.set_margin_start(12)
        content_area.set_margin_end(12)

        # Inline error banner (hidden until validation fails)
        self.error_label = Gtk.Label()
        self.error_label.set_halign(Gtk.Align.START)
        self.error_label.set_wrap(True)
        self.error_label.set_visible(False)
        self.error_label.add_css_class("error")
        content_area.append(self.error_label)

        description = Gtk.Label()
        description.set_text(_("Enter SSH command or connection details:"))
        description.set_halign(Gtk.Align.START)
        content_area.append(description)

        self.entry = Gtk.Entry()
        self.entry.set_placeholder_text("ssh -p 2222 user@host")
        self.entry.set_hexpand(True)
        self.entry.connect('activate', self.on_connect)
        self.entry.connect('changed', self._on_entry_changed)
        content_area.append(self.entry)

        # Auth fields group
        prefs_group = Adw.PreferencesGroup()

        self.password_row = Adw.PasswordEntryRow(title=_("Password (optional)"))
        prefs_group.add(self.password_row)

        self.keyfile_row = Adw.ActionRow()
        self.keyfile_row.set_title(_("Key File (optional)"))
        self.keyfile_row.set_subtitle(_("No key file selected"))
        browse_button = Gtk.Button(label=_("Browse…"))
        browse_button.set_valign(Gtk.Align.CENTER)
        browse_button.connect('clicked', self._browse_for_key_file)
        self.keyfile_row.add_suffix(browse_button)
        prefs_group.add(self.keyfile_row)

        content_area.append(prefs_group)

        self.set_extra_child(content_area)

        self.add_response("cancel", _("Cancel"))
        self.add_response("save", _("Save Connection…"))
        self.add_response("connect", _("Connect"))
        self.set_response_appearance("connect", Adw.ResponseAppearance.SUGGESTED)

        self.connect('response', self.on_response)
        self.entry.grab_focus()

    def _on_entry_changed(self, entry):
        if self.error_label.get_visible():
            self.error_label.set_visible(False)

    def _show_error(self, message: str):
        self.error_label.set_text(message)
        self.error_label.set_visible(True)

    def on_response(self, dialog, response):
        if response == "connect":
            self.on_connect()
        elif response == "save":
            self._on_save_connection()
        else:
            self.destroy()

    def on_connect(self, *args):
        text = self.entry.get_text().strip()
        if not text:
            return

        result = self._parse_ssh_command(text)
        if result is None:
            self._show_error(_("Could not parse command. Use: ssh user@host or user@host"))
            return
        if "error" in result:
            self._show_error(result["error"])
            return

        errors = self._validate_parsed(result)
        if errors:
            self._show_error("\n".join(errors))
            return

        password = self.password_row.get_text()
        if password:
            result["password"] = password
            result["auth_method"] = 1
        if self._selected_keyfile:
            result["keyfile"] = self._selected_keyfile
            result["key_select_mode"] = 2

        connection = Connection(result)
        self.parent_window.terminal_manager.connect_to_host(connection, force_new=False)
        self.destroy()

    def _on_save_connection(self):
        text = self.entry.get_text().strip()
        if not text:
            return

        result = self._parse_ssh_command(text)
        if result is None:
            self._show_error(_("Could not parse command. Use: ssh user@host or user@host"))
            return
        if "error" in result:
            self._show_error(result["error"])
            return

        errors = self._validate_parsed(result)
        if errors:
            self._show_error("\n".join(errors))
            return

        password = self.password_row.get_text()
        if password:
            result["password"] = password
            result["auth_method"] = 1
        if self._selected_keyfile:
            result["keyfile"] = self._selected_keyfile
            result["key_select_mode"] = 2

        connection = Connection(result)
        self.destroy()

        from .connection_dialog import ConnectionDialog
        conn_dialog = ConnectionDialog(
            self.parent_window,
            connection=connection,
            connection_manager=getattr(self.parent_window, 'connection_manager', None),
        )
        # load_connection_data() already ran inside __init__ to pre-fill the form.
        # Mark as new so window.on_connection_saved takes the new-connection branch
        # that appends to connection_manager.connections instead of the edit branch
        # that tries to update a transient object not registered in the manager.
        conn_dialog.is_editing = False
        conn_dialog.connect('connection-saved', self._on_connection_saved)
        conn_dialog.present()

    def _on_connection_saved(self, dialog, connection_data):
        if hasattr(self.parent_window, 'on_connection_saved'):
            self.parent_window.on_connection_saved(dialog, connection_data)

    def _browse_for_key_file(self, button):
        dialog = Gtk.FileDialog(title=_("Select SSH Key File"))
        ssh_dir = get_ssh_dir()
        if ssh_dir and os.path.isdir(ssh_dir):
            dialog.set_initial_folder(Gio.File.new_for_path(ssh_dir))
        dialog.open(self, None, self._on_key_file_selected)

    def _on_key_file_selected(self, dialog, result):
        try:
            gfile = dialog.open_finish(result)
            self._selected_keyfile = gfile.get_path()
            self.keyfile_row.set_subtitle(self._selected_keyfile)
        except Exception:
            pass

    def _validate_parsed(self, connection_data: dict) -> list:
        from .connection_dialog import SSHConnectionValidator
        validator = SSHConnectionValidator()
        errors = []

        hostname = connection_data.get("hostname") or connection_data.get("host", "")
        result = validator.validate_hostname(hostname)
        if not result.is_valid:
            errors.append(result.message)

        port = connection_data.get("port", 22)
        result = validator.validate_port(str(port))
        if not result.is_valid:
            errors.append(result.message)

        return errors

    def _parse_ssh_command(self, command_text):
        """Parse an SSH command string into connection parameters.

        Returns a connection data dict, a dict with an "error" key for user-visible
        errors, or None when the input cannot be parsed at all.
        Only accepts bare user@host or commands starting with 'ssh'.
        """
        try:
            raw_command = command_text.strip()

            # Allow bare user@host without the 'ssh' prefix
            if '@' in raw_command and ' ' not in raw_command and not raw_command.startswith('ssh'):
                parts = raw_command.split('@', 1)
                username, host = parts[0], parts[1]
                if not host:
                    return None
                return {
                    "nickname": host,
                    "host": host,
                    "hostname": host,
                    "username": username,
                    "port": 22,
                    "auth_method": 0,
                    "key_select_mode": 0,
                    "quick_connect_command": "",
                    "unparsed_args": [],
                }

            # For any other input the first token must be exactly "ssh"
            try:
                tokens = shlex.split(raw_command)
            except ValueError:
                tokens = raw_command.split()

            if not tokens:
                return None

            if tokens[0] != "ssh":
                return {"error": _("Only SSH commands are allowed. Example: ssh user@host")}

            quick_connect_command = raw_command
            args = tokens[1:]

            connection_data = {
                "nickname": "",
                "host": "",
                "hostname": "",
                "username": "",
                "port": 22,
                "auth_method": 0,
                "key_select_mode": 0,
                "keyfile": "",
                "certificate": "",
                "x11_forwarding": False,
                "forwarding_rules": [],
                "proxy_jump": [],
                "forward_agent": False,
                "extra_ssh_config": "",
                "quick_connect_command": quick_connect_command,
                "unparsed_args": [],
            }

            i = 0
            while i < len(args):
                arg = args[i]

                if arg == '-p' and i + 1 < len(args):
                    try:
                        connection_data["port"] = int(args[i + 1])
                        i += 2
                        continue
                    except ValueError:
                        pass
                elif arg == '-i' and i + 1 < len(args):
                    connection_data["keyfile"] = args[i + 1]
                    connection_data["key_select_mode"] = 2
                    i += 2
                    continue
                elif arg == '-o' and i + 1 < len(args):
                    option = args[i + 1]
                    parsed = option.split('=', 1)
                    if len(parsed) == 2:
                        key, value = parsed
                        key_lower = key.lower()
                        value = value.strip()
                        if key_lower == 'user':
                            connection_data["username"] = value
                        elif key_lower == 'port':
                            try:
                                connection_data["port"] = int(value)
                            except ValueError:
                                pass
                        elif key_lower == 'identityfile':
                            connection_data["keyfile"] = value
                            connection_data["key_select_mode"] = 2
                        elif key_lower == 'identitiesonly':
                            if value.lower() in ('yes', 'true', '1', 'on'):
                                connection_data["key_select_mode"] = 1
                            elif value.lower() in ('no', 'false', '0', 'off') and connection_data.get("keyfile"):
                                connection_data["key_select_mode"] = 2
                        elif key_lower == 'forwardagent':
                            connection_data["forward_agent"] = value.lower() in ('yes', 'true', '1', 'on')
                        else:
                            _append_extra_config(connection_data, f"{key} {value}")
                    i += 2
                    continue
                elif arg.startswith('-o') and '=' in arg[2:]:
                    key, value = arg[2:].split('=', 1)
                    key_lower = key.lower()
                    value = value.strip()
                    if key_lower == 'identityfile':
                        connection_data["keyfile"] = value
                        connection_data["key_select_mode"] = 2
                    elif key_lower == 'identitiesonly':
                        if value.lower() in ('yes', 'true', '1', 'on'):
                            connection_data["key_select_mode"] = 1
                        elif value.lower() in ('no', 'false', '0', 'off') and connection_data.get("keyfile"):
                            connection_data["key_select_mode"] = 2
                    elif key_lower == 'user':
                        connection_data["username"] = value
                    elif key_lower == 'port':
                        try:
                            connection_data["port"] = int(value)
                        except ValueError:
                            pass
                    elif key_lower == 'forwardagent':
                        connection_data["forward_agent"] = value.lower() in ('yes', 'true', '1', 'on')
                    else:
                        _append_extra_config(connection_data, f"{key} {value}")
                    i += 1
                    continue
                elif arg == '-X':
                    connection_data["x11_forwarding"] = True
                    i += 1
                    continue
                elif arg == '-A':
                    connection_data["forward_agent"] = True
                    i += 1
                    continue
                elif arg == '-C':
                    _append_extra_config(connection_data, "Compression yes")
                    i += 1
                    continue
                elif arg == '-4':
                    _append_extra_config(connection_data, "AddressFamily inet")
                    i += 1
                    continue
                elif arg == '-6':
                    _append_extra_config(connection_data, "AddressFamily inet6")
                    i += 1
                    continue
                elif arg == '-J' and i + 1 < len(args):
                    connection_data["proxy_jump"] = [
                        h.strip() for h in args[i + 1].split(',') if h.strip()
                    ]
                    i += 2
                    continue
                elif arg == '-L' and i + 1 < len(args):
                    rule = _parse_forward_spec(args[i + 1], 'local')
                    if rule:
                        connection_data["forwarding_rules"].append(rule)
                    i += 2
                    continue
                elif arg == '-R' and i + 1 < len(args):
                    rule = _parse_forward_spec(args[i + 1], 'remote')
                    if rule:
                        connection_data["forwarding_rules"].append(rule)
                    i += 2
                    continue
                elif arg == '-D' and i + 1 < len(args):
                    rule = _parse_dynamic_spec(args[i + 1])
                    if rule:
                        connection_data["forwarding_rules"].append(rule)
                    i += 2
                    continue
                elif arg.startswith('-p'):
                    try:
                        connection_data["port"] = int(arg[2:])
                        i += 1
                        continue
                    except ValueError:
                        pass
                elif arg.startswith('-i'):
                    connection_data["keyfile"] = arg[2:]
                    connection_data["key_select_mode"] = 2
                    i += 1
                    continue
                elif not arg.startswith('-'):
                    if not connection_data["host"]:
                        if '@' in arg:
                            username, host = arg.split('@', 1)
                            connection_data["username"] = username
                            connection_data["host"] = host
                            connection_data["hostname"] = host
                            connection_data["nickname"] = host
                        else:
                            connection_data["host"] = arg
                            connection_data["hostname"] = arg
                            connection_data["nickname"] = arg
                    else:
                        connection_data["unparsed_args"].append(arg)
                    i += 1
                else:
                    option_key = arg
                    attached_value = ""
                    if option_key.startswith('--'):
                        option_key, _sep, attached_value = option_key.partition('=')
                    elif option_key.startswith('-') and len(option_key) > 2:
                        option_key, attached_value = option_key[:2], option_key[2:]

                    expects_argument = option_key in SSH_OPTIONS_EXPECTING_ARGUMENT
                    if attached_value:
                        connection_data["unparsed_args"].append(arg)
                        i += 1
                        continue

                    if expects_argument:
                        if i + 1 < len(args) and not args[i + 1].startswith('-'):
                            connection_data["unparsed_args"].extend([arg, args[i + 1]])
                            i += 2
                        else:
                            connection_data["unparsed_args"].append(arg)
                            i += 1
                    else:
                        connection_data["unparsed_args"].append(arg)
                        i += 1
                    continue

            if not connection_data["host"]:
                return None

            if connection_data.get("keyfile") and connection_data.get("key_select_mode", 0) == 0:
                connection_data["key_select_mode"] = 2

            return connection_data

        except Exception:
            # Last-resort fallback for bare user@host that somehow raised
            if '@' in command_text and ' ' not in command_text:
                try:
                    username, host = command_text.split('@', 1)
                    if host:
                        return {
                            "nickname": host,
                            "host": host,
                            "hostname": host,
                            "username": username,
                            "port": 22,
                            "auth_method": 0,
                            "key_select_mode": 0,
                            "quick_connect_command": "",
                            "unparsed_args": [],
                        }
                except Exception:
                    pass
            return None
