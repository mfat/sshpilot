"""Command Blocks — right-side panel for storing and running command snippets."""

from __future__ import annotations

import re
import uuid
import logging
from datetime import datetime, timezone

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, Gio, GLib, GObject, Gdk, Pango

from .context_menu import IconContextMenu

logger = logging.getLogger(__name__)

try:
    import gettext
    _ = gettext.gettext
except ImportError:
    _ = lambda s: s

# ---------------------------------------------------------------------------
# Default content (seeded once into an empty store)
# ---------------------------------------------------------------------------

DEFAULT_FOLDERS = [
    {"id": "f-sysinfo",  "name": "System Info", "parent_id": None, "order": 0, "expanded": True},
    {"id": "f-network",  "name": "Network",      "parent_id": None, "order": 1, "expanded": True},
    {"id": "f-procs",    "name": "Processes",    "parent_id": None, "order": 2, "expanded": True},
    {"id": "f-docker",   "name": "Docker",       "parent_id": None, "order": 3, "expanded": False},
    {"id": "f-systemd",  "name": "Systemd",      "parent_id": None, "order": 4, "expanded": False},
    {"id": "f-disk",     "name": "Disk",         "parent_id": None, "order": 5, "expanded": False},
    {"id": "f-logs",     "name": "Logs",         "parent_id": None, "order": 6, "expanded": False},
    {"id": "f-security", "name": "Security",     "parent_id": None, "order": 7, "expanded": False},
]

DEFAULT_COMMANDS = [
    # System Info
    {"id": "c-uname",    "name": "Kernel info",         "command": "uname -a",
     "description": "Kernel & architecture", "tags": ["system"], "folder_id": "f-sysinfo",
     "is_favorite": True, "has_placeholders": False},
    {"id": "c-osrel",    "name": "OS release",           "command": "cat /etc/os-release",
     "description": "Distribution details", "tags": ["system"], "folder_id": "f-sysinfo",
     "is_favorite": False, "has_placeholders": False},
    {"id": "c-uptime",   "name": "Uptime",               "command": "uptime -p",
     "description": "System uptime", "tags": ["system"], "folder_id": "f-sysinfo",
     "is_favorite": False, "has_placeholders": False},
    {"id": "c-mem",      "name": "Memory usage",         "command": "free -h",
     "description": "RAM & swap usage", "tags": ["system", "resources"], "folder_id": "f-sysinfo",
     "is_favorite": False, "has_placeholders": False},
    # Network
    {"id": "c-iface",    "name": "Network interfaces",   "command": "ip addr show",
     "description": "All interfaces and IPs", "tags": ["network"], "folder_id": "f-network",
     "is_favorite": True, "has_placeholders": False},
    {"id": "c-sockets",  "name": "Listening sockets",    "command": "ss -tulnp",
     "description": "Open ports & services", "tags": ["network", "ports"], "folder_id": "f-network",
     "is_favorite": False, "has_placeholders": False},
    {"id": "c-pubip",    "name": "Public IP",            "command": "curl -s ifconfig.me",
     "description": "Show public IP address", "tags": ["network"], "folder_id": "f-network",
     "is_favorite": False, "has_placeholders": False},
    {"id": "c-ping",     "name": "Ping host",            "command": "ping -c 4 ${HOST}",
     "description": "Ping a host 4 times", "tags": ["network"], "folder_id": "f-network",
     "is_favorite": False, "has_placeholders": True},
    # Processes
    {"id": "c-cpu-top",  "name": "Top CPU processes",    "command": "ps aux --sort=-%cpu | head -20",
     "description": "20 most CPU-hungry processes", "tags": ["processes", "resources"], "folder_id": "f-procs",
     "is_favorite": False, "has_placeholders": False},
    {"id": "c-proc-snap","name": "Process snapshot",     "command": "top -bn1 | head -30",
     "description": "One-shot top output", "tags": ["processes"], "folder_id": "f-procs",
     "is_favorite": False, "has_placeholders": False},
    {"id": "c-kill",     "name": "Force kill PID",       "command": "kill -9 ${PID}",
     "description": "Kill process by PID", "tags": ["processes"], "folder_id": "f-procs",
     "is_favorite": False, "has_placeholders": True},
    # Docker
    {"id": "c-dps",      "name": "List containers",      "command": "docker ps -a",
     "description": "All Docker containers", "tags": ["docker"], "folder_id": "f-docker",
     "is_favorite": True, "has_placeholders": False},
    {"id": "c-dimages",  "name": "List images",          "command": "docker images",
     "description": "Local Docker images", "tags": ["docker"], "folder_id": "f-docker",
     "is_favorite": False, "has_placeholders": False},
    {"id": "c-dlogs",    "name": "Container logs",       "command": "docker logs --tail 100 -f ${CONTAINER}",
     "description": "Follow container logs", "tags": ["docker", "logs"], "folder_id": "f-docker",
     "is_favorite": False, "has_placeholders": True},
    {"id": "c-dexec",    "name": "Shell into container", "command": "docker exec -it ${CONTAINER} /bin/bash",
     "description": "Interactive shell in container", "tags": ["docker"], "folder_id": "f-docker",
     "is_favorite": False, "has_placeholders": True},
    {"id": "c-dstats",   "name": "Container stats",      "command": "docker stats --no-stream",
     "description": "Resource usage per container", "tags": ["docker", "resources"], "folder_id": "f-docker",
     "is_favorite": False, "has_placeholders": False},
    {"id": "c-dprune",   "name": "Prune Docker",         "command": "docker system prune -f",
     "description": "Remove unused Docker resources", "tags": ["docker"], "folder_id": "f-docker",
     "is_favorite": False, "has_placeholders": False},
    # Systemd
    {"id": "c-svc-stat", "name": "Service status",       "command": "systemctl status ${SERVICE}",
     "description": "Status of a systemd service", "tags": ["systemd", "services"], "folder_id": "f-systemd",
     "is_favorite": True, "has_placeholders": True},
    {"id": "c-svc-log",  "name": "Service logs",         "command": "journalctl -u ${SERVICE} -n 100 --no-pager",
     "description": "Last 100 lines of service journal", "tags": ["systemd", "logs"], "folder_id": "f-systemd",
     "is_favorite": False, "has_placeholders": True},
    {"id": "c-svc-fail", "name": "Failed units",         "command": "systemctl list-units --state=failed",
     "description": "All failed systemd units", "tags": ["systemd"], "folder_id": "f-systemd",
     "is_favorite": False, "has_placeholders": False},
    {"id": "c-svc-rst",  "name": "Restart service",      "command": "systemctl restart ${SERVICE}",
     "description": "Restart a systemd service", "tags": ["systemd", "services"], "folder_id": "f-systemd",
     "is_favorite": False, "has_placeholders": True},
    # Disk
    {"id": "c-df",       "name": "Disk usage",           "command": "df -h",
     "description": "Disk usage by mount point", "tags": ["disk"], "folder_id": "f-disk",
     "is_favorite": False, "has_placeholders": False},
    {"id": "c-du-logs",  "name": "Log dir sizes",        "command": "du -sh /var/log/*",
     "description": "Size of each log file/dir", "tags": ["disk", "logs"], "folder_id": "f-disk",
     "is_favorite": False, "has_placeholders": False},
    {"id": "c-lsblk",    "name": "Block devices",        "command": "lsblk",
     "description": "List all block devices", "tags": ["disk"], "folder_id": "f-disk",
     "is_favorite": False, "has_placeholders": False},
    {"id": "c-bigfiles", "name": "Largest files",
     "command": "find / -xdev -size +100M -printf '%s %p\\n' 2>/dev/null | sort -rn | head -10",
     "description": "Files over 100 MB", "tags": ["disk"], "folder_id": "f-disk",
     "is_favorite": False, "has_placeholders": False},
    # Logs
    {"id": "c-syslog",   "name": "Follow syslog",        "command": "tail -f /var/log/syslog",
     "description": "Live syslog (Ctrl+C to stop)", "tags": ["logs"], "folder_id": "f-logs",
     "is_favorite": False, "has_placeholders": False},
    {"id": "c-journal",  "name": "Follow journal",       "command": "journalctl -f -n 50",
     "description": "Live systemd journal", "tags": ["logs", "systemd"], "folder_id": "f-logs",
     "is_favorite": False, "has_placeholders": False},
    {"id": "c-errlogs",  "name": "Recent errors",        "command": "grep -i error /var/log/syslog | tail -50",
     "description": "Last 50 error lines in syslog", "tags": ["logs"], "folder_id": "f-logs",
     "is_favorite": False, "has_placeholders": False},
    # Security
    {"id": "c-last",     "name": "Recent logins",        "command": "last -n 20",
     "description": "Last 20 login events", "tags": ["security"], "folder_id": "f-security",
     "is_favorite": False, "has_placeholders": False},
    {"id": "c-who",      "name": "Who is logged in",     "command": "who",
     "description": "Currently logged-in users", "tags": ["security"], "folder_id": "f-security",
     "is_favorite": False, "has_placeholders": False},
    {"id": "c-lastb",    "name": "Failed logins",        "command": "lastb | head -20",
     "description": "Last 20 failed login attempts", "tags": ["security"], "folder_id": "f-security",
     "is_favorite": False, "has_placeholders": False},
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# CommandBlockStore — pure-Python model
# ---------------------------------------------------------------------------

class CommandBlockStore:
    """Reads/writes config.config_data['command_blocks']. No GTK dependency."""

    def __init__(self, config: 'Config') -> None:
        self._config = config
        self._load()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _data(self) -> dict:
        cb = self._config.config_data.get('command_blocks')
        if not isinstance(cb, dict):
            self._config.config_data['command_blocks'] = {
                'folders': [], 'commands': [], 'defaults_loaded': False
            }
        return self._config.config_data['command_blocks']

    def _load(self) -> None:
        self._ensure_defaults()

    def _save(self) -> None:
        try:
            self._config.save_json_config()
        except Exception as exc:
            logger.error("Failed to save command blocks: %s", exc)

    def _new_id(self) -> str:
        return str(uuid.uuid4())

    def _ensure_defaults(self) -> None:
        data = self._data()
        if data.get('defaults_loaded'):
            return
        if data.get('commands'):
            data['defaults_loaded'] = True
            self._save()
            return
        for f in DEFAULT_FOLDERS:
            if not any(x['id'] == f['id'] for x in data.get('folders', [])):
                data.setdefault('folders', []).append(dict(f))
        for c in DEFAULT_COMMANDS:
            entry = {
                'id': c['id'],
                'name': c['name'],
                'command': c['command'],
                'description': c.get('description', ''),
                'tags': list(c.get('tags', [])),
                'folder_id': c.get('folder_id'),
                'is_favorite': bool(c.get('is_favorite', False)),
                'use_count': 0,
                'last_used': None,
                'has_placeholders': bool(c.get('has_placeholders', False)),
                'created_at': _now_iso(),
            }
            if not any(x['id'] == entry['id'] for x in data.get('commands', [])):
                data.setdefault('commands', []).append(entry)
        data['defaults_loaded'] = True
        self._save()

    # ------------------------------------------------------------------
    # Folders
    # ------------------------------------------------------------------

    def get_folders(self) -> list[dict]:
        return list(self._data().get('folders', []))

    def add_folder(self, name: str, parent_id: str | None = None) -> dict:
        folders = self._data().setdefault('folders', [])
        entry = {
            'id': self._new_id(),
            'name': name,
            'parent_id': parent_id,
            'order': len(folders),
            'expanded': True,
        }
        folders.append(entry)
        self._save()
        return entry

    def update_folder(self, folder_id: str, **kwargs) -> None:
        for f in self._data().get('folders', []):
            if f['id'] == folder_id:
                f.update({k: v for k, v in kwargs.items() if k in f or k in ('name', 'expanded', 'order', 'parent_id')})
                self._save()
                return

    def delete_folder(self, folder_id: str) -> None:
        data = self._data()
        data['folders'] = [f for f in data.get('folders', []) if f['id'] != folder_id]
        for cmd in data.get('commands', []):
            if cmd.get('folder_id') == folder_id:
                cmd['folder_id'] = None
        self._save()

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def get_commands(self) -> list[dict]:
        return list(self._data().get('commands', []))

    def add_command(self, name: str, command: str, **kwargs) -> dict:
        entry = {
            'id': self._new_id(),
            'name': name,
            'command': command,
            'description': kwargs.get('description', ''),
            'tags': list(kwargs.get('tags', [])),
            'folder_id': kwargs.get('folder_id'),
            'is_favorite': bool(kwargs.get('is_favorite', False)),
            'use_count': 0,
            'last_used': None,
            'has_placeholders': bool(kwargs.get('has_placeholders', False)),
            'created_at': _now_iso(),
        }
        self._data().setdefault('commands', []).append(entry)
        self._save()
        return entry

    def update_command(self, cmd_id: str, **kwargs) -> None:
        allowed = {'name', 'command', 'description', 'tags', 'folder_id',
                   'is_favorite', 'has_placeholders'}
        for cmd in self._data().get('commands', []):
            if cmd['id'] == cmd_id:
                for k, v in kwargs.items():
                    if k in allowed:
                        cmd[k] = v
                self._save()
                return

    def delete_command(self, cmd_id: str) -> None:
        data = self._data()
        data['commands'] = [c for c in data.get('commands', []) if c['id'] != cmd_id]
        self._save()

    def duplicate_command(self, cmd_id: str) -> dict | None:
        for cmd in self._data().get('commands', []):
            if cmd['id'] == cmd_id:
                new_cmd = dict(cmd)
                new_cmd['id'] = self._new_id()
                new_cmd['name'] = cmd['name'] + _(' (copy)')
                new_cmd['use_count'] = 0
                new_cmd['last_used'] = None
                new_cmd['created_at'] = _now_iso()
                self._data().setdefault('commands', []).append(new_cmd)
                self._save()
                return new_cmd
        return None

    def record_use(self, cmd_id: str) -> None:
        for cmd in self._data().get('commands', []):
            if cmd['id'] == cmd_id:
                cmd['use_count'] = cmd.get('use_count', 0) + 1
                cmd['last_used'] = _now_iso()
                self._save()
                return

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def search(self, query: str) -> list[dict]:
        q = query.lower().strip()
        if not q:
            return self.get_commands()
        results = []
        for cmd in self._data().get('commands', []):
            haystack = ' '.join([
                cmd.get('name', ''),
                cmd.get('description', ''),
                cmd.get('command', ''),
                ' '.join(cmd.get('tags', [])),
            ]).lower()
            if q in haystack:
                results.append(cmd)
        return results

    def get_favorites(self) -> list[dict]:
        return [c for c in self._data().get('commands', []) if c.get('is_favorite')]


# ---------------------------------------------------------------------------
# PlaceholderDialog
# ---------------------------------------------------------------------------

class PlaceholderDialog(Adw.Window):
    """Fill in ${VAR} placeholders before sending a command to the terminal."""

    __gsignals__ = {
        'send': (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, parent: Gtk.Window, cmd: dict) -> None:
        super().__init__()
        self._cmd = cmd
        self._entries: dict[str, Gtk.Entry] = {}
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_default_size(420, -1)
        self.set_title(_('Fill Placeholders'))
        self._build_ui()

    def _parse_placeholders(self, command: str) -> list[str]:
        seen: list[str] = []
        for var in re.findall(r'\$\{([^}]+)\}', command):
            if var not in seen:
                seen.append(var)
        return seen

    def _build_ui(self) -> None:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(root)

        header = Adw.HeaderBar()
        cancel_btn = Gtk.Button(label=_('Cancel'))
        cancel_btn.connect('clicked', lambda _: self.close())
        header.pack_start(cancel_btn)
        send_btn = Gtk.Button(label=_('Send'))
        send_btn.add_css_class('suggested-action')
        send_btn.connect('clicked', self._on_confirm)
        header.pack_end(send_btn)
        root.append(header)

        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect('key-pressed', self._on_key_pressed)
        self.add_controller(key_ctrl)

        scr = Gtk.ScrolledWindow()
        scr.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scr.set_propagate_natural_height(True)
        root.append(scr)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        body.set_margin_start(16)
        body.set_margin_end(16)
        body.set_margin_top(12)
        body.set_margin_bottom(16)
        scr.set_child(body)

        title_lbl = Gtk.Label()
        title_lbl.set_markup(f'<b>{GLib.markup_escape_text(self._cmd.get("name", ""))}</b>')
        title_lbl.set_xalign(0)
        title_lbl.add_css_class('heading')
        body.append(title_lbl)

        vars_ = self._parse_placeholders(self._cmd.get('command', ''))
        group = Adw.PreferencesGroup()
        for var in vars_:
            row = Adw.EntryRow()
            row.set_title(f'${{{var}}}')
            row.connect('entry-activated', self._on_confirm)
            self._entries[var] = row
            group.add(row)
        body.append(group)

        self._preview = Gtk.Label(label=self._cmd.get('command', ''))
        self._preview.set_xalign(0)
        self._preview.set_wrap(True)
        self._preview.add_css_class('monospace')
        self._preview.add_css_class('dim-label')
        body.append(self._preview)

        for row in self._entries.values():
            row.connect('notify::text', lambda *_: self._update_preview())

    def _update_preview(self) -> None:
        result = self._cmd.get('command', '')
        for var, entry in self._entries.items():
            result = result.replace(f'${{{var}}}', entry.get_text() or f'${{{var}}}')
        self._preview.set_text(result)

    def _on_key_pressed(self, _ctrl, keyval, _keycode, _state) -> bool:
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True
        return False

    def _on_confirm(self, *_) -> None:
        result = self._cmd.get('command', '')
        for var, entry in self._entries.items():
            result = result.replace(f'${{{var}}}', entry.get_text())
        self.emit('send', result)
        self.close()


# ---------------------------------------------------------------------------
# FolderRow / CommandRow — flat list rows (matches connection list pattern)
# ---------------------------------------------------------------------------

_FOLDER_INDENT_PX = 24


class FavoritesRow(Gtk.ListBoxRow):
    """Virtual 'Favorites' folder header — unremovable, always first."""

    __gsignals__ = {
        'folder-toggled': (GObject.SignalFlags.RUN_FIRST, None, (bool,)),
    }

    def __init__(self, cmd_count: int) -> None:
        super().__init__()
        self._expanded = True
        self.set_selectable(True)
        self.set_can_focus(True)

        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        content.set_margin_start(12)
        content.set_margin_end(12)
        content.set_margin_top(6)
        content.set_margin_bottom(6)

        icon = Gtk.Image.new_from_icon_name('starred-symbolic')
        icon.set_pixel_size(16)
        icon.set_valign(Gtk.Align.CENTER)
        content.append(icon)

        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        info.set_hexpand(True)
        info.set_valign(Gtk.Align.CENTER)

        name_lbl = Gtk.Label(label=_('Favorites'))
        name_lbl.set_halign(Gtk.Align.START)
        name_lbl.set_xalign(0)
        name_lbl.set_hexpand(True)
        name_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        info.append(name_lbl)

        self._count_lbl = Gtk.Label(
            label=_('%d commands') % cmd_count if cmd_count != 1 else _('1 command')
        )
        self._count_lbl.set_halign(Gtk.Align.START)
        self._count_lbl.set_xalign(0)
        self._count_lbl.add_css_class('dim-label')
        info.append(self._count_lbl)

        content.append(info)

        self._expand_btn = Gtk.Button()
        self._expand_btn.set_icon_name('pan-down-symbolic')
        self._expand_btn.add_css_class('flat')
        self._expand_btn.set_can_focus(False)
        self._expand_btn.connect('clicked', self._on_expand_clicked)
        content.append(self._expand_btn)

        self.set_child(content)

        gesture = Gtk.GestureClick()
        gesture.set_button(1)
        gesture.connect('pressed', self._on_click)
        self.add_controller(gesture)

    def _on_click(self, _gesture, n_press, _x, _y) -> None:
        listbox = self.get_parent()
        if listbox and n_press == 1:
            listbox.select_row(self)
        elif n_press == 2:
            self._toggle_expand()

    def _on_expand_clicked(self, _button) -> None:
        self._toggle_expand()

    def _toggle_expand(self) -> None:
        self._expanded = not self._expanded
        self._expand_btn.set_icon_name(
            'pan-down-symbolic' if self._expanded else 'pan-end-symbolic'
        )
        self.emit('folder-toggled', self._expanded)


class FolderRow(Gtk.ListBoxRow):
    """Folder header row in the command blocks tree."""

    __gsignals__ = {
        'folder-toggled': (GObject.SignalFlags.RUN_FIRST, None, (str, bool)),
    }

    def __init__(self, folder: dict, cmd_count: int) -> None:
        super().__init__()
        self.folder_id = folder['id']
        self._folder = folder
        self.set_selectable(True)
        self.set_can_focus(True)

        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        content.set_margin_start(12)
        content.set_margin_end(12)
        content.set_margin_top(6)
        content.set_margin_bottom(6)

        icon = Gtk.Image.new_from_icon_name('folder-symbolic')
        icon.set_pixel_size(16)
        icon.set_valign(Gtk.Align.CENTER)
        content.append(icon)

        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        info.set_hexpand(True)
        info.set_valign(Gtk.Align.CENTER)

        name_lbl = Gtk.Label(label=folder.get('name', ''))
        name_lbl.set_halign(Gtk.Align.START)
        name_lbl.set_xalign(0)
        name_lbl.set_hexpand(True)
        name_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        info.append(name_lbl)

        count_lbl = Gtk.Label(label=_('%d commands') % cmd_count if cmd_count != 1 else _('1 command'))
        count_lbl.set_halign(Gtk.Align.START)
        count_lbl.set_xalign(0)
        count_lbl.add_css_class('dim-label')
        info.append(count_lbl)

        content.append(info)

        self._expand_btn = Gtk.Button()
        self._update_expand_icon()
        self._expand_btn.add_css_class('flat')
        self._expand_btn.set_can_focus(False)
        self._expand_btn.connect('clicked', self._on_expand_clicked)
        content.append(self._expand_btn)

        self.set_child(content)
        self._setup_double_click_gesture()

    def _setup_double_click_gesture(self) -> None:
        gesture = Gtk.GestureClick()
        gesture.set_button(1)
        gesture.connect('pressed', self._on_click)
        self.add_controller(gesture)

    def _on_click(self, _gesture, n_press, _x, _y) -> None:
        listbox = self.get_parent()
        if listbox and n_press == 1:
            listbox.select_row(self)
        elif n_press == 2:
            self._toggle_expand()

    def _update_expand_icon(self) -> None:
        icon_name = ('pan-down-symbolic' if self._folder.get('expanded', True)
                     else 'pan-end-symbolic')
        self._expand_btn.set_icon_name(icon_name)

    def _on_expand_clicked(self, _button) -> None:
        self._toggle_expand()

    def _toggle_expand(self) -> None:
        expanded = not self._folder.get('expanded', True)
        self._folder['expanded'] = expanded
        self._update_expand_icon()
        self.emit('folder-toggled', self.folder_id, expanded)


class CommandRow(Gtk.ListBoxRow):
    """Individual command row — direct ListBox child for proper selection."""

    def __init__(self, cmd: dict, indent: bool = False) -> None:
        super().__init__()
        self._cmd_data = cmd
        self.set_selectable(True)

        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        margin_start = 12 + (_FOLDER_INDENT_PX if indent else 0)
        content.set_margin_start(margin_start)
        content.set_margin_end(12)
        content.set_margin_top(6)
        content.set_margin_bottom(6)

        prefix = Gtk.Image.new_from_icon_name('utilities-terminal-symbolic')
        prefix.set_pixel_size(16)
        prefix.set_valign(Gtk.Align.CENTER)
        content.append(prefix)

        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        info.set_hexpand(True)
        info.set_valign(Gtk.Align.CENTER)

        title = Gtk.Label(label=cmd.get('name', ''))
        title.set_halign(Gtk.Align.START)
        title.set_xalign(0)
        title.set_hexpand(True)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        info.append(title)

        subtitle = cmd.get('description') or cmd.get('command', '')[:60]
        if subtitle:
            sub = Gtk.Label(label=subtitle)
            sub.set_halign(Gtk.Align.START)
            sub.set_xalign(0)
            sub.set_hexpand(True)
            sub.set_ellipsize(Pango.EllipsizeMode.END)
            sub.add_css_class('dim-label')
            info.append(sub)

        content.append(info)

        self._star_btn = Gtk.ToggleButton()
        self._star_btn.set_icon_name(
            'starred-symbolic' if cmd.get('is_favorite') else 'non-starred-symbolic'
        )
        self._star_btn.set_active(bool(cmd.get('is_favorite')))
        self._star_btn.add_css_class('flat')
        self._star_btn.set_valign(Gtk.Align.CENTER)
        self._star_btn.set_tooltip_text(_('Toggle favorite'))
        content.append(self._star_btn)

        self.set_child(content)


# ---------------------------------------------------------------------------
# CommandEditDialog
# ---------------------------------------------------------------------------

class CommandEditDialog(Adw.Window):
    """Add or edit a CommandBlock."""

    __gsignals__ = {
        'saved': (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self, parent: Gtk.Window, store: CommandBlockStore,
                 cmd: dict | None = None) -> None:
        super().__init__()
        self._store = store
        self._cmd = cmd
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_default_size(520, 580)
        self.set_title(_('Edit Command') if cmd else _('New Command'))
        self._build_ui()

    def _build_ui(self) -> None:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(root)

        header = Adw.HeaderBar()
        cancel_btn = Gtk.Button(label=_('Cancel'))
        cancel_btn.connect('clicked', lambda _: self.close())
        header.pack_start(cancel_btn)
        save_btn = Gtk.Button(label=_('Save'))
        save_btn.add_css_class('suggested-action')
        save_btn.connect('clicked', self._on_save)
        header.pack_end(save_btn)
        root.append(header)

        scr = Gtk.ScrolledWindow()
        scr.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scr.set_vexpand(True)
        root.append(scr)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        body.set_margin_start(16)
        body.set_margin_end(16)
        body.set_margin_top(12)
        body.set_margin_bottom(16)
        scr.set_child(body)

        details_group = Adw.PreferencesGroup(title=_('Command Details'))
        body.append(details_group)

        self._name_row = Adw.EntryRow(title=_('Name *'))
        if self._cmd:
            self._name_row.set_text(self._cmd.get('name', ''))
        details_group.add(self._name_row)

        self._desc_row = Adw.EntryRow(title=_('Description'))
        if self._cmd:
            self._desc_row.set_text(self._cmd.get('description', ''))
        details_group.add(self._desc_row)

        self._tags_row = Adw.EntryRow(title=_('Tags (comma-separated)'))
        if self._cmd:
            self._tags_row.set_text(', '.join(self._cmd.get('tags', [])))
        details_group.add(self._tags_row)

        # Folder selector
        folders = self._store.get_folders()
        folder_names = [_('(No folder)')] + [f['name'] for f in folders]
        self._folder_names_list = [None] + [f['id'] for f in folders]
        folder_string_list = Gtk.StringList()
        for n in folder_names:
            folder_string_list.append(n)
        self._folder_row = Adw.ComboRow(title=_('Folder'))
        self._folder_row.set_model(folder_string_list)
        current_folder_id = self._cmd.get('folder_id') if self._cmd else None
        if current_folder_id and current_folder_id in self._folder_names_list:
            self._folder_row.set_selected(self._folder_names_list.index(current_folder_id))
        details_group.add(self._folder_row)

        # Command text (multi-line)
        cmd_group = Adw.PreferencesGroup(title=_('Command *'))
        body.append(cmd_group)

        cmd_frame = Gtk.Frame()
        cmd_frame.add_css_class('card')
        cmd_group.add(cmd_frame)

        self._cmd_view = Gtk.TextView()
        self._cmd_view.set_size_request(-1, 96)
        self._cmd_view.set_monospace(True)
        self._cmd_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._cmd_view.set_top_margin(8)
        self._cmd_view.set_bottom_margin(8)
        self._cmd_view.set_left_margin(8)
        self._cmd_view.set_right_margin(8)
        if self._cmd:
            self._cmd_view.get_buffer().set_text(self._cmd.get('command', ''))
        cmd_frame.set_child(self._cmd_view)

        opts_group = Adw.PreferencesGroup(title=_('Options'))
        body.append(opts_group)

        self._placeholder_row = Adw.SwitchRow(title=_('Has Placeholders'),
                                               subtitle=_('Use ${VAR} syntax in command'))
        if self._cmd:
            self._placeholder_row.set_active(self._cmd.get('has_placeholders', False))
        opts_group.add(self._placeholder_row)

        self._favorite_row = Adw.SwitchRow(title=_('Favorite'))
        if self._cmd:
            self._favorite_row.set_active(self._cmd.get('is_favorite', False))
        opts_group.add(self._favorite_row)

    def _on_save(self, *_) -> None:
        name = self._name_row.get_text().strip()
        buf = self._cmd_view.get_buffer()
        command = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False).strip()
        if not name or not command:
            return

        tags_raw = self._tags_row.get_text().strip()
        tags = [t.strip() for t in tags_raw.split(',') if t.strip()] if tags_raw else []
        selected_idx = self._folder_row.get_selected()
        folder_id = self._folder_names_list[selected_idx] if selected_idx < len(self._folder_names_list) else None

        if self._cmd:
            self._store.update_command(
                self._cmd['id'],
                name=name,
                command=command,
                description=self._desc_row.get_text().strip(),
                tags=tags,
                folder_id=folder_id,
                has_placeholders=self._placeholder_row.get_active(),
                is_favorite=self._favorite_row.get_active(),
            )
            saved = next((c for c in self._store.get_commands() if c['id'] == self._cmd['id']), None)
        else:
            saved = self._store.add_command(
                name, command,
                description=self._desc_row.get_text().strip(),
                tags=tags,
                folder_id=folder_id,
                has_placeholders=self._placeholder_row.get_active(),
                is_favorite=self._favorite_row.get_active(),
            )

        self.emit('saved', saved)
        self.close()


# ---------------------------------------------------------------------------
# AddFolderDialog
# ---------------------------------------------------------------------------

class AddFolderDialog(Adw.Window):
    """Simple dialog to name a new folder."""

    __gsignals__ = {
        'created': (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, parent: Gtk.Window) -> None:
        super().__init__()
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_default_size(360, -1)
        self.set_title(_('New Folder'))
        self._build_ui()

    def _build_ui(self) -> None:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(root)

        header = Adw.HeaderBar()
        cancel_btn = Gtk.Button(label=_('Cancel'))
        cancel_btn.connect('clicked', lambda _: self.close())
        header.pack_start(cancel_btn)
        create_btn = Gtk.Button(label=_('Create'))
        create_btn.add_css_class('suggested-action')
        create_btn.connect('clicked', self._on_create)
        header.pack_end(create_btn)
        root.append(header)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        body.set_margin_start(16)
        body.set_margin_end(16)
        body.set_margin_top(12)
        body.set_margin_bottom(16)
        root.append(body)

        group = Adw.PreferencesGroup()
        self._name_row = Adw.EntryRow(title=_('Folder Name'))
        self._name_row.connect('entry-activated', self._on_create)
        group.add(self._name_row)
        body.append(group)

    def _on_create(self, *_) -> None:
        name = self._name_row.get_text().strip()
        if name:
            self.emit('created', name)
            self.close()


# ---------------------------------------------------------------------------
# CommandBlocksPanel — the right-side sidebar widget
# ---------------------------------------------------------------------------

class CommandBlocksPanel(Gtk.Box):
    """Right-side panel listing command blocks with search, folders, and editing."""

    def __init__(self, window, store: CommandBlockStore) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self.store = store
        self._search_query = ''
        self._favorites_expanded = True
        self._auto_hide_timer_id = None
        self._build_ui()
        self.refresh()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self.add_css_class('sidebar')

        # --- Header ---
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        header.set_margin_start(8)
        header.set_margin_end(8)
        header.set_margin_top(8)
        header.set_margin_bottom(4)

        title = Gtk.Label(label=_('Commands'))
        title.set_hexpand(True)
        title.set_xalign(0)
        title.add_css_class('title-4')
        header.append(title)

        add_cmd_btn = Gtk.Button()
        add_cmd_btn.set_icon_name('list-add-symbolic')
        add_cmd_btn.add_css_class('flat')
        add_cmd_btn.set_tooltip_text(_('Add command'))
        add_cmd_btn.connect('clicked', lambda _: self._open_edit_dialog(None))
        header.append(add_cmd_btn)

        add_folder_btn = Gtk.Button()
        add_folder_btn.set_icon_name('folder-new-symbolic')
        add_folder_btn.add_css_class('flat')
        add_folder_btn.set_tooltip_text(_('New folder'))
        add_folder_btn.connect('clicked', lambda _: self._open_add_folder_dialog())
        header.append(add_folder_btn)

        self._search_toggle = Gtk.ToggleButton()
        self._search_toggle.set_icon_name('system-search-symbolic')
        self._search_toggle.add_css_class('flat')
        self._search_toggle.set_tooltip_text(_('Search commands'))
        self._search_toggle.connect('toggled', self._on_search_toggle)
        header.append(self._search_toggle)

        self.append(header)

        # --- Search bar (revealed on toggle) ---
        self._search_revealer = Gtk.Revealer()
        self._search_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self._search_revealer.set_reveal_child(False)

        self._search_entry = Gtk.SearchEntry()
        self._search_entry.set_margin_start(8)
        self._search_entry.set_margin_end(8)
        self._search_entry.set_margin_top(4)
        self._search_entry.set_margin_bottom(4)
        self._search_entry.connect('search-changed', self._on_search_changed)
        self._search_revealer.set_child(self._search_entry)
        self.append(self._search_revealer)

        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        self.append(sep)

        # --- Main stack: tree / search results / empty ---
        self._main_stack = Gtk.Stack()
        self._main_stack.set_vexpand(True)
        self._main_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.append(self._main_stack)

        # Tree page
        self._tree_scroll = Gtk.ScrolledWindow()
        self._tree_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._tree_scroll.set_vexpand(True)
        self._tree_list = Gtk.ListBox()
        self._tree_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._tree_list.set_activate_on_single_click(False)
        self._tree_list.add_css_class('navigation-sidebar')
        self._tree_list.set_show_separators(False)
        self._tree_scroll.set_child(self._tree_list)
        self._main_stack.add_named(self._tree_scroll, 'tree')

        # Add key controller on tree list for Delete / Ctrl+E
        tree_key_ctrl = Gtk.EventControllerKey()
        tree_key_ctrl.connect('key-pressed', self._on_list_key_pressed)
        self._tree_list.add_controller(tree_key_ctrl)

        # Search results page
        self._search_scroll = Gtk.ScrolledWindow()
        self._search_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._search_scroll.set_vexpand(True)
        self._search_results_list = Gtk.ListBox()
        self._search_results_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._search_results_list.add_css_class('navigation-sidebar')
        self._search_results_list.set_show_separators(False)
        self._search_scroll.set_child(self._search_results_list)
        self._main_stack.add_named(self._search_scroll, 'search')

        search_key_ctrl = Gtk.EventControllerKey()
        search_key_ctrl.connect('key-pressed', self._on_list_key_pressed)
        self._search_results_list.add_controller(search_key_ctrl)

        # Empty page
        empty_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        empty_box.set_valign(Gtk.Align.CENTER)
        empty_box.set_halign(Gtk.Align.CENTER)
        empty_box.set_vexpand(True)
        empty_icon = Gtk.Image.new_from_icon_name('utilities-terminal-symbolic')
        empty_icon.set_pixel_size(48)
        empty_icon.add_css_class('dim-label')
        empty_box.append(empty_icon)
        empty_lbl = Gtk.Label(label=_('No commands yet.\nClick + to add one.'))
        empty_lbl.set_justify(Gtk.Justification.CENTER)
        empty_lbl.add_css_class('dim-label')
        empty_box.append(empty_lbl)
        self._main_stack.add_named(empty_box, 'empty')

        # Panel-level '/' shortcut to focus search
        panel_key_ctrl = Gtk.EventControllerKey()
        panel_key_ctrl.connect('key-pressed', self._on_panel_key_pressed)
        self.add_controller(panel_key_ctrl)

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        # Clear tree list
        while True:
            child = self._tree_list.get_first_child()
            if child is None:
                break
            self._tree_list.remove(child)

        commands = self.store.get_commands()
        folders = self.store.get_folders()

        if not commands:
            self._main_stack.set_visible_child_name('empty')
            return

        if self._search_query:
            self._show_search_results(self._search_query)
            return

        self._main_stack.set_visible_child_name('tree')

        cmds_by_folder: dict[str | None, list[dict]] = {}
        for cmd in commands:
            fid = cmd.get('folder_id')
            cmds_by_folder.setdefault(fid, []).append(cmd)

        # Virtual Favorites folder — always first, unremovable
        favorites = self.store.get_favorites()
        if favorites:
            fav_row = FavoritesRow(len(favorites))
            fav_row.connect('folder-toggled', self._on_favorites_toggled)
            self._tree_list.append(fav_row)
            if self._favorites_expanded:
                for cmd in favorites:
                    row = self._build_command_row(cmd, indent=True)
                    self._tree_list.append(row)

        # Root commands (no folder)
        for cmd in cmds_by_folder.get(None, []):
            row = self._build_command_row(cmd)
            self._tree_list.append(row)

        # Folders in order — flat list like the connection sidebar
        sorted_folders = sorted(folders, key=lambda f: f.get('order', 0))
        for folder in sorted_folders:
            fid = folder['id']
            folder_cmds = cmds_by_folder.get(fid, [])
            folder_row = FolderRow(folder, len(folder_cmds))
            folder_row.connect('folder-toggled', self._on_folder_toggled)
            self._tree_list.append(folder_row)
            if folder.get('expanded', True):
                for cmd in folder_cmds:
                    row = self._build_command_row(cmd, indent=True)
                    self._tree_list.append(row)

    def _row_index(self, target: Gtk.ListBoxRow) -> int:
        index = 0
        child = self._tree_list.get_first_child()
        while child is not None:
            if child == target:
                return index
            index += 1
            child = child.get_next_sibling()
        return -1

    def _on_favorites_toggled(self, fav_row: FavoritesRow, expanded: bool) -> None:
        self._favorites_expanded = expanded
        if expanded:
            position = self._row_index(fav_row) + 1
            for cmd in self.store.get_favorites():
                row = self._build_command_row(cmd, indent=True)
                self._tree_list.insert(row, position)
                position += 1
        else:
            sibling = fav_row.get_next_sibling()
            while sibling is not None:
                next_s = sibling.get_next_sibling()
                if isinstance(sibling, (FolderRow, FavoritesRow)):
                    break
                if isinstance(sibling, CommandRow) and sibling._cmd_data.get('is_favorite'):
                    self._tree_list.remove(sibling)
                sibling = next_s

    def _on_folder_toggled(self, folder_row: FolderRow, folder_id: str, expanded: bool) -> None:
        self.store.update_folder(folder_id, expanded=expanded)
        if expanded:
            self._insert_folder_commands(folder_row, folder_id)
        else:
            self._remove_folder_commands(folder_row, folder_id)

    def _insert_folder_commands(self, folder_row: FolderRow, folder_id: str) -> None:
        sibling = folder_row.get_next_sibling()
        while sibling is not None:
            if isinstance(sibling, FolderRow):
                break
            if (isinstance(sibling, CommandRow)
                    and sibling._cmd_data.get('folder_id') == folder_id):
                return
            sibling = sibling.get_next_sibling()

        cmds = [c for c in self.store.get_commands() if c.get('folder_id') == folder_id]
        position = self._row_index(folder_row) + 1
        for cmd in cmds:
            row = self._build_command_row(cmd, indent=True)
            self._tree_list.insert(row, position)
            position += 1

    def _remove_folder_commands(self, folder_row: FolderRow, folder_id: str) -> None:
        selected = self._tree_list.get_selected_row()
        selected_in_folder = (
            isinstance(selected, CommandRow)
            and selected._cmd_data.get('folder_id') == folder_id
        )

        sibling = folder_row.get_next_sibling()
        while sibling is not None:
            next_sibling = sibling.get_next_sibling()
            if isinstance(sibling, FolderRow):
                break
            if (isinstance(sibling, CommandRow)
                    and sibling._cmd_data.get('folder_id') == folder_id):
                self._tree_list.remove(sibling)
            sibling = next_sibling

        if selected_in_folder:
            self._tree_list.select_row(folder_row)

    def _build_command_row(self, cmd: dict, indent: bool = False) -> CommandRow:
        row = CommandRow(cmd, indent=indent)
        _cmd = cmd

        def _on_star_toggled(btn, c=_cmd):
            self._toggle_favorite(c)
            btn.set_icon_name(
                'starred-symbolic' if c.get('is_favorite') else 'non-starred-symbolic'
            )
        row._star_btn.connect('toggled', _on_star_toggled)

        dbl_click = Gtk.GestureClick()
        dbl_click.set_button(1)
        dbl_click.connect('pressed', lambda g, n, x, y, c=_cmd: self._on_row_click(g, n, x, y, c))
        row.add_controller(dbl_click)

        right_click = Gtk.GestureClick()
        right_click.set_button(3)
        right_click.connect(
            'pressed', lambda g, n, x, y, r=row, c=_cmd: self._show_command_context_menu(r, c)
        )
        row.add_controller(right_click)

        self._setup_command_drag_source(row, cmd)
        return row

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def _on_search_toggle(self, btn: Gtk.ToggleButton) -> None:
        revealed = btn.get_active()
        self._search_revealer.set_reveal_child(revealed)
        if revealed:
            self._search_entry.grab_focus()
        else:
            self._search_entry.set_text('')
            self._search_query = ''
            self.refresh()

    def _on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        query = entry.get_text().strip()
        self._search_query = query
        if query:
            self._show_search_results(query)
        else:
            self.refresh()

    def _show_search_results(self, query: str) -> None:
        while True:
            child = self._search_results_list.get_first_child()
            if child is None:
                break
            self._search_results_list.remove(child)

        results = self.store.search(query)
        if not results:
            # Show a 'no results' label in the search list
            lbl = Gtk.Label(label=_('No results'))
            lbl.add_css_class('dim-label')
            lbl.set_margin_top(24)
            self._search_results_list.append(lbl)
        else:
            for cmd in results:
                row = self._build_command_row(cmd)
                self._search_results_list.append(row)

        self._main_stack.set_visible_child_name('search')

    def focus_search(self) -> None:
        self._search_toggle.set_active(True)
        self._search_revealer.set_reveal_child(True)
        self._search_entry.grab_focus()

    # ------------------------------------------------------------------
    # Keyboard
    # ------------------------------------------------------------------

    def _on_panel_key_pressed(self, ctrl, keyval, keycode, state) -> bool:
        if keyval == Gdk.KEY_slash:
            self.focus_search()
            return True
        return False

    def _on_list_key_pressed(self, ctrl, keyval, keycode, state) -> bool:
        mods = state & Gtk.accelerator_get_default_mod_mask()
        primary = Gdk.ModifierType.CONTROL_MASK

        active_list = (self._search_results_list
                       if self._main_stack.get_visible_child_name() == 'search'
                       else self._tree_list)
        selected = active_list.get_selected_row()
        cmd = getattr(selected, '_cmd_data', None) if selected else None

        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and mods == 0 and cmd:
            self._send_command_to_terminal(cmd, anchor=selected)
            return True
        if keyval == Gdk.KEY_Delete and mods == 0 and cmd:
            self._delete_command(cmd)
            return True
        if keyval == Gdk.KEY_e and mods == primary and cmd:
            self._open_edit_dialog(cmd)
            return True
        return False

    # ------------------------------------------------------------------
    # Row interactions
    # ------------------------------------------------------------------

    def _on_row_click(self, gesture, n_press, x, y, cmd: dict) -> None:
        if n_press == 2:
            self._send_command_to_terminal(cmd, anchor=gesture.get_widget())

    # ------------------------------------------------------------------
    # Drag source
    # ------------------------------------------------------------------

    def _setup_command_drag_source(self, row: Adw.ActionRow, cmd: dict) -> None:
        drag_source = Gtk.DragSource()
        drag_source.set_actions(Gdk.DragAction.COPY)

        _cmd = cmd

        def _on_prepare(src, x, y):
            payload = {
                'type': 'command_block',
                'command_id': _cmd['id'],
                'name': _cmd.get('name', ''),
                'command': _cmd.get('command', ''),
                'has_placeholders': _cmd.get('has_placeholders', False),
            }
            val = GObject.Value(GObject.TYPE_PYOBJECT)
            val.set_boxed(payload)
            return Gdk.ContentProvider.new_for_value(val)

        drag_source.connect('prepare', _on_prepare)
        row.add_controller(drag_source)

    # ------------------------------------------------------------------
    # Send command to terminal
    # ------------------------------------------------------------------

    def _show_toast(self, message: str, timeout: int = 3) -> None:
        toast = Adw.Toast.new(message)
        toast.set_timeout(timeout)
        try:
            overlay = getattr(self.window, 'toast_overlay', None)
            if overlay is not None:
                overlay.add_toast(toast)
            else:
                self.window.add_toast(toast)
        except Exception:
            pass

    def _send_command_to_terminal(self, cmd: dict, anchor: Gtk.Widget | None = None) -> None:
        terminal = None
        try:
            terminal = self.window._get_active_terminal_widget()
        except Exception:
            pass
        if terminal is None:
            if anchor is not None:
                self._show_run_on_host_picker(cmd, anchor)
            else:
                self._show_toast(_('No active terminal — open a connection first'), timeout=3)
            return
        if cmd.get('has_placeholders'):
            dlg = PlaceholderDialog(self.window, cmd)
            dlg.connect('send', lambda d, filled: self._feed_terminal(filled, cmd.get('id')))
            dlg.present()
        else:
            self._feed_terminal(cmd.get('command', ''), cmd.get('id'))

    def _broadcast_command(self, cmd: dict) -> None:
        if cmd.get('has_placeholders'):
            dlg = PlaceholderDialog(self.window, cmd)
            dlg.connect('send', lambda d, filled: self._do_broadcast(filled, cmd.get('id')))
            dlg.present()
        else:
            self._do_broadcast(cmd.get('command', ''), cmd.get('id'))

    def _do_broadcast(self, command_text: str, cmd_id: str | None = None) -> None:
        command = command_text.strip()
        if not command:
            return

        terminal_manager = getattr(self.window, 'terminal_manager', None)
        if terminal_manager is None:
            return

        sent_count, failed_count = terminal_manager.broadcast_command(command)

        if sent_count == 0 and failed_count == 0:
            message = _('No SSH terminals open — connect to a server first')
        elif failed_count:
            message = _('Command broadcast to {} terminals ({} failed)').format(
                sent_count, failed_count,
            )
        else:
            message = _('Command broadcast to {} terminals').format(sent_count)

        self._show_toast(message, timeout=3)

        if cmd_id and sent_count > 0:
            self.store.record_use(cmd_id)

    def _feed_terminal(self, command_text: str, cmd_id: str | None = None) -> None:
        terminal = None
        try:
            terminal = self.window._get_active_terminal_widget()
        except Exception:
            pass

        if terminal is None:
            self._show_toast(_('No active terminal — open a connection first'), timeout=3)
            return

        insert_only = bool(self.store._config.get_setting('command_blocks.insert_only', False))
        data = command_text.encode('utf-8') if insert_only else (command_text + '\n').encode('utf-8')
        try:
            if hasattr(terminal, 'backend') and terminal.backend:
                terminal.backend.feed_child(data)
            elif hasattr(terminal, 'vte') and terminal.vte:
                terminal.vte.feed_child(data)
        except Exception as exc:
            logger.error("Failed to send command to terminal: %s", exc)
            return

        if cmd_id:
            self.store.record_use(cmd_id)

        if self.store._config.get_setting('command_blocks.auto_hide_sidebar', False):
            if self._auto_hide_timer_id is not None:
                try:
                    GLib.source_remove(self._auto_hide_timer_id)
                except Exception:
                    pass
                self._auto_hide_timer_id = None
            timeout = max(1, min(30, int(self.store._config.get_setting('command_blocks.auto_hide_timeout', 3))))
            def _do_hide():
                try:
                    self.window._toggle_command_blocks_panel(False)
                except Exception:
                    pass
                self._auto_hide_timer_id = None
                return GLib.SOURCE_REMOVE
            self._auto_hide_timer_id = GLib.timeout_add_seconds(timeout, _do_hide)

    # ------------------------------------------------------------------
    # Run command picker (called from sidebar context menu)
    # ------------------------------------------------------------------

    def show_command_picker_for_target(
        self,
        anchor: Gtk.Widget,
        *,
        connection=None,
        group: dict | None = None,
    ) -> None:
        """Show a command picker popover anchored to *anchor*.

        Either *connection* (a Connection object) or *group* (a group dict)
        must be supplied to specify the target.
        """
        commands = self.store.get_commands()

        popover = Gtk.Popover()
        popover.set_parent(anchor)
        popover.set_has_arrow(True)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        outer.set_margin_top(8)
        outer.set_margin_bottom(8)
        outer.set_margin_start(8)
        outer.set_margin_end(8)
        outer.set_size_request(300, -1)

        search_entry = Gtk.SearchEntry()
        search_entry.set_placeholder_text(_('Search commands…'))
        outer.append(search_entry)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_size_request(-1, min(360, (len(commands) + 1) * 52 + 8))

        list_box = Gtk.ListBox()
        list_box.set_selection_mode(Gtk.SelectionMode.SINGLE)
        list_box.add_css_class('boxed-list')

        # "Custom command…" pseudo-row — always first, never filtered out
        custom_row = Gtk.ListBoxRow()
        custom_row._cmd = None
        cbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        cbox.set_margin_top(6)
        cbox.set_margin_bottom(6)
        cbox.set_margin_start(8)
        cbox.set_margin_end(8)
        cicon = Gtk.Image.new_from_icon_name('utilities-terminal-symbolic')
        cicon.set_pixel_size(16)
        cicon.set_valign(Gtk.Align.CENTER)
        cbox.append(cicon)
        clbl = Gtk.Label(label=_('Custom command…'))
        clbl.set_halign(Gtk.Align.START)
        clbl.set_hexpand(True)
        clbl.add_css_class('dim-label')
        cbox.append(clbl)
        custom_row.set_child(cbox)
        list_box.append(custom_row)

        # One row per command block
        for cmd in commands:
            list_row = Gtk.ListBoxRow()
            list_row._cmd = cmd
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            box.set_margin_top(6)
            box.set_margin_bottom(6)
            box.set_margin_start(8)
            box.set_margin_end(8)
            lbl = Gtk.Label(label=cmd.get('name', ''))
            lbl.set_halign(Gtk.Align.START)
            lbl.add_css_class('heading')
            lbl.set_ellipsize(Pango.EllipsizeMode.END)
            box.append(lbl)
            cmd_preview = cmd.get('command', '').split('\n')[0][:60]
            if cmd_preview:
                lbl2 = Gtk.Label(label=cmd_preview)
                lbl2.set_halign(Gtk.Align.START)
                lbl2.add_css_class('caption')
                lbl2.add_css_class('dim-label')
                lbl2.set_ellipsize(Pango.EllipsizeMode.END)
                box.append(lbl2)
            list_row.set_child(box)
            list_box.append(list_row)

        def _filter(list_row):
            if getattr(list_row, '_cmd', None) is None:
                return True  # always show custom row
            q = search_entry.get_text().lower().strip()
            if not q:
                return True
            cmd = list_row._cmd
            haystack = ' '.join([
                cmd.get('name', ''),
                cmd.get('command', ''),
                cmd.get('description', ''),
                ' '.join(cmd.get('tags', [])),
            ]).lower()
            return q in haystack

        list_box.set_filter_func(_filter)
        search_entry.connect('search-changed', lambda _e: list_box.invalidate_filter())

        def _on_activated(_lb, list_row):
            cmd = getattr(list_row, '_cmd', None)
            popover.popdown()
            if cmd is None:
                self._show_custom_command_dialog(connection=connection, group=group)
            else:
                self._run_cmd_block_on_target(cmd, connection=connection, group=group)

        list_box.connect('row-activated', _on_activated)
        scrolled.set_child(list_box)
        outer.append(scrolled)
        popover.set_child(outer)
        GLib.idle_add(popover.popup)

    def _run_cmd_block_on_target(self, cmd: dict, *, connection=None, group=None) -> None:
        if cmd.get('has_placeholders'):
            dlg = PlaceholderDialog(self.window, cmd)
            dlg.connect('send', lambda d, filled: self._dispatch_to_target(
                filled, cmd.get('id'), connection=connection, group=group))
            dlg.present()
        else:
            self._dispatch_to_target(cmd.get('command', ''), cmd.get('id'),
                                     connection=connection, group=group)

    def _dispatch_to_target(self, command_text: str, cmd_id=None, *,
                            connection=None, group=None) -> None:
        if connection is not None:
            self._connect_and_feed(connection, command_text, cmd_id)
        elif group is not None:
            self._feed_group_in_split_view(group, command_text, cmd_id)

    def _show_custom_command_dialog(self, *, connection=None, group=None) -> None:
        dlg = Adw.AlertDialog(
            heading=_('Run Custom Command'),
            body=_('Enter a shell command to run:'),
        )
        entry = Gtk.Entry()
        entry.set_activates_default(True)
        dlg.set_extra_child(entry)
        dlg.add_response('cancel', _('Cancel'))
        dlg.add_response('run', _('Run'))
        dlg.set_default_response('run')
        dlg.set_response_appearance('run', Adw.ResponseAppearance.SUGGESTED)

        def _on_response(d, response):
            if response == 'run':
                text = entry.get_text().strip()
                if text:
                    self._dispatch_to_target(text, None, connection=connection, group=group)

        dlg.connect('response', _on_response)
        dlg.present(self.window)

    def _feed_group_in_split_view(self, group: dict, command_text: str,
                                  cmd_id: str | None = None) -> None:
        from .split_view import SplitViewTab
        from sshpilot import icon_utils

        cm = getattr(self.window, 'connection_manager', None)
        if cm is None:
            return
        nicknames = set(group.get('connections', []))
        connections = [c for c in cm.connections if c.nickname in nicknames]
        if not connections:
            self._show_toast(_('No connections in group'))
            return

        svt = SplitViewTab(self.window)
        page = self.window.tab_view.append(svt)
        page.set_title(group.get('name', _('Group')))
        page.set_icon(icon_utils.new_gicon_from_icon_name('view-dual-symbolic'))
        svt._tab_page = page
        svt.populate(connections)
        self.window.show_tab_view()
        self.window.tab_view.set_selected_page(page)

        for terminal in svt.get_all_terminals():
            def _make_handler(t):
                handler_id = [None]

                def _on_connected(_t):
                    GObject.signal_handler_disconnect(_t, handler_id[0])
                    self._feed_specific_terminal(command_text, _t, cmd_id)

                handler_id[0] = t.connect('connection-established', _on_connected)

            _make_handler(terminal)

    # ------------------------------------------------------------------
    # Run on host
    # ------------------------------------------------------------------

    def _show_run_on_host_picker(self, cmd: dict, anchor: Gtk.Widget) -> None:
        cm = getattr(self.window, 'connection_manager', None)
        if cm is None:
            return
        connections = getattr(cm, 'connections', [])
        if not connections:
            self._show_toast(_('No connections in inventory'))
            return

        active_terminals = getattr(self.window, 'active_terminals', {})

        popover = Gtk.Popover()
        popover.set_parent(anchor)
        popover.set_has_arrow(True)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        outer.set_margin_top(8)
        outer.set_margin_bottom(8)
        outer.set_margin_start(8)
        outer.set_margin_end(8)
        outer.set_size_request(280, -1)

        search_entry = Gtk.SearchEntry()
        search_entry.set_placeholder_text(_('Filter hosts…'))
        outer.append(search_entry)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_size_request(-1, min(300, len(connections) * 56 + 8))

        list_box = Gtk.ListBox()
        list_box.set_selection_mode(Gtk.SelectionMode.SINGLE)
        list_box.add_css_class('boxed-list')

        for conn in connections:
            is_open = conn in active_terminals
            list_row = Gtk.ListBoxRow()
            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row_box.set_margin_top(6)
            row_box.set_margin_bottom(6)
            row_box.set_margin_start(8)
            row_box.set_margin_end(8)

            info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            info.set_hexpand(True)
            lbl = Gtk.Label(label=conn.nickname)
            lbl.set_halign(Gtk.Align.START)
            lbl.add_css_class('heading')
            info.append(lbl)
            host = getattr(conn, 'hostname', '') or getattr(conn, 'host', '')
            user = getattr(conn, 'username', '')
            subtitle = f"{user}@{host}" if user and host else host
            if subtitle:
                lbl2 = Gtk.Label(label=subtitle)
                lbl2.set_halign(Gtk.Align.START)
                lbl2.add_css_class('caption')
                lbl2.add_css_class('dim-label')
                info.append(lbl2)
            row_box.append(info)

            if is_open:
                dot = Gtk.Image.new_from_icon_name('media-record-symbolic')
                dot.set_pixel_size(10)
                dot.set_valign(Gtk.Align.CENTER)
                dot.add_css_class('success')
                row_box.append(dot)

            list_row.set_child(row_box)
            list_row._connection = conn
            list_box.append(list_row)

        def _filter(list_row):
            q = search_entry.get_text().lower().strip()
            if not q:
                return True
            conn = getattr(list_row, '_connection', None)
            if conn is None:
                return False
            host = getattr(conn, 'hostname', '') or getattr(conn, 'host', '')
            return q in conn.nickname.lower() or q in host.lower()

        list_box.set_filter_func(_filter)
        search_entry.connect('search-changed', lambda _e: list_box.invalidate_filter())

        def _on_activated(_lb, list_row):
            conn = getattr(list_row, '_connection', None)
            if conn:
                popover.popdown()
                self._run_command_on_connection(cmd, conn)

        list_box.connect('row-activated', _on_activated)
        scrolled.set_child(list_box)
        outer.append(scrolled)
        popover.set_child(outer)
        GLib.idle_add(popover.popup)

    def _run_command_on_connection(self, cmd: dict, connection) -> None:
        if cmd.get('has_placeholders'):
            dlg = PlaceholderDialog(self.window, cmd)
            dlg.connect('send', lambda d, filled: self._connect_and_feed(connection, filled, cmd.get('id')))
            dlg.present()
        else:
            self._connect_and_feed(connection, cmd.get('command', ''), cmd.get('id'))

    def _connect_and_feed(self, connection, command_text: str, cmd_id: str | None = None) -> None:
        tm = getattr(self.window, 'terminal_manager', None)
        if tm is None:
            return
        active = getattr(self.window, 'active_terminals', {})

        # Always open a new terminal tab
        tm.connect_to_host(connection, force_new=True)
        terminal = active.get(connection)
        if terminal is None:
            # External terminal mode — cannot feed programmatically
            return
        if getattr(terminal, 'is_connected', False):
            self._feed_specific_terminal(command_text, terminal, cmd_id)
            return

        # New terminal: wait for SSH handshake before feeding
        handler_id = [None]

        def _on_connected(t):
            GObject.signal_handler_disconnect(t, handler_id[0])
            self._feed_specific_terminal(command_text, t, cmd_id)

        handler_id[0] = terminal.connect('connection-established', _on_connected)

    def _feed_specific_terminal(self, command_text: str, terminal, cmd_id: str | None = None) -> None:
        data = (command_text + '\n').encode('utf-8')
        try:
            if hasattr(terminal, 'backend') and terminal.backend:
                terminal.backend.feed_child(data)
            elif hasattr(terminal, 'vte') and terminal.vte:
                terminal.vte.feed_child(data)
        except Exception as exc:
            logger.error("Failed to send command to terminal: %s", exc)
            return
        if cmd_id:
            self.store.record_use(cmd_id)

    # ------------------------------------------------------------------
    # Run for group
    # ------------------------------------------------------------------

    def _show_run_for_group_picker(self, cmd: dict, anchor: Gtk.Widget) -> None:
        gm = getattr(self.window, 'group_manager', None)
        if gm is None:
            return
        groups = [g for g in gm.groups.values() if g.get('connections')]
        if not groups:
            self._show_toast(_('No groups with connections'))
            return

        groups = sorted(groups, key=lambda g: g.get('order', 0))

        popover = Gtk.Popover()
        popover.set_parent(anchor)
        popover.set_has_arrow(True)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        outer.set_margin_top(8)
        outer.set_margin_bottom(8)
        outer.set_margin_start(8)
        outer.set_margin_end(8)
        outer.set_size_request(260, -1)

        search_entry = Gtk.SearchEntry()
        search_entry.set_placeholder_text(_('Filter groups…'))
        outer.append(search_entry)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_size_request(-1, min(300, len(groups) * 52 + 8))

        list_box = Gtk.ListBox()
        list_box.set_selection_mode(Gtk.SelectionMode.SINGLE)
        list_box.add_css_class('boxed-list')

        for group in groups:
            list_row = Gtk.ListBoxRow()
            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row_box.set_margin_top(6)
            row_box.set_margin_bottom(6)
            row_box.set_margin_start(8)
            row_box.set_margin_end(8)

            color = group.get('color')
            if color:
                dot = Gtk.Image.new_from_icon_name('media-record-symbolic')
                dot.set_pixel_size(12)
                dot.set_valign(Gtk.Align.CENTER)
                row_box.append(dot)

            info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            info.set_hexpand(True)
            lbl = Gtk.Label(label=group.get('name', ''))
            lbl.set_halign(Gtk.Align.START)
            lbl.add_css_class('heading')
            info.append(lbl)
            count = len(group.get('connections', []))
            lbl2 = Gtk.Label(label=_('%d connections') % count if count != 1 else _('1 connection'))
            lbl2.set_halign(Gtk.Align.START)
            lbl2.add_css_class('caption')
            lbl2.add_css_class('dim-label')
            info.append(lbl2)
            row_box.append(info)

            list_row.set_child(row_box)
            list_row._group = group
            list_box.append(list_row)

        def _filter(list_row):
            q = search_entry.get_text().lower().strip()
            if not q:
                return True
            g = getattr(list_row, '_group', None)
            return g is not None and q in g.get('name', '').lower()

        list_box.set_filter_func(_filter)
        search_entry.connect('search-changed', lambda _e: list_box.invalidate_filter())

        def _on_activated(_lb, list_row):
            g = getattr(list_row, '_group', None)
            if g:
                popover.popdown()
                self._run_command_for_group(cmd, g)

        list_box.connect('row-activated', _on_activated)
        scrolled.set_child(list_box)
        outer.append(scrolled)
        popover.set_child(outer)
        GLib.idle_add(popover.popup)

    def _run_command_for_group(self, cmd: dict, group: dict) -> None:
        if cmd.get('has_placeholders'):
            dlg = PlaceholderDialog(self.window, cmd)
            dlg.connect('send', lambda d, filled: self._feed_group(group, filled, cmd.get('id')))
            dlg.present()
        else:
            self._feed_group(group, cmd.get('command', ''), cmd.get('id'))

    def _feed_group(self, group: dict, command_text: str, cmd_id: str | None = None) -> None:
        cm = getattr(self.window, 'connection_manager', None)
        if cm is None:
            return
        nicknames = set(group.get('connections', []))
        connections = [c for c in cm.connections if c.nickname in nicknames]
        for connection in connections:
            self._connect_and_feed(connection, command_text, cmd_id)

    # ------------------------------------------------------------------
    # Context menu
    # ------------------------------------------------------------------

    def _show_command_context_menu(self, row: Gtk.ListBoxRow, cmd: dict) -> None:
        listbox = row.get_parent()
        if listbox:
            listbox.select_row(row)

        menu = IconContextMenu()
        menu.add_section(
            menu.add_item('document-edit-symbolic', _('Edit'), lambda: self._open_edit_dialog(cmd)),
            menu.add_item('edit-copy-symbolic', _('Duplicate'), lambda: self._duplicate_command(cmd)),
            menu.add_item('document-send-symbolic', _('Broadcast Command'), lambda: self._broadcast_command(cmd)),
            menu.add_item('computer-symbolic', _('Run on host…'), lambda: self._show_run_on_host_picker(cmd, row)),
            menu.add_item('folder-symbolic', _('Run for group…'), lambda: self._show_run_for_group_picker(cmd, row)),
        )

        if cmd.get('is_favorite'):
            fav_item = menu.add_item(
                'non-starred-symbolic', _('Remove from Favorites'),
                lambda: self._toggle_favorite(cmd),
            )
        else:
            fav_item = menu.add_item(
                'starred-symbolic', _('Add to Favorites'),
                lambda: self._toggle_favorite(cmd),
            )

        menu.add_section(fav_item)
        menu.add_section(
            menu.add_item('user-trash-symbolic', _('Delete'), lambda: self._delete_command(cmd)),
        )
        menu.show(row)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _open_edit_dialog(self, cmd: dict | None) -> None:
        dlg = CommandEditDialog(self.window, self.store, cmd)
        dlg.connect('saved', lambda d, c: self.refresh())
        dlg.present()

    def _open_add_folder_dialog(self) -> None:
        dlg = AddFolderDialog(self.window)
        dlg.connect('created', lambda d, name: (self.store.add_folder(name), self.refresh()))
        dlg.present()

    def _delete_command(self, cmd: dict) -> None:
        self.store.delete_command(cmd['id'])
        self.refresh()
        self._show_toast(_('Command deleted'), timeout=2)

    def _duplicate_command(self, cmd: dict) -> None:
        self.store.duplicate_command(cmd['id'])
        self.refresh()

    def _toggle_favorite(self, cmd: dict) -> None:
        new_val = not cmd.get('is_favorite', False)
        self.store.update_command(cmd['id'], is_favorite=new_val)
        cmd['is_favorite'] = new_val
        self.refresh()

