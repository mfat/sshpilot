#!/usr/bin/env python3
"""Benchmark an efficient GTK window shaped like sshPilot's main window.

The child process builds only representative UI structure: a title/tab bar,
sidebar action header, search field, fully materialized ``Gtk.ListBox`` rows
shaped like sshPilot's connection rows, Start page, hidden banner slots, and
overlay layers.  It deliberately contains no sshPilot models, daemon client,
plugins, settings, or application services.

Each sample runs in a fresh interpreter.  The first sample is an untimed warmup
for GTK/font/icon caches; reported values are medians of the remaining runs.

Run on Windows with the configured development environment::

    .venv-win\\Scripts\\python.exe scripts\\gtk_window_baseline.py --runs 10
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LAUNCH_TIME_ENV = "SSHPILOT_GTK_BASELINE_LAUNCH_TIME"


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    gtk_bin = ROOT / "vendor" / "gtk" / "bin"
    if sys.platform == "win32" and gtk_bin.is_dir():
        env["PATH"] = f"{gtk_bin}{os.pathsep}{env.get('PATH', '')}"
    return env


def _after_next_paint(window, callback) -> None:
    """Invoke *callback* after the next frame reaches GTK's after-paint phase."""
    handler: list[int] = []

    def on_paint(frame_clock) -> None:
        frame_clock.disconnect(handler.pop())
        callback()

    def hook_frame_clock(*_args) -> None:
        if handler:
            window.disconnect(handler.pop())
        clock = window.get_frame_clock()
        if clock is None:
            callback()
            return
        handler.append(clock.connect("after-paint", on_paint))
        window.queue_draw()

    if window.get_frame_clock() is None and not window.get_realized():
        handler.append(window.connect("realize", hook_frame_clock))
    else:
        hook_frame_clock()


def _run_child(rows: int) -> int:
    wall_ready = time.time()
    launch_time = float(os.environ.get(LAUNCH_TIME_ENV, wall_ready))
    offset_ms = max(0.0, (wall_ready - launch_time) * 1000.0)
    started = time.perf_counter()
    marks: dict[str, float] = {"python_ready": offset_ms}

    def mark(name: str) -> None:
        marks[name] = offset_ms + (time.perf_counter() - started) * 1000.0

    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

    mark("gtk_imported")

    class BaselineApplication(Adw.Application):
        def __init__(self) -> None:
            super().__init__(
                application_id="io.github.mfat.sshpilot.GtkWindowBaseline",
                flags=Gio.ApplicationFlags.NON_UNIQUE,
            )
            self.connect("activate", self.on_activate)
            self.window = None
            self.connection_list = None

        @staticmethod
        def icon_button(icon_name: str, tooltip: str) -> Gtk.Button:
            button = Gtk.Button.new_from_icon_name(icon_name)
            button.add_css_class("flat")
            button.set_tooltip_text(tooltip)
            button.set_can_focus(False)
            return button

        @staticmethod
        def row_action_slot(button) -> Gtk.Stack:
            slot = Gtk.Stack()
            slot.set_hhomogeneous(False)
            slot.set_vhomogeneous(True)
            slot.set_transition_type(Gtk.StackTransitionType.NONE)
            slot.set_valign(Gtk.Align.CENTER)
            slot.add_named(Gtk.Box(), "none")
            slot.add_named(button, "button")
            slot.set_visible_child_name("button")
            return slot

        def build_connection_row(self, index: int) -> Gtk.ListBoxRow:
            """Construct the same kind of fully materialized row sshPilot uses."""
            row = Gtk.ListBoxRow()
            row.add_css_class("card")
            row.set_selectable(True)
            row.set_tooltip_text(
                f"bench-{index:03d}\nuser{index % 7}@192.0.2.{index % 250 + 1}:22"
            )

            main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            for _position in range(2):
                indicator = Gtk.Box()
                indicator.set_size_request(-1, 4)
                indicator.set_visible(False)
                main.append(indicator)

            content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            content.set_margin_start(10)
            content.set_margin_end(8)
            content.set_margin_top(4)
            content.set_margin_bottom(4)

            color_dot = Gtk.Image.new_from_icon_name("media-record-symbolic")
            color_dot.set_visible(False)
            content.append(color_dot)
            color_badge = Gtk.Image.new_from_icon_name("tag-symbolic")
            color_badge.set_visible(False)
            content.append(color_badge)

            connection_icon = Gtk.Image.new_from_icon_name("computer-symbolic")
            connection_icon.set_valign(Gtk.Align.CENTER)
            content.append(connection_icon)

            info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            info.set_hexpand(True)
            info.set_valign(Gtk.Align.CENTER)
            title = Gtk.Label(label=f"bench-{index:03d}", xalign=0)
            title.set_hexpand(True)
            title.set_ellipsize(Pango.EllipsizeMode.END)
            title.set_width_chars(8)
            title.set_max_width_chars(25)
            info.append(title)
            host = Gtk.Label(
                label=f"user{index % 7}@192.0.2.{index % 250 + 1}",
                xalign=0,
            )
            host.add_css_class("dim-label")
            host.set_hexpand(True)
            host.set_ellipsize(Pango.EllipsizeMode.END)
            host.set_width_chars(8)
            host.set_max_width_chars(25)
            host.set_visible(False)
            info.append(host)
            content.append(info)

            badges = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            for icon_name in ("network-transmit-receive-symbolic", "changes-allow-symbolic"):
                badge = Gtk.Image.new_from_icon_name(icon_name)
                badge.set_visible(False)
                badges.append(badge)
            content.append(badges)

            hover_actions = Gtk.Box(
                orientation=Gtk.Orientation.HORIZONTAL,
                spacing=3,
            )
            for icon_name, tooltip in (
                ("folder-open-symbolic", "Manage files"),
                ("dialog-information-symbolic", "Dashboard"),
            ):
                button = self.icon_button(icon_name, tooltip)
                button.set_valign(Gtk.Align.CENTER)
                button.set_opacity(0.0)
                hover_actions.append(self.row_action_slot(button))
            content.append(hover_actions)

            status_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            status_icon = Gtk.Image.new_from_icon_name("changes-prevent-symbolic")
            status_icon.set_pixel_size(16)
            status_box.append(status_icon)
            status_box.set_visible(False)
            content.append(status_box)
            main.insert_child_after(content, main.get_first_child())
            row.set_child(main)

            motion = Gtk.EventControllerMotion()
            motion.connect("enter", lambda *_args: None)
            motion.connect("leave", lambda *_args: None)
            row.add_controller(motion)
            drag_source = Gtk.DragSource()
            drag_source.set_actions(Gdk.DragAction.MOVE)
            row.add_controller(drag_source)
            return row

        def build_connection_list(self):
            connection_list = Gtk.ListBox()
            connection_list.add_css_class("navigation-sidebar")
            connection_list.set_hexpand(True)
            connection_list.set_selection_mode(Gtk.SelectionMode.MULTIPLE)
            connection_list.set_focusable(True)
            scroller = Gtk.ScrolledWindow()
            scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            scroller.set_min_content_width(0)
            scroller.set_child(connection_list)
            scroller.set_vexpand(True)
            scroller.set_hexpand(True)
            self.connection_list = connection_list
            return scroller

        def build_sidebar(self):
            sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            sidebar.set_size_request(300, -1)

            actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            actions.set_margin_start(8)
            actions.set_margin_end(8)
            actions.set_margin_top(8)
            actions.set_margin_bottom(6)
            for icon, tooltip in (
                ("list-add-symbolic", "New connection"),
                ("folder-new-symbolic", "New group"),
                ("system-search-symbolic", "Search"),
                ("view-sort-ascending-symbolic", "Sort"),
                ("view-more-symbolic", "More"),
            ):
                actions.append(self.icon_button(icon, tooltip))
            sidebar.append(actions)

            search = Gtk.SearchEntry()
            search.set_placeholder_text("Search connections")
            search.set_margin_start(8)
            search.set_margin_end(8)
            search.set_margin_bottom(6)
            search.set_visible(False)
            sidebar.append(search)
            sidebar.append(self.build_connection_list())

            footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            footer.set_margin_start(8)
            footer.set_margin_end(8)
            footer.set_margin_top(6)
            footer.set_margin_bottom(8)
            footer.append(self.icon_button("utilities-terminal-symbolic", "Local terminal"))
            footer.append(self.icon_button("preferences-system-symbolic", "Settings"))
            sidebar.append(footer)
            return sidebar

        def build_start_page(self):
            page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            page.set_halign(Gtk.Align.CENTER)
            page.set_valign(Gtk.Align.CENTER)
            page.set_hexpand(True)
            page.set_vexpand(True)

            mark_image = Gtk.Image.new_from_icon_name("utilities-terminal-symbolic")
            mark_image.set_pixel_size(72)
            page.append(mark_image)

            search = Gtk.SearchEntry()
            search.set_placeholder_text("Search connections and commands")
            search.set_size_request(520, -1)
            page.append(search)

            recent = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            for index in range(4):
                row = Gtk.Button(label=f"Recent connection {index + 1}")
                row.add_css_class("flat")
                recent.append(row)
            page.append(recent)

            footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            footer.set_halign(Gtk.Align.CENTER)
            for label in ("Documentation", "Keyboard shortcuts", "About"):
                button = Gtk.Button(label=label)
                button.add_css_class("flat")
                footer.append(button)
            page.append(footer)
            return page

        def build_window(self):
            window = Adw.ApplicationWindow(application=self)
            window.set_title("GTK sshPilot baseline")
            window.set_default_size(1100, 720)

            outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

            header = Gtk.WindowHandle()
            header_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            header_row.set_size_request(-1, 46)
            start_controls = Gtk.WindowControls.new(Gtk.PackType.START)
            end_controls = Gtk.WindowControls.new(Gtk.PackType.END)
            header_row.append(start_controls)
            header_row.append(self.icon_button("sidebar-show-symbolic", "Toggle sidebar"))
            header_row.append(self.icon_button("tab-new-symbolic", "New tab"))

            tab_view = Adw.TabView()
            tab_bar = Adw.TabBar.new()
            tab_bar.set_view(tab_view)
            tab_bar.set_hexpand(True)
            header_row.append(tab_bar)
            header_row.append(self.icon_button("system-search-symbolic", "Global search"))
            header_row.append(self.icon_button("view-more-symbolic", "Main menu"))
            header_row.append(end_controls)
            header.set_child(header_row)
            outer.append(header)

            # Optimal hidden-feature representation: stable zero-height slots,
            # with actual banner/search widgets created only when requested.
            outer.append(Gtk.Box())
            outer.append(Gtk.Box())

            paned = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)
            paned.set_start_child(self.build_sidebar())
            paned.set_position(300)
            paned.set_resize_start_child(False)
            paned.set_shrink_start_child(False)

            start_page = self.build_start_page()
            tab_view.append_pinned(start_page)
            paned.set_end_child(tab_view)
            paned.set_vexpand(True)

            content_overlay = Gtk.Overlay()
            content_overlay.set_child(paned)
            global_overlay = Gtk.Overlay()
            global_overlay.set_child(content_overlay)
            outer.append(global_overlay)
            window.set_content(outer)
            return window

        def on_activate(self, _app) -> None:
            mark("app_activated")
            self.window = self.build_window()
            mark("widget_tree_built")
            self.window.present()
            mark("window_presented")
            _after_next_paint(self.window, self.on_first_paint)

        def on_first_paint(self) -> None:
            mark("window_first_painted")
            first_count = min(rows, 25)
            for index in range(first_count):
                self.connection_list.append(self.build_connection_row(index))
            mark("first_rows_built")
            _after_next_paint(self.window, self.on_first_rows_painted)

        def on_first_rows_painted(self) -> None:
            mark("first_rows_painted")
            GLib.idle_add(self.build_remaining_rows)

        def build_remaining_rows(self) -> bool:
            staged = [
                self.build_connection_row(index)
                for index in range(min(rows, 25), rows)
            ]
            for row in staged:
                self.connection_list.append(row)
            mark("all_rows_built")
            _after_next_paint(self.window, self.on_rows_painted)
            return GLib.SOURCE_REMOVE

        def on_rows_painted(self) -> None:
            mark("rows_painted")
            GLib.idle_add(self.quit)

    app = BaselineApplication()
    mark("app_initialized")
    app.run([])
    print(json.dumps({"marks": marks, "rows": rows}, separators=(",", ":")))
    return 0


def _sample(rows: int, timeout: float) -> dict:
    env = _child_env()
    env[LAUNCH_TIME_ENV] = repr(time.time())
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--child", "--rows", str(rows)],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"GTK baseline child failed with exit {proc.returncode}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _phases(marks: dict[str, float]) -> dict[str, float]:
    return {
        "launch_to_python": marks["python_ready"],
        "gtk_import": marks["gtk_imported"] - marks["python_ready"],
        "application_init": marks["app_activated"] - marks["gtk_imported"],
        "widget_tree_build": marks["widget_tree_built"] - marks["app_activated"],
        "build_to_present": marks["window_presented"] - marks["widget_tree_built"],
        "present_to_first_paint": (
            marks["window_first_painted"] - marks["window_presented"]
        ),
        "activation_to_first_paint": (
            marks["window_first_painted"] - marks["app_activated"]
        ),
        "launch_to_first_paint": marks["window_first_painted"],
        "first_rows_build": marks["first_rows_built"] - marks["window_first_painted"],
        "first_rows_to_paint": marks["first_rows_painted"] - marks["first_rows_built"],
        "remaining_rows_build": marks["all_rows_built"] - marks["first_rows_painted"],
        "remaining_rows_to_paint": marks["rows_painted"] - marks["all_rows_built"],
        "all_rows_after_first_paint": (
            marks["rows_painted"] - marks["window_first_painted"]
        ),
        "launch_to_rows_painted": marks["rows_painted"],
    }


def _report(samples: list[dict]) -> dict:
    phase_runs = [_phases(sample["marks"]) for sample in samples]
    summary: dict[str, dict[str, float]] = {}
    print(f"\n=== efficient GTK window: median of {len(samples)} fresh processes ===")
    for name in phase_runs[0]:
        values = [run[name] for run in phase_runs]
        median = statistics.median(values)
        summary[name] = {
            "median_ms": round(median, 1),
            "min_ms": round(min(values), 1),
            "max_ms": round(max(values), 1),
        }
        print(
            f"{median:8.1f} ms  {min(values):7.1f}-{max(values):<7.1f}  "
            f"{name.replace('_', ' ')}"
        )
    return {"summary": summary, "runs": samples}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--rows", type=int, default=60)
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--json", help="write raw samples and summary")
    args = parser.parse_args(argv)

    if args.child:
        return _run_child(args.rows)
    if args.runs < 1:
        parser.error("--runs must be at least 1")

    # Discard one cache-warming process, matching sshPilot's launch profiler.
    _sample(args.rows, args.timeout)
    samples = [_sample(args.rows, args.timeout) for _ in range(args.runs)]
    report = _report(samples)
    report["rows"] = args.rows
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
