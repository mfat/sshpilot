"""Docker Manager tab: Images."""

from __future__ import annotations

from typing import Any, List, Optional

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from .dialogs import TextViewDialog, prompt_text  # noqa: E402
from . import widgets as w  # noqa: E402


class ImagesTabMixin:
    def _build_images_section(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        pull = Gtk.Button(label="Pull image")
        pull.set_tooltip_text("docker pull — progress shown in a terminal tab")
        pull.add_css_class("suggested-action")
        pull.connect("clicked", lambda _b: self._pull_image())
        toolbar.append(pull)
        iprune = Gtk.Button(label="Prune images")
        iprune.set_tooltip_text("docker image prune -f (remove dangling images)")
        iprune.add_css_class("destructive-action")
        iprune.connect("clicked", lambda _b: self._image_prune())
        toolbar.append(iprune)
        prune = Gtk.Button(label="System prune")
        prune.set_tooltip_text("docker system prune -f (dangling images, stopped containers, unused networks)")
        prune.add_css_class("destructive-action")
        prune.connect("clicked", lambda _b: self._system_prune())
        toolbar.append(prune)
        vprune = Gtk.Button(label="Prune volumes")
        vprune.add_css_class("destructive-action")
        vprune.connect("clicked", lambda _b: self._volume_prune())
        toolbar.append(vprune)
        box.append(toolbar)

        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        self._images_list = Gtk.ListBox()
        self._images_list.add_css_class("boxed-list")
        self._images_list.set_selection_mode(Gtk.SelectionMode.NONE)
        scroller.set_child(self._images_list)
        self._images_placeholder = self._make_loading_placeholder("Loading images…")
        box.append(w.wrap_with_overlay(scroller, self._images_placeholder))
        return box

    def _refresh_images(self) -> None:
        client = self._client()
        if client is None:
            return
        self._set_placeholder_loading(self._images_placeholder, "Loading images…")
        self._run_async(client.images, self._on_images)

    def _on_images(self, rows: Optional[List[dict]], err: Optional[Exception]) -> None:
        w.clear_listbox(self._images_list)
        if err is not None:
            self._cached_images = []
            self._set_placeholder_idle(self._images_placeholder, w.error_text(err))
            return
        self._cached_images = rows or []
        if not rows:
            self._set_placeholder_idle(self._images_placeholder, "No images")
            return
        self._hide_placeholder(self._images_placeholder)
        for img in rows:
            iid = w.field(img, "ID", "Id")
            repo = w.field(img, "Repository", default="<none>")
            tag = w.field(img, "Tag", default="<none>")
            size = w.field(img, "Size")

            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            row.set_margin_top(6)
            row.set_margin_bottom(6)
            row.set_margin_start(8)
            row.set_margin_end(8)
            info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
            t = Gtk.Label(label=f"{repo}:{tag}", xalign=0)
            t.add_css_class("heading")
            info.append(t)
            sub = Gtk.Label(label=" · ".join(p for p in (iid[:12], size) if p), xalign=0)
            sub.add_css_class("dim-label")
            sub.add_css_class("caption")
            info.append(sub)
            row.append(info)

            ref = f"{repo}:{tag}" if repo != "<none>" else iid
            hist = Gtk.Button(icon_name="document-open-recent-symbolic")
            hist.set_tooltip_text("Image history (layers)")
            hist.add_css_class("flat")
            hist.connect("clicked", lambda _b, r=ref: self._image_history(r))
            row.append(hist)

            rm = Gtk.Button(icon_name="user-trash-symbolic")
            rm.set_tooltip_text("Remove image")
            rm.add_css_class("flat")
            rm.connect("clicked", lambda _b, i=iid, r=f"{repo}:{tag}": self._remove_image(i, r))
            row.append(rm)
            self._images_list.append(w.listbox_wrap(row))

    def _pull_image(self) -> None:
        client = self._client()
        nick = self._current_nickname()
        if client is None or not nick:
            self._toast("Select a host first")
            return

        def on_ok(ref: str) -> None:
            ok = self.ctx.open_command_terminal(
                nick, client.pull_command(ref), title=f"pull: {ref}")
            if not ok:
                self._toast("Could not start pull")

        prompt_text(self._window(), "Pull image",
                    "Image reference to pull (e.g. nginx:latest).",
                    "nginx:latest", "Pull", on_ok)

    def _image_prune(self) -> None:
        client = self._client()
        if client is None:
            return

        def do(_force: bool) -> None:
            self._run_async(client.image_prune,
                            lambda res, err: self._on_prune(res, err))

        self._confirm(
            heading="Prune unused images?",
            body="Removes all dangling images (untagged layers not used by a container).",
            destructive_label="Prune",
            on_confirm=do,
        )

    def _image_history(self, ref: str) -> None:
        client = self._client()
        if client is None:
            return

        def render(rows: List[dict]) -> str:
            lines = []
            for layer in rows:
                created = w.field(layer, "CreatedSince", "CreatedAt", default="")
                size = w.field(layer, "Size", default="")
                by = w.field(layer, "CreatedBy", "Comment", default="").strip()
                lines.append(f"{created}  {size}\n  {by}")
            return "\n\n".join(lines) or "(no layers)"

        def done(rows: Optional[List[dict]], err: Optional[Exception]) -> None:
            if err is not None:
                self._toast(f"History {ref} failed: {err}")
                return
            TextViewDialog(self._window(), f"{ref} — history", render(rows or [])).present()

        self._run_async(lambda: client.image_history(ref), done)

    def _remove_image(self, image_id: str, label: str) -> None:
        client = self._client()
        if client is None:
            return

        def do(force: bool) -> None:
            self._run_async(
                lambda: client.remove_image(image_id, force=force),
                lambda res, err: self._on_action(f"remove {label}", res, err, self._refresh_images),
            )

        self._confirm(
            heading="Remove image?",
            body=f"This will remove “{label}”.",
            destructive_label="Remove",
            on_confirm=do,
            force_label="Force (-f)",
        )

    def _system_prune(self) -> None:
        client = self._client()
        if client is None:
            return

        def do(_force: bool) -> None:
            self._run_async(
                client.system_prune,
                lambda res, err: self._on_prune(res, err),
            )

        self._confirm(
            heading="Run system prune?",
            body="Removes all dangling images, stopped containers, and unused networks.",
            destructive_label="Prune",
            on_confirm=do,
        )

    def _volume_prune(self) -> None:
        client = self._client()
        if client is None:
            return

        def do(_force: bool) -> None:
            self._run_async(
                client.volume_prune,
                lambda res, err: self._on_prune(res, err),
            )

        self._confirm(
            heading="Prune unused volumes?",
            body=("Removes ALL volumes not used by a container — this can delete "
                  "orphaned database data permanently. Type PRUNE to confirm."),
            destructive_label="Prune",
            on_confirm=do,
            confirm_word="PRUNE",
        )

    def _on_prune(self, res: Any, err: Optional[Exception]) -> None:
        if err is not None or (res is not None and getattr(res, "exit_code", 0) != 0):
            self._toast(f"Prune failed: {err or getattr(res, 'stderr', '')}")
            return
        out = (getattr(res, "stdout", "") or "").strip()
        # Show the full output (what was deleted + reclaimed space) in a dialog,
        # not just the one reclaimed line — the detail matters for a destructive op.
        if out:
            TextViewDialog(self._window(), "Prune result", out).present()
        else:
            self._toast("Prune complete")
        self._refresh_images()

