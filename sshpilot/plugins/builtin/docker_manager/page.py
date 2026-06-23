"""GTK UI for the Docker Manager plugin.

A single page with a host picker and five sections — Containers, Logs, Stats,
Images, Compose — driven by :class:`DockerClient` over ``ctx.run_command``. Every Docker
call runs on a worker thread and marshals back with ``ctx.run_on_ui_thread``;
streamed/interactive output (live logs, exec shell) opens a terminal tab via
``ctx.open_command_terminal``.
"""

from __future__ import annotations

import logging
import math
import threading
from typing import Any, Callable, List, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib, Gio, Gtk, Adw, Pango, Gdk, GdkPixbuf  # noqa: E402

_MARK_RESOURCE = "/io/github/mfat/sshpilot/icons/scalable/actions/docker-mark-ocean-blue.svg"

from .client import DockerClient, DockerError  # noqa: E402
from .dialogs import (  # noqa: E402
    ContainerDetailsDialog,
    CreateContainerDialog,
    TextViewDialog,
    prompt_text,
)

logger = logging.getLogger(__name__)

_REFRESH_SECONDS = 3


def _field(d: dict, *keys: str, default: str = "") -> str:
    """First present non-empty value among ``keys`` (Docker/Podman differ)."""
    for k in keys:
        v = d.get(k)
        if isinstance(v, (list, tuple)):
            v = ", ".join(str(x) for x in v)
        if v not in (None, ""):
            return str(v)
    return default


class DockerManagerPage(Gtk.Box):
    def __init__(self, ctx: Any, initial_host: Optional[str] = None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.ctx = ctx
        self._connections: List[Any] = []
        self._containers: List[dict] = []
        self._cached_images: List[dict] = []  # last images() result (for create dialog)
        self._logs_raw: str = ""              # last snapshot, for search/errors-only
        self._refresh_source: Optional[int] = None
        # In-flight guards so the 3s auto-refresh can't pile up overlapping SSH
        # calls when a host is slow.
        self._containers_busy = False
        self._stats_busy = False
        self._stats_has_data = False
        # Bumped on every host switch; async loads carry the gen they started
        # with and a stale (previous-host) result is dropped on arrival.
        self._load_gen = 0
        # Host chosen at construction (e.g. the right-clicked connection) so the
        # single map-time load targets it directly — no default-vs-target race.
        self._initial_host = initial_host
        self._initial_loaded = False
        # Pulsing Docker-mark loading indicators (stopped on unmap).
        self._pulse_widgets: List[Gtk.Image] = []

        self.set_margin_top(12)
        self.set_margin_bottom(12)
        self.set_margin_start(12)
        self.set_margin_end(12)

        self.append(self._build_host_bar())

        self._stack = Gtk.Stack()
        self._stack.set_vexpand(True)
        switcher = Gtk.StackSwitcher(stack=self._stack)
        switcher.set_halign(Gtk.Align.CENTER)
        self.append(switcher)
        self.append(self._stack)

        self._stack.add_titled(self._build_containers_section(), "containers", "Containers")
        self._stack.add_titled(self._build_logs_section(), "logs", "Logs")
        self._stack.add_titled(self._build_stats_section(), "stats", "Stats")
        self._stack.add_titled(self._build_images_section(), "images", "Images")
        self._stack.add_titled(self._build_compose_section(), "compose", "Compose")
        # Lazy-load the newly shown tab (e.g. Images doesn't load until viewed).
        self._stack.connect("notify::visible-child", self._on_tab_switched)

        self.connect("map", self._on_map)
        self.connect("unmap", self._on_unmap)

    def _on_tab_switched(self, *_a) -> None:
        if self.get_mapped():
            self._refresh_visible()

    # ================================================================
    # infrastructure
    # ================================================================
    def _run_async(self, fn: Callable[[], Any], on_done: Callable[[Any, Optional[Exception]], None]) -> None:
        """Run ``fn`` on a worker thread; deliver ``(result, error)`` on the UI thread."""
        def worker() -> None:
            try:
                result, err = fn(), None
            except Exception as exc:  # noqa: BLE001 - reported to the UI
                result, err = None, exc
            self.ctx.run_on_ui_thread(on_done, result, err)

        threading.Thread(target=worker, daemon=True).start()

    # --- pulsing Docker-mark loading indicator -------------------------
    def _make_docker_mark(self, size: int = 56) -> Gtk.Image:
        """The full-colour Docker mark used (pulsing) as the loading indicator.

        Loaded as a texture straight from the resource SVG rather than via the
        icon theme: the theme path renders nothing for this non-square,
        non-symbolic brand icon, whereas a texture rasterises reliably."""
        img = Gtk.Image()
        img.set_pixel_size(size)
        img.set_halign(Gtk.Align.CENTER)
        img.set_valign(Gtk.Align.CENTER)
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_resource_at_scale(
                _MARK_RESOURCE, size, size, True)
            img.set_from_paintable(Gdk.Texture.new_for_pixbuf(pixbuf))
        except Exception:
            img.set_from_icon_name("docker-mark-ocean-blue")  # fallback
        img._pulse_id = None      # type: ignore[attr-defined]
        img._pulse_phase = 0.0    # type: ignore[attr-defined]
        self._pulse_widgets.append(img)
        return img

    def _pulse_start(self, img: Gtk.Image) -> None:
        if getattr(img, "_pulse_id", None):
            return

        def _tick() -> bool:
            img._pulse_phase += 0.18  # type: ignore[attr-defined]
            img.set_opacity(0.3 + 0.7 * (0.5 + 0.5 * math.sin(img._pulse_phase)))
            return True

        img._pulse_phase = 0.0    # type: ignore[attr-defined]
        img.set_opacity(1.0)
        img._pulse_id = GLib.timeout_add(60, _tick)  # type: ignore[attr-defined]

    def _pulse_stop(self, img: Gtk.Image) -> None:
        pid = getattr(img, "_pulse_id", None)
        if pid:
            GLib.source_remove(pid)
            img._pulse_id = None  # type: ignore[attr-defined]
        img.set_opacity(1.0)

    # --- in-content loading placeholders --------------------------------
    def _make_loading_placeholder(self, text: str = "Loading…") -> Gtk.Widget:
        """A centered pulsing Docker mark + label, used as a Gtk.ListBox
        placeholder so it shows (only) while a list is empty — i.e. on first
        load. Built in the loading state so the logo is visible immediately."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_valign(Gtk.Align.CENTER)
        box.set_halign(Gtk.Align.CENTER)
        box.set_margin_top(24)
        box.set_margin_bottom(24)
        mark = self._make_docker_mark(56)
        label = Gtk.Label(label=text)
        label.add_css_class("dim-label")
        box.append(mark)
        box.append(label)
        box._pulse = mark    # type: ignore[attr-defined]
        box._label = label   # type: ignore[attr-defined]
        return box

    @staticmethod
    def _wrap_with_overlay(content: Gtk.Widget, placeholder: Gtk.Widget) -> Gtk.Overlay:
        """Overlay ``placeholder`` (centered) on ``content``. Used instead of
        Gtk.ListBox.set_placeholder, which did not reliably display the loading
        indicator. The placeholder's own visibility is toggled in code."""
        overlay = Gtk.Overlay()
        overlay.set_vexpand(True)
        overlay.set_child(content)
        placeholder.set_can_target(False)  # never intercept clicks meant for the list
        overlay.add_overlay(placeholder)
        return overlay

    def _set_placeholder_loading(self, ph: Gtk.Widget, text: str = "Loading…") -> None:
        ph.set_visible(True)
        ph._pulse.set_visible(True)   # type: ignore[attr-defined]
        self._pulse_start(ph._pulse)  # type: ignore[attr-defined]
        ph._label.set_text(text)      # type: ignore[attr-defined]

    def _set_placeholder_idle(self, ph: Gtk.Widget, text: str) -> None:
        ph.set_visible(True)
        self._pulse_stop(ph._pulse)     # type: ignore[attr-defined]
        ph._pulse.set_visible(False)    # type: ignore[attr-defined]
        ph._label.set_text(text)        # type: ignore[attr-defined]

    def _hide_placeholder(self, ph: Gtk.Widget) -> None:
        self._pulse_stop(ph._pulse)     # type: ignore[attr-defined]
        ph.set_visible(False)

    def _current_nickname(self) -> Optional[str]:
        idx = self._host_combo.get_selected()
        if 0 <= idx < len(self._connections):
            return self._connections[idx].nickname
        return None

    def _runtime_for(self, nickname: str) -> str:
        return self.ctx.settings.get(f"runtime:{nickname}", "docker") or "docker"

    def _use_sudo_for(self, nickname: str) -> bool:
        return bool(self.ctx.settings.get(f"sudo:{nickname}", False))

    def _client(self) -> Optional[DockerClient]:
        nick = self._current_nickname()
        if not nick:
            return None
        return DockerClient(self.ctx.run_command, nick, self._runtime_for(nick),
                            use_sudo=self._use_sudo_for(nick))

    def _toast(self, message: str) -> None:
        try:
            self.ctx.ui.notify(message)
        except Exception:
            logger.debug("notify failed: %s", message)

    def _window(self) -> Optional[Gtk.Window]:
        root = self.get_root()
        return root if isinstance(root, Gtk.Window) else None

    # ================================================================
    # host bar
    # ================================================================
    def _build_host_bar(self) -> Gtk.Widget:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.append(Gtk.Label(label="Host:"))

        self._connections = self._list_ssh_connections()
        names = [f"{c.nickname}" for c in self._connections] or ["(no connections)"]
        self._host_combo = Gtk.DropDown.new_from_strings(names)
        self._host_combo.set_hexpand(True)
        # Select the construction-time host (right-clicked connection), else the
        # last-used host. Done BEFORE connecting the signal so the initial load
        # happens once, at map time, rather than firing here.
        target = self._initial_host or self.ctx.settings.get("last_host", None)
        if target:
            for i, c in enumerate(self._connections):
                if c.nickname == target:
                    self._host_combo.set_selected(i)
                    break
        self._host_combo.connect("notify::selected", self._on_host_changed)
        bar.append(self._host_combo)

        self._sudo_check = Gtk.CheckButton(label="sudo")
        self._sudo_check.set_tooltip_text(
            "Run docker with sudo (for users not in the 'docker' group)"
        )
        nick0 = self._current_nickname()
        if nick0:
            self._sudo_check.set_active(self._use_sudo_for(nick0))
        self._sudo_check.connect("toggled", self._on_sudo_toggled)
        bar.append(self._sudo_check)

        refresh = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh.set_tooltip_text("Refresh")
        refresh.connect("clicked", lambda _b: self._refresh_visible())
        bar.append(refresh)
        return bar

    def _on_sudo_toggled(self, check: Gtk.CheckButton) -> None:
        nick = self._current_nickname()
        if nick:
            self.ctx.settings.set(f"sudo:{nick}", bool(check.get_active()))
        self._refresh_visible()

    def _list_ssh_connections(self) -> List[Any]:
        try:
            conns = self.ctx.list_connections()
        except Exception:
            conns = []
        return [c for c in conns if getattr(c, "protocol", "ssh") in ("ssh", "", None)]

    def _on_host_changed(self, *_a) -> None:
        nick = self._current_nickname()
        if not nick:
            return
        self.ctx.settings.set("last_host", nick)
        self._sudo_check.set_active(self._use_sudo_for(nick))

        # New host: invalidate any in-flight loads for the previous host (their
        # late results are dropped) and let the new host load immediately.
        self._load_gen += 1
        self._containers_busy = False
        self._stats_busy = False
        self._stats_has_data = False

        # Drop the previous host's rows and show a spinner immediately (clearing
        # makes each list's placeholder visible) for the whole probe + load.
        self._containers = []
        for lst, ph, text in (
            (self._containers_list, self._containers_placeholder, "Loading containers…"),
            (self._images_list, self._images_placeholder, "Loading images…"),
            (self._compose_list, self._compose_placeholder, "Loading compose projects…"),
        ):
            self._clear_listbox(lst)
            self._set_placeholder_loading(ph, text)
        self._clear_grid(self._stats_grid)
        self._stats_pulse.set_visible(True)
        self._pulse_start(self._stats_pulse)

        rc = self.ctx.run_command

        # Detect docker vs podman, then probe access: if a plain `ps` is denied by
        # the daemon socket but `sudo -n ps` works, switch this host to sudo.
        def probe():
            runtime = DockerClient(rc, nick).detect_runtime() or "docker"
            plain = DockerClient(rc, nick, runtime, use_sudo=False).ping()
            if getattr(plain, "exit_code", 0) == 0:
                return runtime, False, None
            text = (getattr(plain, "stderr", "") or "") + (getattr(plain, "stdout", "") or "")
            if DockerClient.is_permission_error(text):
                sudo = DockerClient(rc, nick, runtime, use_sudo=True).ping()
                if getattr(sudo, "exit_code", 1) == 0:
                    return runtime, True, None
                return runtime, False, "denied"  # sudo needs a password
            return runtime, False, None  # other error — let the refresh surface it

        def done(result, _err: Optional[Exception]) -> None:
            if result:
                runtime, use_sudo, status = result
                self.ctx.settings.set(f"runtime:{nick}", runtime)
                if use_sudo:
                    self.ctx.settings.set(f"sudo:{nick}", True)
                    self._sudo_check.set_active(True)
                elif status == "denied":
                    self._toast("Docker access denied. Add your user to the "
                                "'docker' group, or enable passwordless sudo.")
            self._refresh_visible()

        self._run_async(probe, done)

    # ================================================================
    # auto-refresh lifecycle
    # ================================================================
    def _on_map(self, *_a) -> None:
        # Do the full host load (runtime/sudo probe → pulse → refresh) exactly
        # once, on first map — the single load both open paths rely on. Later
        # maps just refresh the visible view.
        if not self._initial_loaded:
            self._initial_loaded = True
            self._on_host_changed()
        else:
            self._refresh_visible()
        if self._refresh_source is None:
            self._refresh_source = GLib.timeout_add_seconds(_REFRESH_SECONDS, self._tick)

    def _on_unmap(self, *_a) -> None:
        if self._refresh_source is not None:
            GLib.source_remove(self._refresh_source)
            self._refresh_source = None
        for img in self._pulse_widgets:
            self._pulse_stop(img)

    def _tick(self) -> bool:
        # Only auto-refresh the live views; logs/images are manual.
        name = self._stack.get_visible_child_name()
        if name == "containers":
            self._refresh_containers()
        elif name == "stats":
            self._refresh_stats()
        return True  # keep ticking

    def _refresh_visible(self) -> None:
        name = self._stack.get_visible_child_name()
        if name == "containers":
            self._refresh_containers()
        elif name == "stats":
            self._refresh_stats()
        elif name == "images":
            self._refresh_images()
        elif name == "logs":
            self._reload_logs()
        elif name == "compose":
            self._refresh_compose()

    # ================================================================
    # 1. Containers
    # ================================================================
    def _build_containers_section(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._show_all_check = Gtk.CheckButton(label="Show stopped")
        self._show_all_check.set_active(True)
        self._show_all_check.connect("toggled", lambda _c: self._refresh_containers())
        toolbar.append(self._show_all_check)
        create = Gtk.Button()
        create.set_child(Adw.ButtonContent(icon_name="list-add-symbolic",
                                           label="New Container"))
        create.add_css_class("suggested-action")
        create.set_halign(Gtk.Align.END)
        create.set_hexpand(True)
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
        box.append(self._wrap_with_overlay(scroller, self._containers_placeholder))
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
        self._clear_listbox(self._containers_list)
        if err is not None:
            self._containers = []
            self._set_placeholder_idle(self._containers_placeholder, self._error_text(err))
            self._refresh_logs_targets()
            return
        self._containers = rows or []
        if not self._containers:
            self._set_placeholder_idle(self._containers_placeholder, "No containers")
        else:
            self._hide_placeholder(self._containers_placeholder)
            for c in self._containers:
                self._containers_list.append(self._container_row(c))
        self._refresh_logs_targets()

    def _container_row(self, c: dict) -> Gtk.Widget:
        cid = _field(c, "ID", "Id", "ContainerID")
        name = _field(c, "Names", "Name", default=cid[:12])
        image = _field(c, "Image")
        status = _field(c, "Status", "State")
        ports = _field(c, "Ports")
        state = _field(c, "State", "Status").lower()
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

        def act(icon: str, tip: str, cb: Callable[[], None], *, sensitive: bool = True) -> None:
            btn = Gtk.Button(icon_name=icon)
            btn.set_tooltip_text(tip)
            btn.add_css_class("flat")
            btn.set_sensitive(sensitive)
            btn.connect("clicked", lambda _b: cb())
            row.append(btn)

        if running or paused:
            act("media-playback-stop-symbolic", "Stop", lambda: self._lifecycle("stop", cid, name))
            act("view-refresh-symbolic", "Restart", lambda: self._lifecycle("restart", cid, name))
            act("process-stop-symbolic", "Kill", lambda: self._lifecycle("kill", cid, name))
            act("utilities-terminal-symbolic", "Open shell", lambda: self._open_shell(cid, name))
            act("view-paged-symbolic", "Follow logs", lambda: self._follow_logs(cid, name))
        else:
            act("media-playback-start-symbolic", "Start", lambda: self._lifecycle("start", cid, name))
            act("view-paged-symbolic", "Logs", lambda: self._follow_logs(cid, name))
        act("dialog-information-symbolic", "Details", lambda: self._show_details(cid, name))
        act("user-trash-symbolic", "Remove", lambda: self._remove_container(cid, name))
        return self._listbox_wrap(row)

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
        images = [f"{_field(i, 'Repository')}:{_field(i, 'Tag')}"
                  for i in self._cached_images
                  if _field(i, "Repository") not in ("", "<none>")]

        def on_create(spec: dict) -> None:
            self._run_async(
                lambda: client.create_container(spec.pop("image"), **spec),
                lambda res, err: self._on_action(
                    "create container", res, err, self._refresh_containers),
            )

        CreateContainerDialog(self._window(), images, on_create).present()

    def _lifecycle(self, action: str, cid: str, name: str) -> None:
        client = self._client()
        if client is None:
            return
        self._run_async(
            lambda: client.lifecycle(action, cid),
            lambda res, err: self._on_action(f"{action} {name}", res, err, self._refresh_containers),
        )

    def _remove_container(self, cid: str, name: str) -> None:
        client = self._client()
        if client is None:
            return

        def do(force: bool) -> None:
            self._run_async(
                lambda: client.lifecycle("rm", cid, force=force),
                lambda res, err: self._on_action(f"remove {name}", res, err, self._refresh_containers),
            )

        self._confirm(
            heading="Remove container?",
            body=f"This will remove “{name}”.",
            destructive_label="Remove",
            on_confirm=do,
            force_label="Force (-f) — remove even if running",
        )

    def _open_shell(self, cid: str, name: str) -> None:
        client = self._client()
        nick = self._current_nickname()
        if client is None or not nick:
            return
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
        cmd = client.logs_follow_command(
            cid, tail=int(self._tail_spin.get_value()),
            timestamps=self._ts_switch.get_active(),
        )
        ok = self.ctx.open_command_terminal(nick, cmd, title=f"logs: {name}")
        if not ok:
            self._toast("Could not open logs")

    # ================================================================
    # 2. Logs (in-page snapshot + controls)
    # ================================================================
    def _build_logs_section(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        toolbar.append(Gtk.Label(label="Container:"))
        self._logs_combo = Gtk.DropDown.new_from_strings(["(refresh containers)"])
        self._logs_combo.set_hexpand(True)
        toolbar.append(self._logs_combo)

        toolbar.append(Gtk.Label(label="Tail:"))
        self._tail_spin = Gtk.SpinButton.new_with_range(10, 5000, 50)
        self._tail_spin.set_value(100)
        toolbar.append(self._tail_spin)

        self._ts_switch = Gtk.Switch()
        self._ts_switch.set_tooltip_text("Show timestamps (-t)")
        self._ts_switch.set_valign(Gtk.Align.CENTER)
        toolbar.append(Gtk.Label(label="Timestamps"))
        toolbar.append(self._ts_switch)
        box.append(toolbar)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        load = Gtk.Button(label="Load")
        load.connect("clicked", lambda _b: self._reload_logs())
        actions.append(load)
        follow = Gtk.Button(label="Follow in terminal")
        follow.connect("clicked", lambda _b: self._follow_logs_selected())
        actions.append(follow)
        copy = Gtk.Button(icon_name="edit-copy-symbolic")
        copy.set_tooltip_text("Copy logs")
        copy.connect("clicked", lambda _b: self._copy_logs())
        actions.append(copy)
        save = Gtk.Button(icon_name="document-save-symbolic")
        save.set_tooltip_text("Save logs…")
        save.connect("clicked", lambda _b: self._save_logs())
        actions.append(save)
        clear = Gtk.Button(icon_name="edit-clear-all-symbolic")
        clear.set_tooltip_text("Clear")
        clear.connect("clicked", lambda _b: self._clear_logs())
        actions.append(clear)
        box.append(actions)

        # Filter row: live search, errors-only, auto-scroll.
        filters = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._logs_search = Gtk.SearchEntry()
        self._logs_search.set_placeholder_text("Filter lines…")
        self._logs_search.set_hexpand(True)
        self._logs_search.connect("search-changed", lambda _e: self._apply_log_filter())
        filters.append(self._logs_search)
        self._logs_errors_only = Gtk.ToggleButton(label="Errors only")
        self._logs_errors_only.set_tooltip_text("Show only lines mentioning error/warn/fail/fatal")
        self._logs_errors_only.connect("toggled", lambda _b: self._apply_log_filter())
        filters.append(self._logs_errors_only)
        self._logs_autoscroll = Gtk.ToggleButton(label="Auto-scroll")
        self._logs_autoscroll.set_active(True)
        filters.append(self._logs_autoscroll)
        box.append(filters)

        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        self._logs_view = Gtk.TextView()
        self._logs_view.set_editable(False)
        self._logs_view.set_monospace(True)
        self._logs_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._logs_buffer = self._logs_view.get_buffer()
        scroller.set_child(self._logs_view)

        # A pulsing Docker mark overlaid on the (otherwise blank) log view while
        # a snapshot loads. TextView has no placeholder API, so use an overlay.
        overlay = Gtk.Overlay()
        overlay.set_vexpand(True)
        overlay.set_child(scroller)
        self._logs_pulse = self._make_docker_mark(56)
        self._logs_pulse.set_visible(False)
        overlay.add_overlay(self._logs_pulse)
        box.append(overlay)
        return box

    def _refresh_logs_targets(self) -> None:
        names = [_field(c, "Names", "Name", "ID", "Id") for c in self._containers]
        model = Gtk.StringList.new(names or ["(no containers)"])
        self._logs_combo.set_model(model)

    def _selected_container_id(self) -> Optional[str]:
        idx = self._logs_combo.get_selected()
        if 0 <= idx < len(self._containers):
            return _field(self._containers[idx], "ID", "Id", "ContainerID")
        return None

    def _selected_container_name(self) -> str:
        idx = self._logs_combo.get_selected()
        if 0 <= idx < len(self._containers):
            return _field(self._containers[idx], "Names", "Name", default="container")
        return "container"

    def _reload_logs(self) -> None:
        client = self._client()
        cid = self._selected_container_id()
        if client is None or not cid:
            return
        tail = int(self._tail_spin.get_value())
        ts = self._ts_switch.get_active()
        self._logs_pulse.set_visible(True)
        self._pulse_start(self._logs_pulse)
        self._run_async(
            lambda: client.logs_snapshot(cid, tail=tail, timestamps=ts),
            self._on_logs,
        )

    def _on_logs(self, text: Optional[str], err: Optional[Exception]) -> None:
        self._pulse_stop(self._logs_pulse)
        self._logs_pulse.set_visible(False)
        if err is not None:
            self._logs_raw = ""
            self._logs_buffer.set_text(f"Error: {err}", -1)
            return
        self._logs_raw = text or ""
        self._apply_log_filter()

    # Substrings that mark a "problem" line for the Errors-only filter.
    _ERROR_MARKERS = ("error", "err ", "warn", "fail", "fatal", "exception",
                      "panic", "critical")

    def _apply_log_filter(self) -> None:
        """Re-render the cached snapshot through the search + errors-only filters."""
        text = self._logs_raw
        if not text:
            self._logs_buffer.set_text("(no output)", -1)
            return
        needle = self._logs_search.get_text().strip().lower()
        errors_only = self._logs_errors_only.get_active()
        lines = text.splitlines()
        if needle:
            lines = [ln for ln in lines if needle in ln.lower()]
        if errors_only:
            lines = [ln for ln in lines
                     if any(m in ln.lower() for m in self._ERROR_MARKERS)]
        shown = "\n".join(lines)
        if not shown:
            shown = "(no matching lines)"
        self._logs_buffer.set_text(shown, -1)
        if self._logs_autoscroll.get_active():
            GLib.idle_add(self._scroll_logs_to_end)

    def _scroll_logs_to_end(self) -> bool:
        end = self._logs_buffer.get_end_iter()
        self._logs_view.scroll_to_iter(end, 0.0, False, 0.0, 0.0)
        return False

    def _logs_text(self) -> str:
        start, end = self._logs_buffer.get_bounds()
        return self._logs_buffer.get_text(start, end, False)

    def _clear_logs(self) -> None:
        self._logs_raw = ""
        self._logs_buffer.set_text("", 0)

    def _copy_logs(self) -> None:
        text = self._logs_text()
        if not text:
            self._toast("Nothing to copy")
            return
        display = self.get_display() or Gdk.Display.get_default()
        if display is not None:
            display.get_clipboard().set(text)
            self._toast("Logs copied")

    def _save_logs(self) -> None:
        text = self._logs_text()
        if not text:
            self._toast("Nothing to save")
            return
        dialog = Gtk.FileDialog.new()
        dialog.set_initial_name(f"{self._selected_container_name()}-logs.txt")

        def done(dlg: Gtk.FileDialog, result: Any) -> None:
            try:
                gfile = dlg.save_finish(result)
            except GLib.Error:
                return  # cancelled
            try:
                gfile.replace_contents(
                    text.encode("utf-8"), None, False,
                    Gio.FileCreateFlags.REPLACE_DESTINATION, None)
                self._toast("Logs saved")
            except Exception as exc:  # noqa: BLE001
                self._toast(f"Save failed: {exc}")

        dialog.save(self._window(), None, done)

    def _follow_logs_selected(self) -> None:
        cid = self._selected_container_id()
        if cid:
            self._follow_logs(cid, self._selected_container_name())

    # ================================================================
    # 3. Stats
    # ================================================================
    # Columns rendered in the stats grid: (header, stat-keys to read).
    _STATS_COLUMNS = (
        ("Name", ("Name", "Container")),
        ("CPU %", ("CPUPerc", "CPU")),
        ("Memory", ("MemUsage", "MemUsageLimit")),
        ("Mem %", ("MemPerc", "Mem")),
        ("Net I/O", ("NetIO",)),
        ("Block I/O", ("BlockIO",)),
    )

    def _build_stats_section(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        # A Grid keeps every column the same width across the header and all
        # rows, so the numbers line up (an HBox-per-row does not).
        self._stats_grid = Gtk.Grid(column_spacing=18, row_spacing=6)
        self._stats_grid.set_margin_top(6)
        self._stats_grid.set_margin_bottom(6)
        self._stats_grid.set_margin_start(8)
        self._stats_grid.set_margin_end(8)
        self._stats_grid.set_hexpand(True)
        scroller.set_child(self._stats_grid)

        # Pulsing Docker-mark overlay while loading (Grid has no placeholder API).
        overlay = Gtk.Overlay()
        overlay.set_vexpand(True)
        overlay.set_child(scroller)
        self._stats_pulse = self._make_docker_mark(56)
        self._stats_pulse.set_visible(False)
        overlay.add_overlay(self._stats_pulse)
        box.append(overlay)
        return box

    def _refresh_stats(self) -> None:
        client = self._client()
        if client is None or self._stats_busy:
            return
        self._stats_busy = True
        # Only show the loading logo on the first load; auto-refresh is silent.
        if not self._stats_has_data:
            self._stats_pulse.set_visible(True)
            self._pulse_start(self._stats_pulse)
        gen = self._load_gen
        self._run_async(client.stats, lambda r, e, g=gen: self._on_stats(r, e, g))

    def _on_stats(self, rows: Optional[List[dict]], err: Optional[Exception],
                  gen: int = 0) -> None:
        if gen != self._load_gen:
            return  # stale result for a previous host — drop it
        self._stats_busy = False
        self._pulse_stop(self._stats_pulse)
        self._stats_pulse.set_visible(False)
        self._clear_grid(self._stats_grid)
        span = len(self._STATS_COLUMNS)
        if err is not None:
            self._stats_has_data = False
            self._stats_grid.attach(self._grid_message(self._error_text(err), error=True), 0, 0, span, 1)
            return
        if not rows:
            self._stats_has_data = False
            self._stats_grid.attach(self._grid_message("No running containers"), 0, 0, span, 1)
            return
        self._stats_has_data = True
        # Header row.
        for col, (title, _keys) in enumerate(self._STATS_COLUMNS):
            head = Gtk.Label(label=title, xalign=0, hexpand=True)
            head.add_css_class("heading")
            self._stats_grid.attach(head, col, 0, 1, 1)
        # Data rows — same column index → same column width as the header.
        for r, s in enumerate(rows, start=1):
            for col, (_title, keys) in enumerate(self._STATS_COLUMNS):
                cell = Gtk.Label(label=_field(s, *keys) or "-", xalign=0, hexpand=True)
                self._stats_grid.attach(cell, col, r, 1, 1)

    # ================================================================
    # 4. Images & cleanup
    # ================================================================
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
        box.append(self._wrap_with_overlay(scroller, self._images_placeholder))
        return box

    def _refresh_images(self) -> None:
        client = self._client()
        if client is None:
            return
        self._set_placeholder_loading(self._images_placeholder, "Loading images…")
        self._run_async(client.images, self._on_images)

    def _on_images(self, rows: Optional[List[dict]], err: Optional[Exception]) -> None:
        self._clear_listbox(self._images_list)
        if err is not None:
            self._cached_images = []
            self._set_placeholder_idle(self._images_placeholder, self._error_text(err))
            return
        self._cached_images = rows or []
        if not rows:
            self._set_placeholder_idle(self._images_placeholder, "No images")
            return
        self._hide_placeholder(self._images_placeholder)
        for img in rows:
            iid = _field(img, "ID", "Id")
            repo = _field(img, "Repository", default="<none>")
            tag = _field(img, "Tag", default="<none>")
            size = _field(img, "Size")

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
            self._images_list.append(self._listbox_wrap(row))

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
                created = _field(layer, "CreatedSince", "CreatedAt", default="")
                size = _field(layer, "Size", default="")
                by = _field(layer, "CreatedBy", "Comment", default="").strip()
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
            body="Removes all volumes not used by at least one container.",
            destructive_label="Prune",
            on_confirm=do,
        )

    def _on_prune(self, res: Any, err: Optional[Exception]) -> None:
        if err is not None or (res is not None and getattr(res, "exit_code", 0) != 0):
            self._toast(f"Prune failed: {err or getattr(res, 'stderr', '')}")
            return
        out = (getattr(res, "stdout", "") or "").strip()
        # Surface the "Total reclaimed space" line if present.
        reclaimed = next((ln for ln in out.splitlines() if "reclaimed" in ln.lower()), "")
        self._toast(reclaimed or "Prune complete")
        self._refresh_images()

    # ================================================================
    # 5. Compose
    # ================================================================
    def _build_compose_section(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        self._compose_list = Gtk.ListBox()
        self._compose_list.add_css_class("boxed-list")
        self._compose_list.set_selection_mode(Gtk.SelectionMode.NONE)
        scroller.set_child(self._compose_list)
        self._compose_placeholder = self._make_loading_placeholder("Loading compose projects…")
        box.append(self._wrap_with_overlay(scroller, self._compose_placeholder))
        return box

    def _refresh_compose(self) -> None:
        client = self._client()
        if client is None:
            return
        self._set_placeholder_loading(self._compose_placeholder, "Loading compose projects…")
        self._run_async(client.compose_ls, self._on_compose)

    def _on_compose(self, rows: Optional[List[dict]], err: Optional[Exception]) -> None:
        self._clear_listbox(self._compose_list)
        if err is not None:
            msg = self._error_text(err)
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
        name = _field(proj, "Name", default="?")
        status = _field(proj, "Status")
        config = _field(proj, "ConfigFiles", "ConfigFile")

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

        def act(icon: str, tip: str, cb: Callable[[], None]) -> None:
            btn = Gtk.Button(icon_name=icon)
            btn.set_tooltip_text(tip)
            btn.add_css_class("flat")
            btn.connect("clicked", lambda _b: cb())
            row.append(btn)

        # Up/redeploy is streamed (needs the config file); start/stop/restart are
        # captured; down is destructive (confirm) and streamed.
        first_config = (config or "").split(",")[0].strip()
        act("view-refresh-symbolic", "Up / redeploy (compose up -d)",
            lambda f=first_config: self._compose_up(name, f))
        act("media-playback-start-symbolic", "Start",
            lambda: self._compose_action(name, "start"))
        act("media-playback-stop-symbolic", "Stop",
            lambda: self._compose_action(name, "stop"))
        act("system-reboot-symbolic", "Restart",
            lambda: self._compose_action(name, "restart"))
        if first_config:
            act("document-open-symbolic", "View compose file",
                lambda f=first_config: self._compose_view_file(f))
        act("user-trash-symbolic", "Down (stop & remove)",
            lambda: self._compose_down(name))
        return self._listbox_wrap(row)

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

    # ================================================================
    # shared helpers
    # ================================================================
    def _on_action(self, label: str, res: Any, err: Optional[Exception],
                   refresh: Callable[[], None]) -> None:
        if err is not None or (res is not None and getattr(res, "exit_code", 0) != 0):
            detail = err or (getattr(res, "stderr", "") or "").strip()
            self._toast(f"{label} failed: {detail}")
        else:
            self._toast(f"{label} done")
        refresh()

    def _confirm(self, *, heading: str, body: str, destructive_label: str,
                 on_confirm: Callable[[bool], None],
                 force_label: Optional[str] = None) -> None:
        dialog = Adw.MessageDialog(
            transient_for=self._window(), modal=True, heading=heading, body=body
        )
        force_check: Optional[Gtk.CheckButton] = None
        if force_label:
            force_check = Gtk.CheckButton(label=force_label)
            dialog.set_extra_child(force_check)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("ok", destructive_label)
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def on_response(_d: Any, response: str) -> None:
            if response == "ok":
                on_confirm(bool(force_check.get_active()) if force_check else False)

        dialog.connect("response", on_response)
        dialog.present()

    @staticmethod
    def _clear_listbox(listbox: Gtk.ListBox) -> None:
        child = listbox.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            listbox.remove(child)
            child = nxt

    @staticmethod
    def _clear_grid(grid: Gtk.Grid) -> None:
        child = grid.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            grid.remove(child)
            child = nxt

    @staticmethod
    def _listbox_wrap(widget: Gtk.Widget) -> Gtk.Widget:
        row = Gtk.ListBoxRow()
        row.set_activatable(False)
        row.set_child(widget)
        return row

    @staticmethod
    def _error_text(err: Exception) -> str:
        msg = str(err) if isinstance(err, DockerError) else f"Error: {err}"
        if DockerClient.is_permission_error(str(err)):
            msg += ("\n\nDocker needs elevated access. Enable the “sudo” toggle "
                    "above (requires passwordless sudo), or add your user to the "
                    "“docker” group on the host.")
        return msg

    @staticmethod
    def _grid_message(text: str, *, error: bool = False) -> Gtk.Widget:
        lbl = Gtk.Label(label=text, wrap=True, xalign=0)
        lbl.add_css_class("error" if error else "dim-label")
        lbl.set_margin_top(12)
        lbl.set_margin_bottom(12)
        return lbl
