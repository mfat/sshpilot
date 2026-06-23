"""Docker Console tab: Containers."""

from __future__ import annotations

from typing import Any, List, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Pango  # noqa: E402

from .dialogs import ContainerDetailsDialog, CreateContainerDialog  # noqa: E402
from . import widgets as w  # noqa: E402


class ContainersTabMixin:
    def _build_containers_section(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._show_all_check = Gtk.CheckButton(label="Show stopped")
        self._show_all_check.set_active(True)
        self._show_all_check.connect("toggled", lambda _c: self._refresh_containers())
        toolbar.append(self._show_all_check)
        self._container_search = Gtk.SearchEntry()
        self._container_search.set_placeholder_text("Filter by name / image / id…")
        self._container_search.set_hexpand(True)
        self._container_search.connect("search-changed", self._on_container_search)
        toolbar.append(self._container_search)
        create = Gtk.Button()
        create.set_child(Adw.ButtonContent(icon_name="list-add-symbolic",
                                           label="New Container"))
        create.add_css_class("suggested-action")
        create.set_halign(Gtk.Align.END)
        create.connect("clicked", lambda _b: self._create_container())
        toolbar.append(create)
        box.append(toolbar)

        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        self._containers_list = Gtk.ListBox()
        self._containers_list.add_css_class("boxed-list")
        self._containers_list.set_selection_mode(Gtk.SelectionMode.NONE)
        scroller.set_child(self._containers_list)
        self._containers_placeholder = self._make_loading_placeholder("Loading containers…")
        box.append(w.wrap_with_overlay(scroller, self._containers_placeholder))
        return box

    def _refresh_containers(self) -> None:
        client = self._client()
        if client is None or self._containers_busy:
            return
        self._containers_busy = True
        # Only show the loading logo on the first load (empty list); auto-refresh
        # of an already-populated list updates silently (no flashing).
        if not self._containers:
            self._set_placeholder_loading(self._containers_placeholder, "Loading containers…")
        show_all = self._show_all_check.get_active()
        gen = self._load_gen
        self._run_async(lambda: client.ps(all=show_all),
                        lambda r, e, g=gen: self._on_containers(r, e, g))

    def _on_containers(self, rows: Optional[List[dict]], err: Optional[Exception],
                       gen: int = 0) -> None:
        if gen != self._load_gen:
            return  # stale result for a previous host — drop it
        self._containers_busy = False
        w.clear_listbox(self._containers_list)
        if err is not None:
            self._containers = []
            self._set_placeholder_idle(self._containers_placeholder, w.error_text(err))
            self._refresh_logs_targets()
            return
        self._containers = rows or []
        self._render_containers()
        self._refresh_logs_targets()

    def _on_container_search(self, entry: Gtk.SearchEntry) -> None:
        self._container_query = entry.get_text().strip().lower()
        self._render_containers()

    def _container_matches(self, c: dict) -> bool:
        if not self._container_query:
            return True
        hay = " ".join((
            w.field(c, "Names", "Name"), w.field(c, "Image"),
            w.field(c, "ID", "Id", "ContainerID"))).lower()
        return self._container_query in hay

    def _render_containers(self) -> None:
        """(Re)build the visible rows from the cached list + the search filter."""
        w.clear_listbox(self._containers_list)
        if not self._containers:
            self._set_placeholder_idle(self._containers_placeholder, "No containers")
            return
        visible = [c for c in self._containers if self._container_matches(c)]
        if not visible:
            self._set_placeholder_idle(self._containers_placeholder, "No matching containers")
            return
        self._hide_placeholder(self._containers_placeholder)
        for c in visible:
            self._containers_list.append(self._container_row(c))

    @staticmethod
    def _health_of(status: str) -> Optional[str]:
        return w.health_of(status)

    def _container_row(self, c: dict) -> Gtk.Widget:
        cid = w.field(c, "ID", "Id", "ContainerID")
        name = w.field(c, "Names", "Name", default=cid[:12])
        image = w.field(c, "Image")
        status = w.field(c, "Status", "State")
        ports = w.field(c, "Ports")
        state = w.field(c, "State", "Status").lower()
        running = "up" in state or "running" in state
        paused = "paused" in state

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row.set_margin_top(6)
        row.set_margin_bottom(6)
        row.set_margin_start(8)
        row.set_margin_end(8)

        dot = Gtk.Image.new_from_icon_name(
            "media-record-symbolic" if running else "media-playback-stop-symbolic"
        )
        dot.add_css_class("success" if running else ("warning" if paused else "dim-label"))
        row.append(dot)

        # Health badge from the ps Status string (no extra round-trip): docker
        # appends "(healthy)" / "(unhealthy)" / "(health: starting)" for containers
        # that define a HEALTHCHECK.
        health = w.health_of(status)
        if health:
            hicon, hcls, htip = {
                "healthy": ("emblem-ok-symbolic", "success", "Healthy"),
                "unhealthy": ("dialog-warning-symbolic", "error", "Unhealthy"),
                "starting": ("content-loading-symbolic", "warning", "Health check starting"),
            }[health]
            hb = Gtk.Image.new_from_icon_name(hicon)
            hb.add_css_class(hcls)
            hb.set_tooltip_text(htip)
            row.append(hb)

        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        title = Gtk.Label(label=name, xalign=0)
        title.add_css_class("heading")
        info.append(title)
        sub = Gtk.Label(xalign=0)
        sub.add_css_class("dim-label")
        sub.add_css_class("caption")
        sub.set_text(" · ".join(p for p in (image, status, ports) if p))
        sub.set_ellipsize(Pango.EllipsizeMode.END)
        info.append(sub)
        row.append(info)

        if running or paused:
            w.add_row_action(row, "media-playback-stop-symbolic", "Stop",
                             lambda: self._lifecycle("stop", cid, name))
            w.add_row_action(row, "view-refresh-symbolic", "Restart",
                             lambda: self._lifecycle("restart", cid, name))
            if paused:
                w.add_row_action(row, "media-playback-start-symbolic", "Resume",
                                 lambda: self._lifecycle("unpause", cid, name))
            else:
                w.add_row_action(row, "media-playback-pause-symbolic", "Pause",
                                 lambda: self._lifecycle("pause", cid, name))
            w.add_row_action(row, "process-stop-symbolic", "Kill",
                             lambda: self._lifecycle("kill", cid, name))
            w.add_row_action(row, "utilities-terminal-symbolic", "Open shell",
                             lambda: self._open_shell(cid, name), refreshes=False)
            w.add_row_action(row, "view-paged-symbolic", "Follow logs",
                             lambda: self._follow_logs(cid, name), refreshes=False)
        else:
            w.add_row_action(row, "media-playback-start-symbolic", "Start",
                             lambda: self._lifecycle("start", cid, name))
            w.add_row_action(row, "view-paged-symbolic", "Logs",
                             lambda: self._follow_logs(cid, name), refreshes=False)
        w.add_row_action(row, "dialog-information-symbolic", "Details",
                         lambda: self._show_details(cid, name), refreshes=False)
        w.add_row_action(row, "user-trash-symbolic", "Remove",
                         lambda: self._remove_container(cid, name))
        return w.listbox_wrap(row)

    def _show_details(self, cid: str, name: str) -> None:
        client = self._client()
        if client is None:
            return
        self._run_async(
            lambda: client.inspect(cid),
            lambda data, err: self._on_details(name, data, err),
        )

    def _on_details(self, name: str, data: Optional[dict], err: Optional[Exception]) -> None:
        if err is not None:
            self._toast(f"Inspect {name} failed: {err}")
            return
        if not data:
            self._toast(f"No details for {name}")
            return
        ContainerDetailsDialog(self._window(), name, data).present()

    # --- container creation -------------------------------------------
    def _create_container(self) -> None:
        client = self._client()
        if client is None:
            self._toast("Select a host first")
            return
        images = [f"{w.field(i, 'Repository')}:{w.field(i, 'Tag')}"
                  for i in self._cached_images
                  if w.field(i, "Repository") not in ("", "<none>")]

        def on_create(spec: dict) -> None:
            self._run_async(
                lambda: client.create_container(spec.pop("image"), **spec),
                lambda res, err: self._on_action(
                    "create container", res, err, self._refresh_containers),
            )

        # Fetch the host's networks first so the dialog's Network picker is
        # populated; fall back to the built-in names if the probe fails.
        def open_dialog(rows: Optional[List[dict]], _err: Optional[Exception]) -> None:
            names = [w.field(n, "Name") for n in (rows or []) if w.field(n, "Name")]
            if not names:
                names = ["bridge", "host", "none"]
            CreateContainerDialog(self._window(), images, names, on_create).present()

        self._run_async(client.networks, open_dialog)

    def _lifecycle(self, action: str, cid: str, name: str) -> None:
        client = self._client()
        if client is None or cid in self._busy_cids:
            return  # an op is already in flight for this container — drop re-entrant
        self._busy_cids.add(cid)

        def done(res: Any, err: Optional[Exception]) -> None:
            self._busy_cids.discard(cid)
            self._on_action(f"{action} {name}", res, err, self._refresh_containers)

        self._run_async(lambda: client.lifecycle(action, cid), done)

    def _remove_container(self, cid: str, name: str) -> None:
        client = self._client()
        if client is None:
            return

        def do(force: bool) -> None:
            if cid in self._busy_cids:
                return
            self._busy_cids.add(cid)

            def done(res: Any, err: Optional[Exception]) -> None:
                self._busy_cids.discard(cid)
                self._on_action(f"remove {name}", res, err, self._refresh_containers)

            self._run_async(lambda: client.lifecycle("rm", cid, force=force), done)

        self._confirm(
            heading="Remove container?",
            body=f"This will remove “{name}”.",
            destructive_label="Remove",
            on_confirm=do,
            force_label="Force (-f) — remove even if running",
        )

    def _warn_sudo_interactive(self, nick: str) -> None:
        """Interactive ops use plain ``sudo`` (the PTY prompts). If the probe found
        even ``sudo -n`` denied, warn so a sudo password prompt isn't mistaken for
        a cryptic docker error."""
        if self._sudo_denied and self._use_sudo_for(nick):
            self._toast("sudo needs a password — the terminal will prompt for it.")

    def _open_shell(self, cid: str, name: str) -> None:
        client = self._client()
        nick = self._current_nickname()
        if client is None or not nick:
            return
        self._warn_sudo_interactive(nick)
        ok = self.ctx.open_command_terminal(
            nick, client.exec_shell_command(cid), title=f"sh: {name}"
        )
        if not ok:
            self._toast("Could not open shell")

    def _follow_logs(self, cid: str, name: str) -> None:
        client = self._client()
        nick = self._current_nickname()
        if client is None or not nick:
            return
        self._warn_sudo_interactive(nick)
        cmd = client.logs_follow_command(
            cid, tail=int(self._tail_spin.get_value()),
            timestamps=self._ts_switch.get_active(),
        )
        ok = self.ctx.open_command_terminal(nick, cmd, title=f"logs: {name}")
        if not ok:
            self._toast("Could not open logs")

    # ================================================================
