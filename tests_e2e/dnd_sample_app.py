"""Minimal GTK application exercising the drag-and-drop helpers."""

from __future__ import annotations

import sys
from typing import Dict

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, GObject, Gtk

from sshpilot_dnd import RowBounds, hit_test_insertion


class DemoRow(Gtk.ListBoxRow):
    def __init__(self, title: str, controller: "DnDDemoApp") -> None:
        super().__init__()
        self.label_text = title
        self._controller = controller

        label = Gtk.Label(label=title)
        label.set_margin_start(12)
        label.set_margin_end(12)
        label.set_margin_top(6)
        label.set_margin_bottom(6)
        label.set_xalign(0.0)
        self.set_child(label)
        self.set_accessible_name(title)

        self._setup_drag_source()

    def _setup_drag_source(self) -> None:
        drag_source = Gtk.DragSource.new()
        drag_source.set_actions(Gdk.DragAction.MOVE)
        drag_source.connect("prepare", self._on_drag_prepare)
        drag_source.connect("drag-begin", self._on_drag_begin)
        self.add_controller(drag_source)

    def _on_drag_prepare(self, source: Gtk.DragSource, x: float, y: float) -> Gdk.ContentProvider:
        return Gdk.ContentProvider.new_for_value(self.label_text)

    def _on_drag_begin(self, source: Gtk.DragSource, drag: Gdk.Drag) -> None:
        self._controller.drag_label = self.label_text


class DnDDemoApp(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id="io.github.mfat.dnddemo")
        self.connect("activate", self._on_activate)
        self.labels = ["Row 1", "Row 2", "Row 3", "Row 4"]
        self.drag_label: str | None = None
        self.listbox: Gtk.ListBox | None = None

    def _on_activate(self, app: Adw.Application) -> None:
        window = Adw.ApplicationWindow(application=self)
        window.set_title("DnD Demo")

        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self.listbox = listbox

        drop_target = Gtk.DropTarget.new(type=str, actions=Gdk.DragAction.MOVE)
        drop_target.connect("drop", self._on_drop)
        drop_target.connect("motion", self._on_motion)
        listbox.add_controller(drop_target)

        self._rebuild_rows()

        window.set_content(listbox)
        window.present()

    def _rebuild_rows(self) -> None:
        if not self.listbox:
            return

        existing = [child for child in self.listbox]
        for child in existing:
            self.listbox.remove(child)

        for label in self.labels:
            self.listbox.append(DemoRow(label, controller=self))

    def _on_motion(self, target: Gtk.DropTarget, x: float, y: float) -> Gdk.DragAction:
        # No visual feedback required for the demo.
        return Gdk.DragAction.MOVE

    def _on_drop(self, target: Gtk.DropTarget, value: GObject.Value | str, x: float, y: float) -> bool:
        if not self.listbox:
            return False

        drag_label = self._coerce_label(value)
        if not drag_label or drag_label not in self.labels:
            return False

        row_bounds, row_lookup = self._collect_row_geometry(self.listbox)
        hit = hit_test_insertion(row_bounds, float(y))
        if not hit:
            return False

        drop_row = row_lookup.get(hit.key)
        if drop_row is None:
            return False

        target_label = getattr(drop_row, "label_text", None)
        if not target_label or target_label == drag_label:
            return False

        new_order = [label for label in self.labels if label != drag_label]

        try:
            anchor_index = new_order.index(target_label)
        except ValueError:
            return False

        if hit.position == "below":
            anchor_index += 1

        new_order.insert(anchor_index, drag_label)
        self.labels = new_order
        self._rebuild_rows()
        return True

    @staticmethod
    def _coerce_label(value: GObject.Value | str) -> str | None:
        if isinstance(value, str):
            return value

        text = None
        for accessor in ("get_string", "get_str", "get_value"):
            if hasattr(value, accessor):
                try:
                    candidate = getattr(value, accessor)()
                    if isinstance(candidate, str):
                        text = candidate
                        break
                except Exception:
                    continue
        if text is None:
            try:
                text = value.get()  # type: ignore[attr-defined]
            except Exception:
                return None
        return text if isinstance(text, str) else None

    @staticmethod
    def _collect_row_geometry(listbox: Gtk.ListBox) -> Tuple[list[RowBounds], Dict[str, Gtk.ListBoxRow]]:
        index = 0
        bounds: list[RowBounds] = []
        lookup: Dict[str, Gtk.ListBoxRow] = {}

        child = listbox.get_first_child()
        while child:
            if isinstance(child, Gtk.ListBoxRow):
                allocation = child.get_allocation()
                key = str(index)
                bounds.append(
                    RowBounds(
                        key=key,
                        top=float(allocation.y),
                        height=float(max(1, allocation.height)),
                    )
                )
                lookup[key] = child
                index += 1
            child = child.get_next_sibling()

        return bounds, lookup


def main() -> int:
    Adw.init()
    app = DnDDemoApp()
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
