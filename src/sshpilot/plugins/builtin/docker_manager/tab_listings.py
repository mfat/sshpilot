"""Docker Console tab: Listings."""

from __future__ import annotations

import json as _json
from typing import Callable, List, Optional

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from .dialogs import TextViewDialog  # noqa: E402
from . import widgets as w  # noqa: E402

from gettext import gettext as _  # noqa: E402


class ListingsTabMixin:
    def _build_listing_section(self, list_attr: str, ph_attr: str,
                               loading: str) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        listbox = Gtk.ListBox()
        listbox.add_css_class("boxed-list")
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        scroller.set_child(listbox)
        ph = self._make_loading_placeholder(loading)
        setattr(self, list_attr, listbox)
        setattr(self, ph_attr, ph)
        box.append(w.wrap_with_overlay(scroller, ph))
        return box

    def _populate_listing(self, listbox: Gtk.ListBox, ph: Gtk.Widget,
                          rows: Optional[List[dict]], err: Optional[Exception],
                          empty: str, build_row: Callable[[dict], Gtk.Widget]) -> None:
        w.clear_listbox(listbox)
        if err is not None:
            self._set_placeholder_idle(ph, w.error_text(err), error=True)
            return
        if not rows:
            self._set_placeholder_idle(ph, empty)
            return
        self._hide_placeholder(ph)
        for r in rows:
            listbox.append(build_row(r))

    def _inspect_dialog(self, fetch: Callable[[], dict], title: str) -> None:
        def done(data: Optional[dict], err: Optional[Exception]) -> None:
            if err is not None:
                self._toast(f"{title} failed: {err}")
                return
            text = _json.dumps(data or {}, indent=2, sort_keys=True)
            TextViewDialog(self._window(), title, text).present()

        self._run_async(fetch, done)

    # -- Volumes --------------------------------------------------------
    def _build_volumes_section(self) -> Gtk.Widget:
        return self._build_listing_section(
            "_volumes_list", "_volumes_placeholder", "Loading volumes…")

    def _refresh_volumes(self) -> None:
        client = self._client()
        if client is None:
            return
        self._set_placeholder_loading(self._volumes_placeholder, "Loading volumes…")
        self._run_async(client.volumes, self._on_volumes)

    def _on_volumes(self, rows: Optional[List[dict]], err: Optional[Exception]) -> None:
        self._populate_listing(self._volumes_list, self._volumes_placeholder,
                               rows, err, "No volumes", self._volume_row)

    def _volume_row(self, v: dict) -> Gtk.Widget:
        name = w.field(v, "Name")
        driver = w.field(v, "Driver")
        row = w.named_row(name, driver)
        w.add_row_action(row, "dialog-information-symbolic", "Inspect",
                         lambda n=name: self._inspect_dialog(
                             lambda: self._client().volume_inspect(n), f"volume: {n}"),
                         refreshes=False)
        w.add_row_action(row, "user-trash-symbolic", "Remove",
                         lambda n=name: self._remove_volume(n))
        return w.listbox_wrap(row)

    def _remove_volume(self, name: str) -> None:
        client = self._client()
        if client is None:
            return

        def do(force: bool) -> None:
            self._run_async(
                lambda: client.remove_volume(name, force=force),
                lambda res, err: self._on_action(f"remove {name}", res, err, self._refresh_volumes))

        self._confirm(heading=_("Remove volume?"), body=_("This will remove “{name}”.").format(name=name),
                      destructive_label="Remove", on_confirm=do,
                      force_label="Force (-f)")

    # -- Networks -------------------------------------------------------
    def _build_networks_section(self) -> Gtk.Widget:
        return self._build_listing_section(
            "_networks_list", "_networks_placeholder", "Loading networks…")

    def _refresh_networks(self) -> None:
        client = self._client()
        if client is None:
            return
        self._set_placeholder_loading(self._networks_placeholder, "Loading networks…")
        self._run_async(client.networks, self._on_networks)

    def _on_networks(self, rows: Optional[List[dict]], err: Optional[Exception]) -> None:
        self._populate_listing(self._networks_list, self._networks_placeholder,
                               rows, err, "No networks", self._network_row)

    # Built-in networks docker won't let you remove — don't offer a dead button.
    _UNREMOVABLE_NETWORKS = {"bridge", "host", "none"}

    def _network_row(self, n: dict) -> Gtk.Widget:
        name = w.field(n, "Name")
        sub = " · ".join(p for p in (w.field(n, "Driver"), w.field(n, "Scope")) if p)
        row = w.named_row(name, sub)
        w.add_row_action(row, "dialog-information-symbolic", "Inspect",
                         lambda nm=name: self._inspect_dialog(
                             lambda: self._client().network_inspect(nm), f"network: {nm}"),
                         refreshes=False)
        if name not in self._UNREMOVABLE_NETWORKS:
            w.add_row_action(row, "user-trash-symbolic", "Remove",
                             lambda nm=name: self._remove_network(nm))
        return w.listbox_wrap(row)

    def _remove_network(self, name: str) -> None:
        client = self._client()
        if client is None:
            return

        def do(_force: bool) -> None:
            self._run_async(
                lambda: client.remove_network(name),
                lambda res, err: self._on_action(f"remove {name}", res, err, self._refresh_networks))

        self._confirm(heading=_("Remove network?"), body=_("This will remove “{name}”.").format(name=name),
                      destructive_label="Remove", on_confirm=do)

