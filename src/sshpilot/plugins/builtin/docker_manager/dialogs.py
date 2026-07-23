"""GTK dialogs for the Docker Console plugin.

Kept separate from page.py to avoid bloating it. Dialogs:

* ``ContainerDetailsDialog`` — Overview / Environment / Raw inspect view with
  copy actions.
* ``TextViewDialog`` — a monospace read-only text viewer (image history,
  compose file).
* ``CreateContainerDialog`` — a form to create + run a container.
* ``DockerConsoleSettingsDialog`` — plugin settings (SSH reuse, polling, logs).
* ``prompt_shell_options`` — optional user/workdir before ``docker exec``.
* ``prompt_text`` — a tiny one-line input dialog (e.g. the image to pull).
"""

from __future__ import annotations

import shlex
from gettext import gettext as _
from typing import Any, Callable, List, Optional, Tuple

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib, Gtk, Adw, Gdk  # noqa: E402

from . import widgets as w  # noqa: E402
from ....shortcut_utils import install_esc_to_close  # noqa: E402


# --------------------------------------------------------------------------
# helpers to read inspect JSON defensively (docker/podman differ slightly)
# --------------------------------------------------------------------------
def _dig(d: Any, *path: str, default: Any = None) -> Any:
    cur = d
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def _info_group(title: str, rows: List[Tuple[str, str]]) -> Adw.PreferencesGroup:
    group = Adw.PreferencesGroup(title=title)
    if not rows:
        empty = Adw.ActionRow(title="—")
        empty.add_css_class("dim-label")
        group.add(empty)
        return group
    for key, value in rows:
        row = Adw.ActionRow(title=key, subtitle=str(value) if value else "—")
        if hasattr(row, "set_subtitle_selectable"):
            row.set_subtitle_selectable(True)  # libadwaita >= 1.3
        group.add(row)
    return group


class _DialogBase(Adw.Window):
    def __init__(self, parent: Optional[Gtk.Window], title: str,
                 width: int = 560, height: int = 640) -> None:
        super().__init__()
        self.set_title(title)
        self.set_default_size(width, height)
        self.set_modal(True)
        if parent is not None:
            self.set_transient_for(parent)
        install_esc_to_close(self)
        self._toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label=title))
        self._toolbar.add_top_bar(header)
        self._header = header
        self.set_content(self._toolbar)


class ContainerDetailsDialog(_DialogBase):
    """Read-only inspect view for a container (Overview / Environment / Raw)."""

    def __init__(self, parent: Optional[Gtk.Window], name: str, data: dict,
                 *, initial_page: str = "overview") -> None:
        super().__init__(parent, f"{name} — details", width=640, height=700)
        self._data = data
        self._name = name

        copy_env = Gtk.Button(label=_("Copy env"))
        copy_env.set_tooltip_text(_("Copy environment variables"))
        copy_env.connect("clicked", lambda _b: self._copy_env())
        self._header.pack_end(copy_env)
        copy_json = Gtk.Button(label=_("Copy JSON"))
        copy_json.set_tooltip_text(_("Copy full inspect JSON"))
        copy_json.connect("clicked", lambda _b: self._copy_json())
        self._header.pack_end(copy_json)

        notebook = Gtk.Notebook()
        notebook.set_vexpand(True)

        overview = Gtk.ScrolledWindow(vexpand=True)
        overview_body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                                margin_top=16, margin_bottom=16,
                                margin_start=16, margin_end=16)
        overview.set_child(overview_body)
        for group in self._build_overview_groups(data):
            overview_body.append(group)
        notebook.append_page(overview, Gtk.Label(label=_("Overview")))

        env_scroller = Gtk.ScrolledWindow(vexpand=True)
        env_body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                           margin_top=16, margin_bottom=16,
                           margin_start=16, margin_end=16)
        env_scroller.set_child(env_body)
        env_group = Adw.PreferencesGroup(title=_("Environment"))
        env_rows = self._env_pairs(data)
        if not env_rows:
            empty = Adw.ActionRow(title="—")
            empty.add_css_class("dim-label")
            env_group.add(empty)
        else:
            for key, value in env_rows:
                row = Adw.ActionRow(title=key, subtitle=value or "—")
                if hasattr(row, "set_subtitle_selectable"):
                    row.set_subtitle_selectable(True)
                env_group.add(row)
        env_body.append(env_group)
        notebook.append_page(env_scroller, Gtk.Label(label=_("Environment")))

        raw_scroller = Gtk.ScrolledWindow(vexpand=True)
        raw_view = Gtk.TextView()
        raw_view.set_editable(False)
        raw_view.set_monospace(True)
        raw_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        raw_view.set_top_margin(8)
        raw_view.set_bottom_margin(8)
        raw_view.set_left_margin(12)
        raw_view.set_right_margin(12)
        import json as _json
        try:
            raw_text = _json.dumps(data, indent=2, default=str)
        except Exception:  # noqa: BLE001
            raw_text = str(data)
        raw_view.get_buffer().set_text(raw_text)
        raw_scroller.set_child(raw_view)
        notebook.append_page(raw_scroller, Gtk.Label(label=_("Raw")))

        page = {"overview": 0, "env": 1, "environment": 1, "raw": 2}.get(
            (initial_page or "overview").lower(), 0)
        notebook.set_current_page(page)
        self._toolbar.set_content(notebook)

    def _copy_env(self) -> None:
        lines = [f"{k}={v}" for k, v in self._env_pairs(self._data)]
        text = "\n".join(lines)
        display = Gdk.Display.get_default()
        if display is not None and text:
            display.get_clipboard().set(text)

    def _copy_json(self) -> None:
        import json as _json
        try:
            text = _json.dumps(self._data, indent=2, default=str)
        except Exception:  # noqa: BLE001
            text = str(self._data)
        display = Gdk.Display.get_default()
        if display is not None and text:
            display.get_clipboard().set(text)

    @staticmethod
    def _env_pairs(d: dict) -> List[Tuple[str, str]]:
        rows = []
        for entry in (_dig(d, "Config", "Env", default=[]) or []):
            k, _unused, v = str(entry).partition("=")
            rows.append((k, v))
        return rows

    @staticmethod
    def _build_overview_groups(d: dict) -> List[Adw.PreferencesGroup]:
        name = (_dig(d, "Name", default="") or "").lstrip("/")
        rp = _dig(d, "HostConfig", "RestartPolicy", "Name", default="") or "no"
        general = _info_group("General", [
            ("Name", name),
            ("ID", (_dig(d, "Id", default="") or "")[:12]),
            ("Image", _dig(d, "Config", "Image", default="")),
            ("Created", _dig(d, "Created", default="")),
            ("Restart policy", rp),
            ("Command", " ".join(_dig(d, "Config", "Cmd", default=[]) or [])),
        ])
        state = _dig(d, "State", default={}) or {}
        state_group = _info_group("State", [
            ("Status", state.get("Status", "")),
            ("Started", state.get("StartedAt", "")),
            ("Exit code", str(state.get("ExitCode", ""))),
        ])

        port_rows: List[Tuple[str, str]] = []
        for cport, binds in (_dig(d, "HostConfig", "PortBindings", default={}) or {}).items():
            hosts = ", ".join(
                f"{b.get('HostIp') or '0.0.0.0'}:{b.get('HostPort', '')}" for b in (binds or []))
            port_rows.append((cport, hosts or "(published)"))
        ports = _info_group("Ports", port_rows)

        nets = list((_dig(d, "NetworkSettings", "Networks", default={}) or {}).keys())
        networks = _info_group("Networks", [(n, "") for n in nets])

        mount_rows = [(m.get("Source", ""), m.get("Destination", ""))
                      for m in (_dig(d, "Mounts", default=[]) or [])]
        mounts = _info_group("Mounts / volumes", mount_rows)

        labels = _dig(d, "Config", "Labels", default={}) or {}
        labels_group = _info_group("Labels", sorted(labels.items()))

        return [general, state_group, ports, networks, mounts, labels_group]


class TextViewDialog(_DialogBase):
    """A monospace, read-only text viewer (image history, compose file).

    Text is mouse-selectable and copyable (Ctrl+C / context menu)."""

    def __init__(self, parent: Optional[Gtk.Window], title: str, text: str) -> None:
        super().__init__(parent, title, width=680, height=560)
        scroller = Gtk.ScrolledWindow(vexpand=True)
        view = Gtk.TextView()
        view.set_editable(False)
        view.set_cursor_visible(True)
        view.set_monospace(True)
        view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        view.set_top_margin(8)
        view.set_bottom_margin(8)
        view.set_left_margin(12)
        view.set_right_margin(12)
        view.get_buffer().set_text(text or _("(empty)"))
        scroller.set_child(view)
        self._toolbar.set_content(scroller)
        self._view = view
        # Focus the view so Ctrl+A / Ctrl+C work immediately.
        def _focus_view() -> bool:
            view.grab_focus()
            return False

        GLib.idle_add(_focus_view)



class _PairList(Gtk.Box):
    """A small add/remove editor for two-field rows (port:port, src:dst, K=V)."""

    def __init__(self, left_ph: str, right_ph: str, sep_label: str) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._left_ph = left_ph
        self._right_ph = right_ph
        self._sep = sep_label
        self._rows: List[Tuple[Gtk.Entry, Gtk.Entry]] = []
        self._rows_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.append(self._rows_box)
        # Standard Adwaita: a flat, circular "+" rather than a text label.
        add = Gtk.Button(icon_name="list-add-symbolic")
        add.set_halign(Gtk.Align.START)
        add.set_tooltip_text(_("Add"))
        add.add_css_class("flat")
        add.add_css_class("circular")
        add.connect("clicked", lambda _b: self.add_row())
        self.append(add)

    def add_row(self, left: str = "", right: str = "") -> None:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        le = Gtk.Entry(hexpand=True, placeholder_text=self._left_ph, text=left)
        re = Gtk.Entry(hexpand=True, placeholder_text=self._right_ph, text=right)
        row.append(le)
        row.append(Gtk.Label(label=self._sep))
        row.append(re)
        rm = Gtk.Button(icon_name="user-trash-symbolic")
        rm.add_css_class("flat")
        row.append(rm)
        pair = (le, re)

        def _remove(_b):
            self._rows_box.remove(row)
            if pair in self._rows:
                self._rows.remove(pair)

        rm.connect("clicked", _remove)
        self._rows.append(pair)
        self._rows_box.append(row)

    def values(self) -> List[Tuple[str, str]]:
        out = []
        for le, re in self._rows:
            a, b = le.get_text().strip(), re.get_text().strip()
            if a or b:
                out.append((a, b))
        return out


_RESTART_POLICIES = ("no", "on-failure", "always", "unless-stopped")


class CreateContainerDialog(_DialogBase):
    """Form to create + run a container. Calls ``on_create(spec)`` where ``spec``
    is a dict (image, name, ports, volumes, envs, restart, command, network,
    interactive, tty, user, memory, cpus) — the keys match ``create_run_args``."""

    def __init__(self, parent: Optional[Gtk.Window], images: List[str],
                 networks: Optional[List[str]] = None,
                 on_create: Optional[Callable[[dict], None]] = None) -> None:
        super().__init__(parent, _("Create container"), width=560, height=680)
        self._on_create = on_create
        self._networks = networks or ["bridge", "host", "none"]

        create_btn = Gtk.Button(label=_("Create"))
        create_btn.add_css_class("suggested-action")
        create_btn.connect("clicked", self._on_create_clicked)
        self._header.pack_end(create_btn)
        cancel_btn = Gtk.Button(label=_("Cancel"))
        cancel_btn.connect("clicked", lambda _b: self.close())
        self._header.pack_start(cancel_btn)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16,
                       margin_top=12, margin_bottom=16, margin_start=16, margin_end=16)
        scroller.set_child(body)
        self._toolbar.set_content(scroller)

        basics = Adw.PreferencesGroup(title=_("Container"))
        self._image_row = Adw.EntryRow(title=_("Image *"))
        if images:
            self._image_row.set_text(images[0])
        basics.add(self._image_row)
        self._name_row = Adw.EntryRow(title=_("Name"))
        basics.add(self._name_row)
        self._restart_row = Adw.ComboRow(title=_("Restart policy"))
        self._restart_row.set_model(Gtk.StringList.new(list(_RESTART_POLICIES)))
        basics.add(self._restart_row)
        self._command_row = Adw.EntryRow(title=_("Command (optional)"))
        self._command_row.set_tooltip_text(_(
            "Parsed like a shell line; quotes are respected and each argument is "
            "passed safely (not run through a remote shell)"))
        basics.add(self._command_row)
        body.append(basics)

        body.append(self._section(_("Ports (host : container)"), "_ports",
                                  _PairList("8080", "80", ":")))
        body.append(self._section(_("Volumes (host path : container path)"), "_volumes",
                                  _PairList("/host/path", "/container/path", ":")))
        body.append(self._section(_("Environment (KEY = value)"), "_envs",
                                  _PairList("KEY", "value", "=")))
        body.append(self._build_advanced())

    def _build_advanced(self) -> Adw.PreferencesGroup:
        """Collapsed 'Advanced options': network, -i/-t, user, memory, cpu."""
        group = Adw.PreferencesGroup()
        expander = Adw.ExpanderRow(title=_("Advanced options"))
        group.add(expander)

        self._network_row = Adw.ComboRow(title=_("Network"))
        self._network_row.set_model(Gtk.StringList.new(self._networks))
        if "bridge" in self._networks:
            self._network_row.set_selected(self._networks.index("bridge"))
        expander.add_row(self._network_row)

        self._interactive_switch = self._switch_row(
            expander, _("Keep STDIN open (-i)"), _("Required for interactive containers"))
        self._tty_switch = self._switch_row(
            expander, _("Allocate a pseudo-TTY (-t)"),
            _("Together with -i, keeps a shell container (e.g. alpine) alive"))

        self._user_row = Adw.EntryRow(title=_("Run as user"))
        self._user_row.set_tooltip_text(_("UID:GID or a username (--user)"))
        expander.add_row(self._user_row)
        self._memory_row = Adw.EntryRow(title=_("Memory limit"))
        self._memory_row.set_tooltip_text(_("e.g. 512m, 2g (--memory)"))
        expander.add_row(self._memory_row)
        self._cpus_row = Adw.EntryRow(title=_("CPU limit"))
        self._cpus_row.set_tooltip_text(_("number of cores, e.g. 1.5 (--cpus)"))
        expander.add_row(self._cpus_row)
        return group

    @staticmethod
    def _switch_row(expander: Adw.ExpanderRow, title: str, subtitle: str) -> Gtk.Switch:
        """An Adw.ActionRow carrying a trailing Gtk.Switch (works on libadwaita
        1.0+, unlike Adw.SwitchRow which needs 1.4). Returns the switch."""
        row = Adw.ActionRow(title=title, subtitle=subtitle)
        switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        row.add_suffix(switch)
        row.set_activatable_widget(switch)
        expander.add_row(row)
        return switch

    def _section(self, title: str, attr: str, widget: _PairList) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title=title)
        widget.add_row()  # start with one empty row
        setattr(self, attr, widget)
        group.add(widget)
        return group

    def _on_create_clicked(self, _b) -> None:
        image = self._image_row.get_text().strip()
        if not image:
            self._image_row.add_css_class("error")
            return
        restart = _RESTART_POLICIES[self._restart_row.get_selected()] \
            if self._restart_row.get_selected() < len(_RESTART_POLICIES) else "no"
        net_idx = self._network_row.get_selected()
        network = self._networks[net_idx] if 0 <= net_idx < len(self._networks) else None
        # Parse the command field into an argv now so a malformed line is caught
        # here (and never reaches the host). The client quotes each token.
        self._command_row.remove_css_class("error")
        command_text = self._command_row.get_text().strip()
        try:
            command = shlex.split(command_text) if command_text else None
        except ValueError:
            self._command_row.add_css_class("error")
            return  # unbalanced quotes — keep the dialog open for a fix
        spec = {
            "image": image,
            "name": self._name_row.get_text().strip() or None,
            "ports": self._ports.values(),
            "volumes": self._volumes.values(),
            "envs": self._envs.values(),
            "restart": restart,
            "command": command,
            "network": network,
            "interactive": self._interactive_switch.get_active(),
            "tty": self._tty_switch.get_active(),
            "user": self._user_row.get_text().strip() or None,
            "memory": self._memory_row.get_text().strip() or None,
            "cpus": self._cpus_row.get_text().strip() or None,
        }
        self.close()
        if self._on_create:
            self._on_create(spec)


class DockerConsoleSettingsDialog(_DialogBase):
    """Plugin settings for the Docker Console page."""

    def __init__(self, parent: Optional[Gtk.Window], *,
                 reuse_ssh: bool,
                 on_reuse_ssh_changed: Callable[[bool], None],
                 refresh_interval: int = 10,
                 on_refresh_interval_changed: Optional[Callable[[int], None]] = None,
                 log_tail: int = 200,
                 on_log_tail_changed: Optional[Callable[[int], None]] = None,
                 max_log_lines: int = 2000,
                 on_max_log_lines_changed: Optional[Callable[[int], None]] = None,
                 ) -> None:
        super().__init__(parent, _("Docker Console Settings"), width=420, height=420)
        close = Gtk.Button(icon_name="window-close-symbolic")
        close.set_tooltip_text(_("Close"))
        close.connect("clicked", lambda _b: self.close())
        self._header.pack_end(close)

        self._reuse_row = Adw.SwitchRow()
        self._reuse_row.set_title(_("Reuse SSH connection"))
        self._reuse_row.set_subtitle(
            _("One open connection per host — faster status checks")
        )
        self._reuse_row.set_active(reuse_ssh)
        self._reuse_row.connect(
            "notify::active",
            lambda row, _pspec: on_reuse_ssh_changed(row.get_active()),
        )

        group = Adw.PreferencesGroup(title=_("Connection"))
        group.add(self._reuse_row)

        self._interval_row = Adw.SpinRow.new_with_range(2, 60, 1)
        self._interval_row.set_title(_("Auto-refresh interval"))
        self._interval_row.set_subtitle(_("Seconds between updates of the visible tab"))
        self._interval_row.set_value(int(refresh_interval))
        if on_refresh_interval_changed is not None:
            self._interval_row.connect(
                "notify::value",
                lambda row, _pspec: on_refresh_interval_changed(int(row.get_value())),
            )
        polling = Adw.PreferencesGroup(title=_("Polling"))
        polling.add(self._interval_row)

        logs = Adw.PreferencesGroup(title=_("Logs"))
        self._tail_row = Adw.SpinRow.new_with_range(10, 5000, 50)
        self._tail_row.set_title(_("Default log tail"))
        self._tail_row.set_subtitle(_("Lines fetched when loading or starting Follow"))
        self._tail_row.set_value(int(log_tail))
        if on_log_tail_changed is not None:
            self._tail_row.connect(
                "notify::value",
                lambda row, _pspec: on_log_tail_changed(int(row.get_value())),
            )
        logs.add(self._tail_row)
        self._max_lines_row = Adw.SpinRow.new_with_range(100, 20000, 100)
        self._max_lines_row.set_title(_("Max buffered lines"))
        self._max_lines_row.set_subtitle(
            _("Oldest lines are dropped when the ring buffer is full"))
        self._max_lines_row.set_value(int(max_log_lines))
        if on_max_log_lines_changed is not None:
            self._max_lines_row.connect(
                "notify::value",
                lambda row, _pspec: on_max_log_lines_changed(int(row.get_value())),
            )
        logs.add(self._max_lines_row)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                       margin_top=16, margin_bottom=16, margin_start=16, margin_end=16)
        body.append(group)
        body.append(polling)
        body.append(logs)
        scroller.set_child(body)
        self._toolbar.set_content(scroller)


def prompt_shell_options(
        parent: Optional[Gtk.Window],
        container_name: str,
        on_open: Callable[[Optional[str], Optional[str]], None]) -> None:
    """Optional user/workdir before ``docker exec`` into a container."""
    dialog = w.build_alert(
        f"Shell: {container_name}",
        _("Optional user and working directory for the container shell."),
    )
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    user = Gtk.Entry(placeholder_text=_("User (optional, e.g. root or 1000:1000)"))
    workdir = Gtk.Entry(placeholder_text=_("Working directory (optional)"))
    box.append(user)
    box.append(workdir)
    dialog.set_extra_child(box)
    dialog.add_response("cancel", _("Cancel"))
    dialog.add_response("ok", _("Open"))
    dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("ok")
    dialog.set_close_response("cancel")

    def _resp(_d, response):
        if response == "ok":
            on_open(user.get_text().strip() or None,
                    workdir.get_text().strip() or None)

    dialog.connect("response", _resp)
    w.present_alert(dialog, parent)


def prompt_text(parent: Optional[Gtk.Window], heading: str, body: str,
                placeholder: str, ok_label: str,
                on_ok: Callable[[str], None]) -> None:
    """One-line input dialog (e.g. the image to pull)."""
    dialog = w.build_alert(heading, body)
    entry = Gtk.Entry(placeholder_text=placeholder, activates_default=True)
    dialog.set_extra_child(entry)
    dialog.add_response("cancel", _("Cancel"))
    dialog.add_response("ok", ok_label)
    dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("ok")
    dialog.set_close_response("cancel")

    def _resp(_d, response):
        if response == "ok":
            text = entry.get_text().strip()
            if text:
                on_ok(text)

    dialog.connect("response", _resp)
    w.present_alert(dialog, parent)
