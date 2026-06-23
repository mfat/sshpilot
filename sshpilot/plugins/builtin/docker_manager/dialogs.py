"""GTK dialogs for the Docker Manager plugin.

Kept separate from page.py to avoid bloating it. Three dialogs:

* ``ContainerDetailsDialog`` — read-only inspect view (env, mounts, networks,
  ports, labels, image, created, restart policy).
* ``TextViewDialog`` — a monospace read-only text viewer (image history,
  compose file).
* ``CreateContainerDialog`` — a form to create + run a container.
* ``prompt_text`` — a tiny one-line input dialog (e.g. the image to pull).
"""

from __future__ import annotations

from typing import Any, Callable, List, Optional, Tuple

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw  # noqa: E402


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
        self._toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label=title))
        self._toolbar.add_top_bar(header)
        self._header = header
        self.set_content(self._toolbar)


class ContainerDetailsDialog(_DialogBase):
    """Read-only inspect view for a container."""

    def __init__(self, parent: Optional[Gtk.Window], name: str, data: dict) -> None:
        super().__init__(parent, f"{name} — details")
        scroller = Gtk.ScrolledWindow(vexpand=True)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                       margin_top=16, margin_bottom=16, margin_start=16, margin_end=16)
        scroller.set_child(body)
        self._toolbar.set_content(scroller)
        for group in self._build_groups(data):
            body.append(group)

    @staticmethod
    def _build_groups(d: dict) -> List[Adw.PreferencesGroup]:
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

        # Ports: HostConfig.PortBindings {"80/tcp": [{"HostIp","HostPort"}]}
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

        env_rows = []
        for entry in (_dig(d, "Config", "Env", default=[]) or []):
            k, _, v = str(entry).partition("=")
            env_rows.append((k, v))
        envs = _info_group("Environment", env_rows)

        labels = _dig(d, "Config", "Labels", default={}) or {}
        labels_group = _info_group("Labels", sorted(labels.items()))

        return [general, state_group, ports, networks, mounts, envs, labels_group]


class TextViewDialog(_DialogBase):
    """A monospace, read-only text viewer (image history, compose file)."""

    def __init__(self, parent: Optional[Gtk.Window], title: str, text: str) -> None:
        super().__init__(parent, title, width=680, height=560)
        scroller = Gtk.ScrolledWindow(vexpand=True)
        view = Gtk.TextView()
        view.set_editable(False)
        view.set_monospace(True)
        view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        view.get_buffer().set_text(text or "(empty)")
        scroller.set_child(view)
        self._toolbar.set_content(scroller)


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
        add.set_tooltip_text("Add")
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
    is a dict (image, name, ports, volumes, envs, restart, command)."""

    def __init__(self, parent: Optional[Gtk.Window], images: List[str],
                 on_create: Callable[[dict], None]) -> None:
        super().__init__(parent, "Create container", width=560, height=680)
        self._on_create = on_create

        create_btn = Gtk.Button(label="Create")
        create_btn.add_css_class("suggested-action")
        create_btn.connect("clicked", self._on_create_clicked)
        self._header.pack_end(create_btn)
        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda _b: self.close())
        self._header.pack_start(cancel_btn)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16,
                       margin_top=12, margin_bottom=16, margin_start=16, margin_end=16)
        scroller.set_child(body)
        self._toolbar.set_content(scroller)

        basics = Adw.PreferencesGroup(title="Container")
        self._image_row = Adw.EntryRow(title="Image *")
        if images:
            self._image_row.set_text(images[0])
        basics.add(self._image_row)
        self._name_row = Adw.EntryRow(title="Name")
        basics.add(self._name_row)
        self._restart_row = Adw.ComboRow(title="Restart policy")
        self._restart_row.set_model(Gtk.StringList.new(list(_RESTART_POLICIES)))
        basics.add(self._restart_row)
        self._command_row = Adw.EntryRow(title="Command (optional)")
        basics.add(self._command_row)
        body.append(basics)

        body.append(self._section("Ports (host : container)", "_ports",
                                  _PairList("8080", "80", ":")))
        body.append(self._section("Volumes (host path : container path)", "_volumes",
                                  _PairList("/host/path", "/container/path", ":")))
        body.append(self._section("Environment (KEY = value)", "_envs",
                                  _PairList("KEY", "value", "=")))

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
        spec = {
            "image": image,
            "name": self._name_row.get_text().strip() or None,
            "ports": self._ports.values(),
            "volumes": self._volumes.values(),
            "envs": self._envs.values(),
            "restart": restart,
            "command": self._command_row.get_text().strip() or None,
        }
        self.close()
        self._on_create(spec)


def prompt_text(parent: Optional[Gtk.Window], heading: str, body: str,
                placeholder: str, ok_label: str,
                on_ok: Callable[[str], None]) -> None:
    """One-line input via Adw.MessageDialog (e.g. the image to pull)."""
    dialog = Adw.MessageDialog(transient_for=parent, modal=True,
                               heading=heading, body=body)
    entry = Gtk.Entry(placeholder_text=placeholder, activates_default=True)
    dialog.set_extra_child(entry)
    dialog.add_response("cancel", "Cancel")
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
    dialog.present()
