"""Docker Manager tab: Compose."""

from __future__ import annotations

from typing import List, Optional

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Pango  # noqa: E402

from .dialogs import TextViewDialog  # noqa: E402
from . import widgets as w  # noqa: E402


class ComposeTabMixin:
    def _build_compose_section(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        self._compose_list = Gtk.ListBox()
        self._compose_list.add_css_class("boxed-list")
        self._compose_list.set_selection_mode(Gtk.SelectionMode.NONE)
        scroller.set_child(self._compose_list)
        self._compose_placeholder = self._make_loading_placeholder("Loading compose projects…")
        box.append(w.wrap_with_overlay(scroller, self._compose_placeholder))
        return box

    def _refresh_compose(self) -> None:
        client = self._client()
        if client is None:
            return
        self._set_placeholder_loading(self._compose_placeholder, "Loading compose projects…")
        self._run_async(client.compose_ls, self._on_compose)

    def _on_compose(self, rows: Optional[List[dict]], err: Optional[Exception]) -> None:
        w.clear_listbox(self._compose_list)
        if err is not None:
            msg = w.error_text(err)
            if "compose" in str(err).lower() or "is not a docker command" in str(err).lower():
                msg = ("Docker Compose is not available on this host.\n"
                       "Install the Compose plugin to manage stacks here.")
            self._set_placeholder_idle(self._compose_placeholder, msg)
            return
        if not rows:
            self._set_placeholder_idle(self._compose_placeholder, "No compose projects")
            return
        self._hide_placeholder(self._compose_placeholder)
        for proj in rows:
            self._compose_list.append(self._compose_row(proj))

    def _compose_row(self, proj: dict) -> Gtk.Widget:
        name = w.field(proj, "Name", default="?")
        status = w.field(proj, "Status")
        config = w.field(proj, "ConfigFiles", "ConfigFile")

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row.set_margin_top(6)
        row.set_margin_bottom(6)
        row.set_margin_start(8)
        row.set_margin_end(8)

        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        t = Gtk.Label(label=name, xalign=0)
        t.add_css_class("heading")
        info.append(t)
        sub = Gtk.Label(label=" · ".join(p for p in (status, config) if p), xalign=0)
        sub.add_css_class("dim-label")
        sub.add_css_class("caption")
        sub.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        info.append(sub)
        row.append(info)

        # Up/redeploy is streamed (needs the config file); start/stop/restart are
        # captured; down is destructive (confirm) and streamed.
        first_config = (config or "").split(",")[0].strip()
        w.add_row_action(row, "view-refresh-symbolic", "Up / redeploy (compose up -d)",
                         lambda f=first_config: self._compose_up(name, f), refreshes=False)
        w.add_row_action(row, "media-playback-start-symbolic", "Start",
                         lambda: self._compose_action(name, "start"))
        w.add_row_action(row, "media-playback-stop-symbolic", "Stop",
                         lambda: self._compose_action(name, "stop"))
        w.add_row_action(row, "system-reboot-symbolic", "Restart",
                         lambda: self._compose_action(name, "restart"))
        w.add_row_action(row, "view-list-symbolic", "Services (compose ps)",
                         lambda: self._compose_services(name), refreshes=False)
        if first_config:
            w.add_row_action(row, "document-open-symbolic", "View compose file",
                             lambda f=first_config: self._compose_view_file(f),
                             refreshes=False)
        w.add_row_action(row, "user-trash-symbolic", "Down (stop & remove)",
                         lambda: self._compose_down(name), refreshes=False)
        return w.listbox_wrap(row)

    def _compose_services(self, project: str) -> None:
        client = self._client()
        if client is None:
            return

        def render(rows: List[dict]) -> str:
            lines = []
            for s in rows:
                svc = w.field(s, "Service", "Name", default="?")
                state = w.field(s, "State", "Status", default="")
                ports = w.field(s, "Publishers", "Ports", default="")
                lines.append(" · ".join(p for p in (svc, state, ports) if p))
            return "\n".join(lines) or "(no services)"

        def done(rows: Optional[List[dict]], err: Optional[Exception]) -> None:
            if err is not None:
                self._toast(f"compose ps {project} failed: {err}")
                return
            TextViewDialog(self._window(), f"{project} — services",
                           render(rows or [])).present()

        self._run_async(lambda: client.compose_ps(project), done)

    def _compose_action(self, project: str, action: str) -> None:
        client = self._client()
        if client is None:
            return
        self._run_async(
            lambda: client.compose(project, action),
            lambda res, err: self._on_action(f"{action} {project}", res, err,
                                             self._refresh_compose),
        )

    def _compose_up(self, project: str, config_file: str) -> None:
        client = self._client()
        nick = self._current_nickname()
        if client is None or not nick:
            return
        if not config_file:
            self._toast("No compose file path for this project")
            return
        ok = self.ctx.open_command_terminal(
            nick, client.compose_up_command(config_file), title=f"compose up: {project}")
        if not ok:
            self._toast("Could not start compose up")

    def _compose_down(self, project: str) -> None:
        client = self._client()
        nick = self._current_nickname()
        if client is None or not nick:
            return

        def do(_force: bool) -> None:
            ok = self.ctx.open_command_terminal(
                nick, client.compose_down_command(project), title=f"compose down: {project}")
            if not ok:
                self._toast("Could not start compose down")

        self._confirm(
            heading="Tear down stack?",
            body=f"This stops and removes all containers, networks for “{project}”.",
            destructive_label="Down",
            on_confirm=do,
        )

    def _compose_view_file(self, path: str) -> None:
        client = self._client()
        if client is None:
            return

        def done(text: Optional[str], err: Optional[Exception]) -> None:
            if err is not None:
                self._toast(f"Read {path} failed: {err}")
                return
            TextViewDialog(self._window(), path, text or "").present()

        self._run_async(lambda: client.read_file(path), done)

