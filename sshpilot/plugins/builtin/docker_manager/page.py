"""GTK UI for the Docker Console plugin.

A single page with a host picker and seven sections — Containers, Logs, Stats,
Images, Volumes, Networks, Compose — driven by :class:`DockerClient` over
``ctx.run_command``. Every Docker call runs on a worker thread and marshals back
with ``ctx.run_on_ui_thread``; streamed/interactive output (live logs, exec shell)
opens a terminal tab via ``ctx.open_command_terminal``.
"""

from __future__ import annotations

import logging
import math
import threading
from typing import Any, Callable, List, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib, Gtk, Adw, Gdk, GdkPixbuf  # noqa: E402

_MARK_RESOURCE = "/io/github/mfat/sshpilot/icons/scalable/actions/docker-mark-ocean-blue.svg"

from .client import DockerClient  # noqa: E402
from .dialogs import DockerConsoleSettingsDialog  # noqa: E402
from . import widgets as w  # noqa: E402
from .tab_compose import ComposeTabMixin  # noqa: E402
from .tab_containers import ContainersTabMixin  # noqa: E402
from .tab_images import ImagesTabMixin  # noqa: E402
from .tab_listings import ListingsTabMixin  # noqa: E402
from .tab_logs import LogsTabMixin  # noqa: E402
from .tab_stats import StatsTabMixin  # noqa: E402

logger = logging.getLogger(__name__)

_DEFAULT_REFRESH_SECONDS = 10
_MIN_REFRESH_SECONDS = 2


class DockerConsolePage(
    ContainersTabMixin,
    LogsTabMixin,
    StatsTabMixin,
    ImagesTabMixin,
    ListingsTabMixin,
    ComposeTabMixin,
    Gtk.Box,
):
    """The Docker Console page, composed from one mixin per tab.

    The tab mixins are NOT independent units — they all operate on the same
    ``self`` and rely on shared state/methods defined here (``_client``,
    ``_run_async``, ``_on_action``, ``_confirm``, ``_toast``, the placeholder
    helpers, ``_containers``/``_cached_images``/``_busy_cids``/``_sudo_denied``,
    …) and on each other (e.g. Containers' ``_follow_logs`` reads Logs' tail/
    timestamp widgets; Stats' live view calls Containers' ``_warn_sudo_interactive``).
    They are a file-level split for readability, not reusable in isolation.
    """

    def __init__(self, ctx: Any, initial_host: Optional[str] = None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.ctx = ctx
        self._connections: List[Any] = []
        self._containers: List[dict] = []
        self._cached_images: List[dict] = []  # last images() result (for create dialog)
        self._logs_raw: str = ""              # last snapshot, for search/errors-only
        self._refresh_source: Optional[int] = None
        # In-flight guards so auto-refresh can't pile up overlapping SSH
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
        # Nickname whose SSH ControlMaster we're currently keeping warm (if any).
        self._mux_nick: Optional[str] = None
        self._selected_nick: Optional[str] = None
        # Auto-refresh paused (session-only) and container ids with an in-flight
        # lifecycle op (double-fire guard).
        self._paused = False
        self._busy_cids: set[str] = set()
        # Set when the runtime probe found docker access denied even with sudo.
        self._sudo_denied = False
        # Container list filter text (search box).
        self._container_query = ""
        # Guards the runtime dropdown against feedback loops when synced in code.
        self._syncing_runtime = False
        # Pulsing Docker-mark loading indicators (stopped on unmap).
        self._pulse_widgets: List[Gtk.Image] = []
        # Open destructive confirm (AlertDialog/MessageDialog); dismissed on host
        # change so a prune/remove can't run against the newly selected host.
        self._active_confirm_dialog: Optional[Any] = None
        self._confirm_gen = 0

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
        self._stack.add_titled(self._build_volumes_section(), "volumes", "Volumes")
        self._stack.add_titled(self._build_networks_section(), "networks", "Networks")
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
        return self._selected_nick

    def _runtime_for(self, nickname: str) -> str:
        return self.ctx.settings.get(f"runtime:{nickname}", "docker") or "docker"

    def _runtime_mode(self, nickname: Optional[str]) -> str:
        """User's runtime override for this host: 'Auto' | 'docker' | 'podman'."""
        if not nickname:
            return "Auto"
        return self.ctx.settings.get(f"runtime_mode:{nickname}", "Auto") or "Auto"

    def _runtime_mode_index(self, nickname: Optional[str]) -> int:
        try:
            return self._RUNTIME_MODES.index(self._runtime_mode(nickname))
        except (ValueError, AttributeError):
            return 0

    def _on_runtime_mode_changed(self, *_a) -> None:
        if self._syncing_runtime:
            return
        nick = self._current_nickname()
        if not nick:
            return
        mode = self._RUNTIME_MODES[self._runtime_drop.get_selected()]
        self.ctx.settings.set(f"runtime_mode:{nick}", mode)
        if mode != "Auto":
            self.ctx.settings.set(f"runtime:{nick}", mode)
        self._on_host_changed()  # re-probe / reload under the new runtime

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

        self._connections = self._list_ssh_connections()
        target = self._initial_host or self.ctx.settings.get("last_host", None)
        if target and any(c.nickname == target for c in self._connections):
            self._selected_nick = target
        elif self._connections:
            self._selected_nick = self._connections[0].nickname

        self._host_btn = Gtk.Button()
        self._host_btn.add_css_class("flat")
        self._host_btn.set_tooltip_text("Choose Docker host")
        host_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        host_icon = Gtk.Image.new_from_icon_name("computer-symbolic")
        host_icon.set_pixel_size(16)
        host_box.append(host_icon)
        self._host_label = Gtk.Label()
        host_box.append(self._host_label)
        caret = Gtk.Image.new_from_icon_name("pan-down-symbolic")
        caret.set_pixel_size(12)
        host_box.append(caret)
        self._host_btn.set_child(host_box)
        self._host_btn.connect("clicked", self._show_host_picker)
        self._update_host_button_label()
        bar.append(self._host_btn)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        bar.append(spacer)

        runtime_label = Gtk.Label(label="Runtime")
        runtime_label.add_css_class("dim-label")
        runtime_label.add_css_class("caption")
        bar.append(runtime_label)

        # Auto-detect (default) or force docker/podman per host.
        self._RUNTIME_MODES = ("Auto", "docker", "podman")
        self._runtime_drop = Gtk.DropDown.new_from_strings(list(self._RUNTIME_MODES))
        self._runtime_drop.set_tooltip_text("Container runtime (Auto detects docker/podman)")
        self._runtime_drop.set_selected(self._runtime_mode_index(self._current_nickname()))
        self._runtime_drop.connect("notify::selected", self._on_runtime_mode_changed)
        bar.append(self._runtime_drop)

        self._sudo_check = Gtk.CheckButton(label="sudo")
        self._sudo_check.set_tooltip_text(
            "Run docker with sudo (for users not in the 'docker' group)"
        )
        nick0 = self._current_nickname()
        if nick0:
            self._sudo_check.set_active(self._use_sudo_for(nick0))
        self._sudo_check.connect("toggled", self._on_sudo_toggled)
        bar.append(self._sudo_check)

        self._pause_btn = Gtk.ToggleButton(icon_name="media-playback-pause-symbolic")
        self._pause_btn.set_tooltip_text("Pause auto-refresh")
        self._pause_btn.connect("toggled", self._on_pause_toggled)
        bar.append(self._pause_btn)

        settings = Gtk.Button(icon_name="settings-symbolic")
        settings.set_tooltip_text("Docker Console settings")
        settings.connect("clicked", lambda _b: self._open_settings())
        bar.append(settings)

        refresh = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh.set_tooltip_text("Refresh")
        refresh.connect("clicked", lambda _b: self._refresh_visible())
        bar.append(refresh)
        return bar

    def _on_pause_toggled(self, btn: Gtk.ToggleButton) -> None:
        self._paused = btn.get_active()
        btn.set_tooltip_text("Resume auto-refresh" if self._paused
                             else "Pause auto-refresh")

    # --- auto-refresh interval / timer ---------------------------------
    def _refresh_interval(self) -> int:
        try:
            val = int(self.ctx.settings.get("refresh_interval", _DEFAULT_REFRESH_SECONDS))
        except (TypeError, ValueError):
            val = _DEFAULT_REFRESH_SECONDS
        return max(_MIN_REFRESH_SECONDS, val)

    def _start_timer(self) -> None:
        if self._refresh_source is None:
            self._refresh_source = GLib.timeout_add_seconds(
                self._refresh_interval(), self._tick)

    def _restart_timer(self) -> None:
        if self._refresh_source is not None:
            GLib.source_remove(self._refresh_source)
            self._refresh_source = None
        if self.get_mapped():
            self._start_timer()

    def _on_refresh_interval_changed(self, _seconds: int) -> None:
        self.ctx.settings.set("refresh_interval", int(_seconds))
        self._restart_timer()

    def _update_host_button_label(self) -> None:
        nick = self._selected_nick
        if nick:
            self._host_label.set_label(nick)
            self._host_btn.set_sensitive(True)
        else:
            self._host_label.set_label("(no connections)")
            self._host_btn.set_sensitive(False)

    def _show_host_picker(self, _btn: Gtk.Button) -> None:
        from ....host_picker import show_host_picker  # noqa: PLC0415

        self._connections = self._list_ssh_connections()
        if not self._connections:
            self._toast("No SSH connections")
            return
        show_host_picker(
            self._window(),
            self._host_btn,
            self._on_host_picked,
            toast=self._toast,
            connections=self._connections,
        )

    def _on_host_picked(self, conn: Any) -> None:
        nick = getattr(conn, "nickname", None)
        if not nick or nick == self._selected_nick:
            return
        self._selected_nick = nick
        self._update_host_button_label()
        self._on_host_changed()

    def _on_sudo_toggled(self, check: Gtk.CheckButton) -> None:
        nick = self._current_nickname()
        if nick:
            self.ctx.settings.set(f"sudo:{nick}", bool(check.get_active()))
        self._refresh_visible()

    # --- SSH multiplexing (ControlMaster) ------------------------------
    def _multiplex_enabled(self) -> bool:
        # On by default — reuse one SSH connection for the chatty Docker polling.
        return bool(self.ctx.settings.get("controlmaster", True))

    def _acquire_multiplex(self, nick: Optional[str]) -> None:
        """Keep a master warm for ``nick`` (idempotent per held nick)."""
        if not nick or not self._multiplex_enabled():
            return
        if self._mux_nick == nick:
            return
        self._release_multiplex()  # drop any previously-held host first
        try:
            self.ctx.acquire_multiplex(nick)
            self._mux_nick = nick
        except Exception:  # noqa: BLE001 — older core without the API; just skip
            self._mux_nick = None

    def _release_multiplex(self) -> None:
        if not self._mux_nick:
            return
        try:
            self.ctx.release_multiplex(self._mux_nick)
        except Exception:  # noqa: BLE001
            pass
        self._mux_nick = None

    def _set_multiplex_enabled(self, enabled: bool) -> None:
        self.ctx.settings.set("controlmaster", bool(enabled))
        if enabled:
            self._acquire_multiplex(self._current_nickname())
        else:
            self._release_multiplex()
        self._refresh_visible()

    def _open_settings(self) -> None:
        DockerConsoleSettingsDialog(
            self._window(),
            reuse_ssh=self._multiplex_enabled(),
            on_reuse_ssh_changed=self._set_multiplex_enabled,
            refresh_interval=self._refresh_interval(),
            on_refresh_interval_changed=self._on_refresh_interval_changed,
        ).present()

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
        self._dismiss_active_confirm()
        self.ctx.settings.set("last_host", nick)
        self._sudo_check.set_active(self._use_sudo_for(nick))
        self._syncing_runtime = True
        self._runtime_drop.set_selected(self._runtime_mode_index(nick))
        self._syncing_runtime = False
        # Move the warm SSH master to the newly selected host.
        self._acquire_multiplex(nick)

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
            w.clear_listbox(lst)
            self._set_placeholder_loading(ph, text)
        w.clear_grid(self._stats_grid)
        self._stats_pulse.set_visible(True)
        self._pulse_start(self._stats_pulse)

        rc = self.ctx.run_command

        mode = self._runtime_mode(nick)

        # Detect docker vs podman, then probe access: if a plain `ps` is denied by
        # the daemon socket but `sudo -n ps` works, switch this host to sudo.
        # An explicit runtime override skips auto-detect.
        def probe():
            runtime = (mode if mode in ("docker", "podman")
                       else DockerClient(rc, nick).detect_runtime() or "docker")
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
            self._sudo_denied = False
            if result:
                runtime, use_sudo, status = result
                self.ctx.settings.set(f"runtime:{nick}", runtime)
                if use_sudo:
                    self.ctx.settings.set(f"sudo:{nick}", True)
                    self._sudo_check.set_active(True)
                elif status == "denied":
                    self._sudo_denied = True
                    self._toast("Docker access denied. Add your user to the "
                                "'docker' group, or enable passwordless sudo.")
            self._refresh_visible()

        self._run_async(probe, done)

    # ================================================================
    # auto-refresh lifecycle
    # ================================================================
    def _on_map(self, *_a) -> None:
        # Keep an SSH master warm for this host while the page is visible.
        self._acquire_multiplex(self._current_nickname())
        # Do the full host load (runtime/sudo probe → pulse → refresh) exactly
        # once, on first map — the single load both open paths rely on. Later
        # maps just refresh the visible view.
        if not self._initial_loaded:
            self._initial_loaded = True
            self._on_host_changed()
        else:
            self._refresh_visible()
        self._start_timer()

    def _on_unmap(self, *_a) -> None:
        self._dismiss_active_confirm()
        if self._refresh_source is not None:
            GLib.source_remove(self._refresh_source)
            self._refresh_source = None
        for img in self._pulse_widgets:
            self._pulse_stop(img)
        # Let the master expire once the page is no longer shown.
        self._release_multiplex()

    def _tick(self) -> bool:
        if self._paused:
            return True  # keep the timer; just skip this round
        # Only auto-refresh the live views; images is manual. Logs auto-refresh
        # only when its toggle is on.
        name = self._stack.get_visible_child_name()
        if name == "containers":
            self._refresh_containers()
        elif name == "stats":
            self._refresh_stats()
        elif name == "logs" and getattr(self, "_logs_autorefresh", None) \
                and self._logs_autorefresh.get_active():
            self._reload_logs()
        return True  # keep ticking

    def _refresh_visible(self) -> None:
        name = self._stack.get_visible_child_name()
        if name == "containers":
            self._refresh_containers()
        elif name == "stats":
            self._refresh_stats()
        elif name == "images":
            self._refresh_images()
        elif name == "volumes":
            self._refresh_volumes()
        elif name == "networks":
            self._refresh_networks()
        elif name == "logs":
            self._reload_logs()
        elif name == "compose":
            self._refresh_compose()


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

    def _dismiss_active_confirm(self) -> None:
        """Close any open destructive confirm and invalidate its response handler.

        Called when the host changes or the page is hidden so a pending prune /
        remove cannot run against a different host than the one shown when the
        dialog was opened."""
        self._confirm_gen += 1
        dlg = self._active_confirm_dialog
        self._active_confirm_dialog = None
        if dlg is None:
            return
        try:
            dlg.close()
        except Exception:
            logger.debug("dismiss confirm dialog failed", exc_info=True)

    def _confirm(self, *, heading: str, body: str, destructive_label: str,
                 on_confirm: Callable[[bool], None],
                 force_label: Optional[str] = None,
                 confirm_word: Optional[str] = None) -> None:
        """Destructive-action confirm. ``force_label`` adds a Force checkbox;
        ``confirm_word`` (mutually exclusive) requires the user to type the word
        before the destructive button enables — for high-blast-radius prunes."""
        self._dismiss_active_confirm()
        gen = self._confirm_gen
        dialog = w.build_alert(heading, body)
        self._active_confirm_dialog = dialog
        force_check: Optional[Gtk.CheckButton] = None
        if force_label:
            force_check = Gtk.CheckButton(label=force_label)
            dialog.set_extra_child(force_check)
        elif confirm_word:
            entry = Gtk.Entry(placeholder_text=f"Type {confirm_word} to confirm")
            dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("ok", destructive_label)
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        if confirm_word and not force_label:
            dialog.set_response_enabled("ok", False)
            entry.connect("changed", lambda e: dialog.set_response_enabled(
                "ok", e.get_text().strip() == confirm_word))

        def on_response(_d: Any, response: str) -> None:
            if gen != self._confirm_gen:
                return  # host changed or a newer confirm superseded this one
            self._active_confirm_dialog = None
            if response == "ok":
                on_confirm(bool(force_check.get_active()) if force_check else False)

        dialog.connect("response", on_response)
        w.present_alert(dialog, self._window())
