"""Advanced Configuration: the SSH configuration as OpenSSH reads it.

Left: the configuration as ordered blocks (the global options, then every
``Host`` and ``Match`` block), editable and reorderable. Right: what OpenSSH
resolves for the host filtered by, which line supplies each value, the order the
blocks are read in, the text itself, and the files ``Include`` lines read.

Nothing here decides what applies. Every answer comes from the daemon, which
asks OpenSSH (``ssh -G``) about the draft in the context a connection would
read the saved file in: see :mod:`sshpilot.core.ssh_config_draft`. The window
never touches the file: it loads and saves text through the daemon, like the
SSH Config Editor, which stays available beside it.
"""
from __future__ import annotations

import logging
import threading
from gettext import gettext as _
from typing import Callable, Dict, List, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

from .api.errors import ErrorCode, SshPilotError  # noqa: E402
from .api.models.connections import (  # noqa: E402
    EvaluateSshConfigDraftRequest,
    SaveSshConfigTextRequest,
    SshConfigDraftEvaluation,
    SshConfigDraftLineState as State,
)
from .connection_dialog_auth import canonical_keyword  # noqa: E402
from .gtk import advanced_config_messages as messages  # noqa: E402
from .ssh_config_blocks import (  # noqa: E402
    GLOBAL,
    HOST,
    MATCH,
    Block,
    ConfigDraft,
    LineMap,
    Option,
    parse,
    serialize,
)

logger = logging.getLogger(__name__)

#: Options offered by "Common option", with a starting value.
COMMON_OPTIONS = (
    ("HostName", ""),
    ("User", ""),
    ("Port", "22"),
    ("IdentityFile", "~/.ssh/id_ed25519"),
    ("ProxyJump", ""),
    ("ForwardAgent", "yes"),
    ("IdentitiesOnly", "yes"),
    ("ServerAliveInterval", "60"),
)

_EVALUATE_DELAY_MS = 300
_HISTORY_DELAY_MS = 700
_HISTORY_LIMIT = 100

_CSS = """
.advcfg-pill { font-size: 0.82em; font-weight: 600; padding: 2px 9px; border-radius: 999px; }
.advcfg-pill.success { background-color: alpha(@success_color, 0.16); color: @success_color; }
.advcfg-pill.warning { background-color: alpha(@warning_color, 0.18); color: @warning_color; }
.advcfg-pill.accent { background-color: alpha(@accent_color, 0.15); color: @accent_color; }
.advcfg-pill.purple { background-color: alpha(@purple_3, 0.18); color: @purple_3; }
.advcfg-pill.muted { background-color: alpha(currentColor, 0.08); color: alpha(currentColor, 0.65); }
.advcfg-block { padding: 0; }
.advcfg-header { padding: 7px 10px; background-color: alpha(currentColor, 0.07);
  border-radius: 12px 12px 0 0; border-bottom: 1px solid alpha(currentColor, 0.10); }
.advcfg-header.movable:hover { background-color: alpha(currentColor, 0.11); }
.advcfg-body { padding: 8px 12px 10px 12px; }
.advcfg-options { margin-left: 18px; padding-left: 12px;
  border-left: 2px solid alpha(currentColor, 0.14); }
.advcfg-options:dir(rtl) { margin-left: 0; padding-left: 0; margin-right: 18px;
  padding-right: 12px; border-left: none; border-right: 2px solid alpha(currentColor, 0.14); }
.advcfg-block.disabled { opacity: 0.6; }
.advcfg-drop-before { box-shadow: 0 -3px 0 0 @accent_color; }
.advcfg-drop-after { box-shadow: 0 3px 0 0 @accent_color; }
.advcfg-unused entry { opacity: 0.6; }
.advcfg-flash { outline: 2px solid @accent_color; outline-offset: 2px; }
"""
_css_installed = False


def _ensure_css() -> None:
    global _css_installed
    display = Gdk.Display.get_default()
    if _css_installed or display is None:
        return
    provider = Gtk.CssProvider()
    provider.load_from_data(_CSS.encode("utf-8"))
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    _css_installed = True


def _pill(text: str = "", style: str = "muted") -> Gtk.Label:
    label = Gtk.Label(label=text)
    label.set_valign(Gtk.Align.CENTER)
    label.add_css_class("advcfg-pill")
    label.add_css_class(style)
    label._style = style
    return label


def _set_pill(label: Gtk.Label, text: str, style: str, tooltip: str = "") -> None:
    label.set_label(text)
    if label._style != style:
        label.remove_css_class(label._style)
        label.add_css_class(style)
        label._style = style
    label.set_tooltip_text(tooltip or None)
    label.set_visible(bool(text))


def split_target(text: str) -> tuple:
    """``(user, host)`` of ``[user@]host``; the user is None when absent."""
    text = text.strip()
    user, separator, host = text.rpartition("@")
    return (user or None, host) if separator else (None, text)


def can_place(blocks: List[Block]) -> bool:
    """Whether *blocks* is an order that means what it shows.

    OpenSSH has no line that ends a block: options written after a ``Host``
    line belong to that host. The global options therefore have to come
    before every block that is switched on.
    """
    seen_active = False
    for block in blocks:
        if block.kind == GLOBAL:
            if seen_active:
                return False
        elif not block.disabled:
            seen_active = True
    return True


class AdvancedConfigWindow(Adw.Window):
    """The Advanced Configuration window.

    *client* is the daemon client. *hosts* supplies saved host names for the
    target shortcuts; *on_saved* runs after each save.
    """

    def __init__(
        self,
        parent: Optional[Gtk.Window],
        client,
        *,
        hosts: Optional[Callable[[], List[str]]] = None,
        on_saved: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__()
        _ensure_css()
        self._client = client
        self._hosts = hosts or (lambda: [])
        self._on_saved = on_saved
        if parent is not None:
            self.set_transient_for(parent)
        self.set_default_size(1180, 760)
        self.set_title(_("Advanced Configuration"))

        self.draft = ConfigDraft()
        self._loaded_text = ""
        self._revision = ""
        self._display_name = ""
        self._writable = True
        self._text = ""
        self._lines = LineMap({}, {})
        self._evaluation: Optional[SshConfigDraftEvaluation] = None
        self._evaluated_serial = 0
        self._serial = 0
        self._evaluate_source = 0
        self._history_source = 0
        self._history: List[ConfigDraft] = []
        self._history_index = -1
        self._allowed_exec: set = set()
        self._block_widgets: Dict[int, dict] = {}
        self._option_widgets: Dict[int, dict] = {}
        self._notes_open: set = set()
        self._rendering = False
        self._closing = False
        self._request_serial = 0
        self._save_after_evaluation = False
        # Blocks added or edited under the current filter: they stay on
        # screen even when they do not apply to the host being filtered by.
        self._kept_visible: set = set()
        self._evaluated_lines = self._lines
        self._banner_kind = ""

        self._build()
        self.connect("close-request", self._on_close_request)
        self._load()

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------

    def _build(self) -> None:
        self._toasts = Adw.ToastOverlay()
        self.set_content(self._toasts)
        view = Adw.ToolbarView()
        self._toasts.set_child(view)

        header = Adw.HeaderBar()
        self._title = Adw.WindowTitle(title=_("Advanced Configuration"), subtitle="")
        header.set_title_widget(self._title)
        self.undo_button = Gtk.Button(icon_name="edit-undo-symbolic", tooltip_text=_("Undo"))
        self.undo_button.connect("clicked", lambda *_a: self.undo())
        self.redo_button = Gtk.Button(icon_name="edit-redo-symbolic", tooltip_text=_("Redo"))
        self.redo_button.connect("clicked", lambda *_a: self.redo())
        header.pack_start(self.undo_button)
        header.pack_start(self.redo_button)
        self.save_button = Gtk.Button(label=_("Save"))
        self.save_button.add_css_class("suggested-action")
        self.save_button.connect("clicked", lambda *_a: self.save())
        header.pack_end(self.save_button)
        menu = Gio.Menu()
        menu.append(_("Reload from Disk"), "advcfg.reload")
        menu.append(_("Copy Configuration"), "advcfg.copy")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu))
        actions = Gio.SimpleActionGroup()
        for name, callback in (("reload", self._on_reload), ("copy", self._on_copy)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            actions.add_action(action)
        self.insert_action_group("advcfg", actions)
        view.add_top_bar(header)

        self.banner = Adw.Banner()
        self.banner.connect("button-clicked", self._on_banner_button)
        self._banner_action: Optional[Callable[[], None]] = None
        view.add_top_bar(self.banner)
        view.add_top_bar(self._build_target_bar())

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, wide_handle=True)
        paned.set_shrink_start_child(False)
        paned.set_shrink_end_child(False)
        paned.set_position(620)
        paned.set_start_child(self._build_blocks_pane())
        paned.set_end_child(self._build_results_pane())
        view.set_content(paned)

    def _build_target_bar(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        for side in ("top", "bottom"):
            getattr(box, f"set_margin_{side}")(8)
        box.set_margin_start(16)
        box.set_margin_end(16)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        # One filter, no separate "target" field: a host typed here (or
        # picked from the drop-down) narrows the rules to the blocks OpenSSH
        # applies to it and shows what it resolves. Empty: every block.
        self.target_entry = Gtk.SearchEntry()
        self.target_entry.set_placeholder_text(
            _("Filter by host: type a name to see what applies to it")
        )
        self.target_entry.set_hexpand(True)
        self.target_entry.connect("search-changed", self._on_target_changed)
        self._host_list = Gtk.ListBox()
        self._host_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self._host_list.add_css_class("navigation-sidebar")
        self._host_list.connect("row-activated", self._on_host_chosen)
        host_scroller = Gtk.ScrolledWindow()
        host_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        host_scroller.set_propagate_natural_height(True)
        host_scroller.set_max_content_height(360)
        host_scroller.set_min_content_width(260)
        host_scroller.set_child(self._host_list)
        self._host_popover = Gtk.Popover()
        self._host_popover.set_child(host_scroller)
        self.target_button = Gtk.MenuButton(icon_name="pan-down-symbolic")
        self.target_button.set_popover(self._host_popover)
        self.target_button.set_tooltip_text(_("Choose a host"))
        target_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        target_box.add_css_class("linked")
        target_box.append(self.target_entry)
        target_box.append(self.target_button)
        row.append(target_box)
        box.append(row)
        self.context_label = Gtk.Label(xalign=0)
        self.context_label.add_css_class("dim-label")
        self.context_label.add_css_class("caption")
        self.context_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.context_label.set_visible(False)
        box.append(self.context_label)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.append(box)
        outer.append(Gtk.Separator())
        return outer

    def _build_blocks_pane(self) -> Gtk.Widget:
        pane = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        pane.set_size_request(420, -1)
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        for side in ("top", "bottom"):
            getattr(header, f"set_margin_{side}")(8)
        header.set_margin_start(16)
        header.set_margin_end(16)
        title = Gtk.Label(label=_("Configuration rules"))
        title.add_css_class("heading")
        header.append(title)
        self.block_count = _pill()
        header.append(self.block_count)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        header.append(spacer)
        self.add_global_button = Gtk.Button(label=_("Global"))
        self.add_global_button.set_tooltip_text(
            _("Add options above every Host and Match line")
        )
        self.add_global_button.connect("clicked", lambda *_a: self.add_block(GLOBAL))
        header.append(self.add_global_button)
        # Host and Match are OpenSSH's keywords: not translated.
        for label, kind in (("Host", HOST), ("Match", MATCH)):
            button = Gtk.Button(label=label)
            button.set_tooltip_text(_("Add a block at the end"))
            button.connect("clicked", lambda *_a, k=kind: self.add_block(k))
            header.append(button)
        pane.append(header)
        pane.append(Gtk.Separator())
        self._blocks_scroll = Gtk.ScrolledWindow()
        self._blocks_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._blocks_scroll.set_vexpand(True)
        self._blocks_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(self._blocks_box, f"set_margin_{side}")(14)
        self._blocks_scroll.set_child(self._blocks_box)
        pane.append(self._blocks_scroll)
        return pane

    def _build_results_pane(self) -> Gtk.Widget:
        pane = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        pane.set_size_request(380, -1)
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        for side in ("top", "bottom"):
            getattr(header, f"set_margin_{side}")(6)
        header.set_margin_start(12)
        header.set_margin_end(16)
        self.stack = Adw.ViewStack()
        self.stack.set_vexpand(True)
        switcher = Adw.ViewSwitcher(stack=self.stack)
        switcher.set_policy(Adw.ViewSwitcherPolicy.WIDE)
        switcher.set_hexpand(True)
        header.append(switcher)
        self.matched_count = _pill(style="success")
        header.append(self.matched_count)
        pane.append(header)
        pane.append(Gtk.Separator())
        pane.append(self.stack)
        self.stack.add_titled_with_icon(
            self._build_effective_page(), "effective", _("Effective"),
            "emblem-ok-symbolic",
        )
        self.stack.add_titled_with_icon(
            self._build_list_page("_trace_list", _(
                "The order OpenSSH reads the configuration in for this target. "
                "The first value it obtains for an option is the one it keeps."
            )), "trace", _("Order"), "view-list-symbolic",
        )
        self.stack.add_titled_with_icon(
            self._build_raw_page(), "raw", _("Text"), "text-x-generic-symbolic",
        )
        self.stack.add_titled_with_icon(
            self._build_list_page("_include_list", _(
                "Files OpenSSH reads for this target besides this one. They are "
                "read where their Include line stands, and are not edited here."
            )), "includes", _("Includes"), "folder-documents-symbolic",
        )
        self.stack.connect("notify::visible-child-name", lambda *_a: self._sync_raw())
        return pane

    @staticmethod
    def _page_box() -> tuple:
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(14)
        scroller.set_child(box)
        return scroller, box

    def _build_effective_page(self) -> Gtk.Widget:
        scroller, box = self._page_box()
        self.summary = Adw.PreferencesGroup(title=_("Connection"))
        self._summary_rows = {}
        for key, title in (
            ("command", _("Command")),
            ("host", _("Host name and port")),
            ("user", _("User")),
            ("proxy", _("Proxy")),
            ("keys", _("Identity files")),
        ):
            row = Adw.ActionRow(title=title)
            row.set_subtitle_selectable(True)
            row.add_css_class("property")
            self.summary.add(row)
            self._summary_rows[key] = row
        box.append(self.summary)
        filters = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.search = Gtk.SearchEntry(placeholder_text=_("Filter options or values"))
        self.search.set_hexpand(True)
        self.search.connect("search-changed", lambda *_a: self._render_effective())
        filters.append(self.search)
        self.only_configured = Gtk.ToggleButton(label=_("Configured"))
        self.only_configured.set_tooltip_text(_("Hide options left at OpenSSH's default"))
        self.only_shadowed = Gtk.ToggleButton(label=_("Shadowed"))
        self.only_shadowed.set_tooltip_text(_("Only options with lines that are not used"))
        for toggle in (self.only_configured, self.only_shadowed):
            toggle.connect("toggled", lambda *_a: self._render_effective())
            filters.append(toggle)
        box.append(filters)
        self._effective_list = Gtk.ListBox()
        self._effective_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self._effective_list.add_css_class("boxed-list")
        box.append(self._effective_list)
        self._effective_note = Gtk.Label(xalign=0, wrap=True)
        self._effective_note.add_css_class("dim-label")
        box.append(self._effective_note)
        return scroller

    def _build_list_page(self, attribute: str, hint: str) -> Gtk.Widget:
        scroller, box = self._page_box()
        label = Gtk.Label(label=hint, xalign=0, wrap=True)
        label.add_css_class("dim-label")
        box.append(label)
        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        listbox.add_css_class("boxed-list")
        box.append(listbox)
        setattr(self, attribute, listbox)
        return scroller

    def _build_raw_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(14)
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hint = Gtk.Label(
            label=_("The configuration as it will be saved. Edit it and apply to "
                    "read it into the rules again."),
            xalign=0, wrap=True,
        )
        hint.add_css_class("dim-label")
        hint.set_hexpand(True)
        top.append(hint)
        self.apply_raw_button = Gtk.Button(label=_("Apply"))
        self.apply_raw_button.set_valign(Gtk.Align.CENTER)
        self.apply_raw_button.set_sensitive(False)
        self.apply_raw_button.connect("clicked", lambda *_a: self.apply_raw())
        top.append(self.apply_raw_button)
        box.append(top)
        frame = Gtk.Frame()
        frame.set_vexpand(True)
        scroller = Gtk.ScrolledWindow()
        self.raw_view = Gtk.TextView(monospace=True)
        for side in ("top", "bottom", "left", "right"):
            getattr(self.raw_view, f"set_{side}_margin")(10)
        self._raw_syncing = False
        self.raw_view.get_buffer().connect("changed", self._on_raw_changed)
        scroller.set_child(self.raw_view)
        frame.set_child(scroller)
        box.append(frame)
        return box

    # ------------------------------------------------------------------
    # loading and saving (through the daemon)
    # ------------------------------------------------------------------

    def _run(self, work: Callable[[], object], done: Callable[[object, Optional[BaseException]], None]) -> None:
        def worker():
            try:
                result, error = work(), None
            except BaseException as exc:  # noqa: BLE001 - reported to the UI
                result, error = None, exc
            GLib.idle_add(lambda: (None if self._closing else done(result, error), False)[1])

        threading.Thread(target=worker, daemon=True, name="sshpilot-advanced-config").start()

    def _load(self, *, keep_target: bool = False) -> None:
        self.save_button.set_sensitive(False)
        self._run(self._client.get_ssh_config_text, lambda r, e: self._loaded(r, e, keep_target))

    def _loaded(self, result, error, keep_target: bool) -> None:
        if error is not None:
            logger.debug("Advanced Configuration could not load", exc_info=error)
            self._show_banner(_("The SSH configuration could not be read."))
            return
        self._revision = result.revision
        self._display_name = result.display_name
        self._writable = bool(result.writable)
        self._loaded_text = result.text
        self._title.set_subtitle(result.display_name)
        self.draft = parse(result.text)
        self._history = []
        self._history_index = -1
        self._allowed_exec = set()
        self._notes_open = set()
        self._push_history()
        self._render_all()
        if not self._writable:
            self._show_banner(_("This SSH configuration is read-only: changes cannot be saved."))

    @property
    def raw_pending(self) -> bool:
        """The Text page holds edits that were not applied to the rules."""
        return self.apply_raw_button.get_sensitive()

    @property
    def dirty(self) -> bool:
        return self._text != self._loaded_text or self.raw_pending

    def save(self) -> None:
        if not self.dirty or not self._writable:
            return
        if self.raw_pending:
            # What is on screen is what gets saved: the text being typed
            # is read into the rules first.
            self.apply_raw()
            if not self.dirty:
                self._update_chrome()
                return
            if self._evaluation is not None:
                # OpenSSH has not seen this text yet: save once it has, so
                # a line it rejects is still asked about.
                self._save_after_evaluation = True
                return
        evaluation = self._evaluation
        if evaluation is not None and evaluation.errors and self._evaluated_serial == self._serial:
            alert = Adw.AlertDialog(
                heading=_("Save a configuration OpenSSH rejects?"),
                body=messages.error_text(evaluation.errors[0]) + "\n\n" + _(
                    "OpenSSH refuses the whole configuration over one such line, "
                    "so no SSH connection starts until it is corrected."
                ),
            )
            alert.add_response("cancel", _("Cancel"))
            alert.add_response("save", _("Save Anyway"))
            alert.set_response_appearance("save", Adw.ResponseAppearance.DESTRUCTIVE)
            alert.set_default_response("cancel")
            alert.set_close_response("cancel")
            alert.connect("response", lambda _d, r: r == "save" and self._save_now())
            alert.present(self)
            return
        self._save_now()

    def _save_now(self) -> None:
        text = self._text
        self.save_button.set_sensitive(False)
        request = SaveSshConfigTextRequest(text=text, expected_revision=self._revision)
        self._run(
            lambda: self._client.save_ssh_config_text(request),
            lambda result, error: self._saved(text, result, error),
        )

    def _saved(self, text: str, result, error) -> None:
        if error is not None:
            code = error.code if isinstance(error, SshPilotError) else None
            if code is ErrorCode.STALE_EDITOR:
                self._show_banner(
                    _("The SSH configuration changed on disk since it was opened."),
                    _("Reload"), lambda: self._load(keep_target=True),
                )
            elif code is ErrorCode.SSH_CONFIG_READ_ONLY:
                self._show_banner(_("This SSH configuration is read-only: changes cannot be saved."))
            else:
                logger.debug("Advanced Configuration could not save", exc_info=error)
                self._show_banner(_("The SSH configuration could not be saved."))
            self._update_chrome()
            return
        self._revision = result.revision
        self._loaded_text = text
        self._toasts.add_toast(Adw.Toast.new(_("SSH configuration saved")))
        self._update_chrome()
        self._schedule_evaluation(now=True)   # Match exec commands are saved now
        if callable(self._on_saved):
            try:
                self._on_saved()
            except Exception:
                logger.debug("on_saved failed", exc_info=True)

    def _on_reload(self, *_args) -> None:
        def reload():
            self.apply_raw_button.set_sensitive(False)   # typed text goes too
            self._load(keep_target=True)

        if not self.dirty:
            reload()
            return
        self._confirm_discard(reload)

    def _on_copy(self, *_args) -> None:
        self.get_clipboard().set(self._text)
        self._toasts.add_toast(Adw.Toast.new(_("Configuration copied")))

    def _confirm_discard(self, proceed: Callable[[], None]) -> None:
        alert = Adw.AlertDialog(
            heading=_("Discard unsaved changes?"),
            body=_("The changes made here have not been saved to the SSH configuration."),
        )
        alert.add_response("cancel", _("Cancel"))
        alert.add_response("discard", _("Discard"))
        alert.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
        alert.set_default_response("cancel")
        alert.set_close_response("cancel")
        alert.connect("response", lambda _d, r: r == "discard" and proceed())
        alert.present(self)

    def _on_close_request(self, *_args) -> bool:
        if self._closing or not self.dirty:
            self._closing = True
            return False

        def close():
            self._closing = True
            self.close()

        self._confirm_discard(close)
        return True

    def _show_banner(
        self,
        text: str,
        button: str = "",
        action: Optional[Callable[[], None]] = None,
        *,
        kind: str = "notice",
    ) -> None:
        """*kind* ``"evaluation"`` is OpenSSH's word on the current draft and
        goes away with the next answer; a notice stays until dismissed."""
        if kind == "evaluation" and self._banner_kind == "notice" and self.banner.get_revealed():
            return
        self._banner_kind = kind
        self.banner.set_title(text)
        self.banner.set_button_label(button or None)
        self._banner_action = action
        self.banner.set_revealed(bool(text))

    def _on_banner_button(self, *_args) -> None:
        action, self._banner_action = self._banner_action, None
        self._banner_kind = ""
        self.banner.set_revealed(False)
        if callable(action):
            action()

    # ------------------------------------------------------------------
    # history
    # ------------------------------------------------------------------

    def _push_history(self) -> None:
        if self._history_source:
            GLib.source_remove(self._history_source)
            self._history_source = 0
        text = serialize(self.draft)[0]
        if self._history_index >= 0 and serialize(self._history[self._history_index])[0] == text:
            return
        del self._history[self._history_index + 1:]
        self._history.append(self.draft.clone())
        del self._history[:-_HISTORY_LIMIT]
        self._history_index = len(self._history) - 1

    def _schedule_history(self) -> None:
        """A snapshot once typing pauses, so undo steps over whole edits."""
        if self._history_source:
            GLib.source_remove(self._history_source)

        def fire():
            self._history_source = 0
            self._push_history()
            self._update_chrome()
            return False

        self._history_source = GLib.timeout_add(_HISTORY_DELAY_MS, fire)

    def _restore(self, index: int) -> None:
        self._history_index = index
        self.draft = self._history[index].clone()
        self._render_all()

    def undo(self) -> None:
        self._push_history()
        if self._history_index > 0:
            self._restore(self._history_index - 1)

    def redo(self) -> None:
        if self._history_index < len(self._history) - 1:
            self._restore(self._history_index + 1)

    # ------------------------------------------------------------------
    # editing the draft
    # ------------------------------------------------------------------

    def _changed(self, *, structure: bool) -> None:
        """The draft was edited: redraw when its shape changed, re-evaluate."""
        self._save_after_evaluation = False
        if structure:
            self._push_history()
            self._render_all()
        else:
            self._refresh_text()
            self._schedule_history()
            self._update_chrome()
            self._schedule_evaluation()

    def add_block(self, kind: str) -> None:
        if kind == GLOBAL:
            if any(block.kind == GLOBAL for block in self.draft.blocks):
                return
            self.draft.blocks.insert(0, Block(kind=GLOBAL, options=[Option()]))
        else:
            block = Block(kind=kind, patterns="", options=[Option()])
            self.draft.blocks.append(block)
        self._kept_visible.add(self.draft.blocks[0 if kind == GLOBAL else -1].uid)
        self._changed(structure=True)
        added = self.draft.blocks[0 if kind == GLOBAL else -1]
        GLib.idle_add(lambda: (self._focus_block(added.uid), False)[1])

    def _focus_block(self, uid: int) -> None:
        widgets = self._block_widgets.get(uid)
        if not widgets:
            return
        target = widgets.get("patterns") or widgets["card"]
        target.grab_focus()

    def _try_order(self, blocks: List[Block]) -> bool:
        if not can_place(blocks):
            self._toasts.add_toast(Adw.Toast.new(_(
                "Global options must stay above every Host and Match block: "
                "OpenSSH reads an option below a Host line as that host's."
            )))
            return False
        self.draft.blocks = blocks
        self._changed(structure=True)
        return True

    def move_block(self, uid: int, index: int) -> bool:
        blocks = list(self.draft.blocks)
        block = self.draft.find_block(uid)
        if block is None:
            return False
        old = blocks.index(block)
        blocks.pop(old)
        index = max(0, min(index, len(blocks)))
        blocks.insert(index, block)
        if blocks == self.draft.blocks:
            return False
        return self._try_order(blocks)

    def toggle_block(self, uid: int) -> None:
        block = self.draft.find_block(uid)
        if block is None:
            return
        self._kept_visible.add(uid)
        block.disabled = not block.disabled
        if not can_place(self.draft.blocks):
            block.disabled = not block.disabled
            self._toasts.add_toast(Adw.Toast.new(_(
                "Global options must stay above every Host and Match block: "
                "OpenSSH reads an option below a Host line as that host's."
            )))
            return
        self._changed(structure=True)

    def duplicate_block(self, uid: int) -> None:
        block = self.draft.find_block(uid)
        if block is None or block.kind == GLOBAL:
            return
        copy = Block(
            kind=block.kind, patterns=block.patterns, disabled=block.disabled,
            options=[Option(key=o.key, value=o.value, comment=o.comment) for o in block.options],
        )
        self._kept_visible.add(copy.uid)
        self.draft.blocks.insert(self.draft.blocks.index(block) + 1, copy)
        self._changed(structure=True)

    def delete_block(self, uid: int) -> None:
        block = self.draft.find_block(uid)
        if block is None:
            return
        self.draft.blocks.remove(block)
        self._changed(structure=True)

    def add_option(self, block_uid: int, key: str = "", value: str = "") -> None:
        block = self.draft.find_block(block_uid)
        if block is None:
            return
        option = Option(key=key, value=value)
        block.options.append(option)
        self._changed(structure=True)
        GLib.idle_add(lambda: (self._focus_option(option.uid, bool(key)), False)[1])

    def _focus_option(self, uid: int, value: bool) -> None:
        widgets = self._option_widgets.get(uid)
        if widgets:
            widgets["value" if value else "key"].grab_focus()

    def delete_option(self, uid: int) -> None:
        block, option = self.draft.find_option(uid)
        if option is None:
            return
        block.options.remove(option)
        self._changed(structure=True)

    def move_option(self, uid: int, block_uid: int, index: Optional[int]) -> bool:
        """Put an option at *index* of a block (None: at its end)."""
        source, option = self.draft.find_option(uid)
        target = self.draft.find_block(block_uid)
        if option is None or target is None:
            return False
        before = [(b.uid, [o.uid for o in b.options]) for b in self.draft.blocks]
        position = source.options.index(option)
        source.options.remove(option)
        if index is None:
            index = len(target.options)
        elif source is target and position < index:
            index -= 1
        target.options.insert(max(0, min(index, len(target.options))), option)
        if before == [(b.uid, [o.uid for o in b.options]) for b in self.draft.blocks]:
            return False
        self._changed(structure=True)
        return True

    def apply_raw(self) -> None:
        buffer = self.raw_view.get_buffer()
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
        self.draft = parse(text)
        self.apply_raw_button.set_sensitive(False)
        self._changed(structure=True)

    # ------------------------------------------------------------------
    # drawing the blocks
    # ------------------------------------------------------------------

    def _refresh_text(self) -> None:
        self._text, self._lines = serialize(self.draft)
        self._serial += 1

    def _render_all(self) -> None:
        self._refresh_text()
        self._render_blocks()
        self._render_quick_hosts()
        self._update_chrome()
        self._apply_evaluation()        # the last answer, until the new one
        self._schedule_evaluation(now=True)

    def _update_chrome(self) -> None:
        self.undo_button.set_sensitive(self._history_index > 0 or bool(self._history_source))
        self.redo_button.set_sensitive(self._history_index < len(self._history) - 1)
        self.save_button.set_sensitive(self.dirty and self._writable)
        self._title.set_title(
            _("Advanced Configuration") + (" •" if self.dirty else "")
        )
        self.add_global_button.set_sensitive(
            not any(block.kind == GLOBAL for block in self.draft.blocks)
        )
        _set_pill(self.block_count, messages.block_count(len(self.draft.blocks)), "muted")
        self._sync_raw()

    def _sync_raw(self) -> None:
        """Show the draft in the Text page unless it is being edited there."""
        if self.apply_raw_button.get_sensitive():
            return
        buffer = self.raw_view.get_buffer()
        if buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False) != self._text:
            self._raw_syncing = True
            buffer.set_text(self._text)
            self._raw_syncing = False

    def _on_raw_changed(self, buffer) -> None:
        if self._raw_syncing:
            return
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
        self.apply_raw_button.set_sensitive(text != self._text)
        self._update_chrome()      # typed text is an unsaved change

    def _render_blocks(self) -> None:
        self._rendering = True
        position = self._blocks_scroll.get_vadjustment().get_value()
        self._block_widgets.clear()
        self._option_widgets.clear()
        while (child := self._blocks_box.get_first_child()) is not None:
            self._blocks_box.remove(child)
        if not self.draft.blocks:
            self._blocks_box.append(Adw.StatusPage(
                icon_name="document-properties-symbolic",
                title=_("No Configuration Rules"),
                description=_("Add a Host or Match block to start."),
            ))
        for index, block in enumerate(self.draft.blocks):
            self._blocks_box.append(self._build_block(index, block))
        self._rendering = False
        GLib.idle_add(
            lambda: (self._blocks_scroll.get_vadjustment().set_value(position), False)[1]
        )

    def _build_block(self, index: int, block: Block) -> Gtk.Widget:
        # A card in two parts: a header band, which is the handle the block
        # is dragged by, and under it the block's options, indented the way
        # they are written below a Host or Match line.
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        card.add_css_class("card")
        card.add_css_class("advcfg-block")
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        body.add_css_class("advcfg-body")
        if block.disabled:
            card.add_css_class("disabled")
        widgets: dict = {"card": card, "block": block}
        self._block_widgets[block.uid] = widgets

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.add_css_class("advcfg-header")
        movable = block.kind != GLOBAL
        if movable:
            header.add_css_class("movable")
            header.set_cursor_from_name("grab")
            header.set_tooltip_text(_("Drag to reorder"))
        handle = Gtk.Image.new_from_icon_name("list-drag-handle-symbolic")
        handle.add_css_class("dim-label")
        handle.set_opacity(1.0 if movable else 0.3)
        header.append(handle)
        number = Gtk.Label(label=f"#{index + 1}")
        number.add_css_class("dim-label")
        number.add_css_class("numeric")
        header.append(number)
        kind_label = {GLOBAL: _("Global"), HOST: "Host", MATCH: "Match"}[block.kind]
        header.append(_pill(kind_label, "accent" if block.kind != GLOBAL else "muted"))
        if block.kind == GLOBAL:
            text = Gtk.Label(label=_("Options for every target"), xalign=0)
            text.add_css_class("dim-label")
            text.set_hexpand(True)
            header.append(text)
        else:
            entry = Gtk.Entry(text=block.patterns)
            entry.add_css_class("monospace")
            entry.set_hexpand(True)
            entry.set_placeholder_text(
                "*.example.com !bastion" if block.kind == HOST else "host *.prod user deploy"
            )
            entry.update_property(
                [Gtk.AccessibleProperty.LABEL],
                [_("Host patterns") if block.kind == HOST else _("Match criteria")],
            )
            entry.connect("changed", self._on_patterns_changed, block)
            header.append(entry)
            widgets["patterns"] = entry
        status = _pill()
        header.append(status)
        widgets["status"] = status
        if movable:
            for icon, delta, tip in (
                ("go-up-symbolic", -1, _("Move up")),
                ("go-down-symbolic", 1, _("Move down")),
            ):
                button = Gtk.Button(icon_name=icon, tooltip_text=tip)
                button.add_css_class("flat")
                button.set_sensitive(0 <= index + delta < len(self.draft.blocks))
                button.connect(
                    "clicked",
                    lambda *_a, u=block.uid, i=index + delta: self.move_block(u, i),
                )
                header.append(button)
        header.append(self._block_menu(block))
        card.append(header)
        card.append(body)

        if block.note or block.uid in self._notes_open:
            note = Gtk.Entry(text=block.note)
            note.set_placeholder_text(_("Note (written as a comment above the block)"))
            note.add_css_class("dim-label")
            note.connect("changed", self._on_note_changed, block)
            body.append(note)
            widgets["note"] = note
        warning = Gtk.Label(xalign=0, wrap=True)
        warning.add_css_class("error")
        warning.set_visible(False)
        body.append(warning)
        widgets["warning"] = warning

        rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        if block.kind != GLOBAL:
            rows.add_css_class("advcfg-options")   # global options are not indented
        for option in block.options:
            rows.append(self._build_option(block, option))
        body.append(rows)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        add = Gtk.Button(label=_("Add Option"))
        add.add_css_class("flat")
        add.connect("clicked", lambda *_a, u=block.uid: self.add_option(u))
        footer.append(add)
        presets = Gio.Menu()
        group = Gio.SimpleActionGroup()
        for number_, (key, value) in enumerate(COMMON_OPTIONS):
            action = Gio.SimpleAction.new(f"preset{number_}", None)
            action.connect(
                "activate", lambda *_a, u=block.uid, k=key, v=value: self.add_option(u, k, v)
            )
            group.add_action(action)
            presets.append(key, f"preset.preset{number_}")
        preset_button = Gtk.MenuButton(label=_("Common Option"), menu_model=presets)
        preset_button.add_css_class("flat")
        preset_button.insert_action_group("preset", group)
        footer.append(preset_button)
        rows.append(footer)

        if movable:
            source = Gtk.DragSource()
            source.set_actions(Gdk.DragAction.MOVE)
            source.connect("prepare", lambda *_a, u=block.uid: Gdk.ContentProvider.new_for_value(
                f"advcfg-block:{u}"
            ))
            source.connect("drag-begin", lambda src, _drag, c=card: self._drag_icon(src, c))
            header.add_controller(source)       # the whole band, not only the grip
        target = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.MOVE)
        target.connect("motion", self._on_drop_motion, card)
        target.connect("leave", lambda *_a, c=card: self._clear_drop(c))
        target.connect("drop", self._on_block_drop, block, card)
        card.add_controller(target)
        return card

    @staticmethod
    def _drag_icon(source, widget) -> None:
        """Drag a picture of the block or option itself."""
        try:
            source.set_icon(Gtk.WidgetPaintable.new(widget), 0, 0)
        except Exception:
            pass

    def _block_menu(self, block: Block) -> Gtk.Widget:
        menu = Gio.Menu()
        group = Gio.SimpleActionGroup()

        def add(name, label, callback):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_a: callback())
            group.add_action(action)
            menu.append(label, f"block.{name}")

        add("toggle", _("Enable Block") if block.disabled else _("Disable Block"),
            lambda: self.toggle_block(block.uid))
        add("note", _("Add or Edit Note"), lambda: self._open_note(block))
        if block.kind != GLOBAL:
            add("duplicate", _("Duplicate Block"), lambda: self.duplicate_block(block.uid))
        add("delete", _("Delete Block"), lambda: self.delete_block(block.uid))
        button = Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu)
        button.add_css_class("flat")
        button.set_tooltip_text(_("Block options"))
        button.insert_action_group("block", group)
        return button

    def _open_note(self, block: Block) -> None:
        self._notes_open.add(block.uid)
        self._render_blocks()
        self._apply_evaluation()
        note = self._block_widgets.get(block.uid, {}).get("note")
        if note is not None:
            note.grab_focus()

    def _build_option(self, block: Block, option: Option) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        handle = Gtk.Image.new_from_icon_name("list-drag-handle-symbolic")
        handle.add_css_class("dim-label")
        handle.set_tooltip_text(_("Drag to reorder or to another block"))
        row.append(handle)
        key = Gtk.Entry(text=option.key)
        key.add_css_class("monospace")
        key.set_width_chars(22)
        key.set_placeholder_text(_("Option"))
        key.connect("changed", self._on_option_changed, option, "key")
        row.append(key)
        value = Gtk.Entry(text=option.value)
        value.add_css_class("monospace")
        value.set_hexpand(True)
        value.set_placeholder_text(_("Value"))
        value.connect("changed", self._on_option_changed, option, "value")
        row.append(value)
        status = _pill()
        status.set_visible(False)
        row.append(status)
        remove = Gtk.Button(icon_name="edit-delete-symbolic", tooltip_text=_("Remove option"))
        remove.add_css_class("flat")
        remove.connect("clicked", lambda *_a, u=option.uid: self.delete_option(u))
        row.append(remove)
        self._option_widgets[option.uid] = {
            "row": row, "key": key, "value": value, "status": status,
        }
        source = Gtk.DragSource()
        source.set_actions(Gdk.DragAction.MOVE)
        source.connect("prepare", lambda *_a, u=option.uid: Gdk.ContentProvider.new_for_value(
            f"advcfg-option:{u}"
        ))
        handle.add_controller(source)
        target = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.MOVE)
        target.connect("motion", self._on_drop_motion, row)
        target.connect("leave", lambda *_a, r=row: self._clear_drop(r))
        target.connect("drop", self._on_option_drop, block, option, row)
        row.add_controller(target)
        return row

    # -- entries ------------------------------------------------------------

    def _on_patterns_changed(self, entry, block: Block) -> None:
        if self._rendering:
            return
        self._kept_visible.add(block.uid)
        block.patterns = entry.get_text()
        self._changed(structure=False)
        self._render_quick_hosts()

    def _on_note_changed(self, entry, block: Block) -> None:
        if self._rendering:
            return
        self._kept_visible.add(block.uid)
        block.note = entry.get_text()
        self._changed(structure=False)

    def _on_option_changed(self, entry, option: Option, field: str) -> None:
        if self._rendering:
            return
        owner, _option = self.draft.find_option(option.uid)
        if owner is not None:
            self._kept_visible.add(owner.uid)
        setattr(option, field, entry.get_text())
        self._changed(structure=False)

    # -- drag and drop ------------------------------------------------------

    @staticmethod
    def _clear_drop(widget) -> None:
        widget.remove_css_class("advcfg-drop-before")
        widget.remove_css_class("advcfg-drop-after")

    def _on_drop_motion(self, _target, _x, y, widget):
        self._clear_drop(widget)
        widget.add_css_class(
            "advcfg-drop-before" if y < widget.get_height() / 2 else "advcfg-drop-after"
        )
        return Gdk.DragAction.MOVE

    def _on_block_drop(self, _target, value, _x, y, block: Block, card) -> bool:
        self._clear_drop(card)
        kind, _sep, raw = str(value or "").partition(":")
        if not raw.isdigit():
            return False
        uid = int(raw)
        if kind == "advcfg-option":
            return self.move_option(uid, block.uid, None)
        if kind != "advcfg-block" or uid == block.uid:
            return False
        blocks = self.draft.blocks
        source = self.draft.find_block(uid)
        if source is None:
            return False
        index = blocks.index(block) + (0 if y < card.get_height() / 2 else 1)
        if blocks.index(source) < index:
            index -= 1
        return self.move_block(uid, index)

    def _on_option_drop(self, _target, value, _x, y, block: Block, option: Option, row) -> bool:
        self._clear_drop(row)
        kind, _sep, raw = str(value or "").partition(":")
        if kind != "advcfg-option" or not raw.isdigit() or int(raw) == option.uid:
            return False
        index = block.options.index(option) + (0 if y < row.get_height() / 2 else 1)
        return self.move_option(int(raw), block.uid, index)

    # ------------------------------------------------------------------
    # targets
    # ------------------------------------------------------------------

    def host_names(self) -> List[str]:
        """Targets to offer: the hosts the configuration names literally,
        then the saved connections."""
        names: List[str] = []
        for block in self.draft.blocks:
            if block.kind != HOST or block.disabled:
                continue
            for token in block.patterns.split():
                if token and not any(char in token for char in "*?!\"") and token not in names:
                    names.append(token)
        try:
            for name in self._hosts() or ():
                if name and name not in names:
                    names.append(name)
        except Exception:
            logger.debug("saved host list unavailable", exc_info=True)
        return names

    def _render_quick_hosts(self) -> None:
        names = self.host_names()
        if names == getattr(self, "_host_names_shown", None):
            return
        self._host_names_shown = names
        while (child := self._host_list.get_first_child()) is not None:
            self._host_list.remove(child)
        for name in names:
            label = Gtk.Label(label=name, xalign=0)
            label.add_css_class("monospace")
            row = Gtk.ListBoxRow()
            row.set_child(label)
            row._host = name
            self._host_list.append(row)
        self.target_button.set_sensitive(bool(names))

    def _on_host_chosen(self, _listbox, row) -> None:
        self._host_popover.popdown()
        self.target_entry.set_text(row._host)

    @staticmethod
    def _children(widget) -> list:
        found = []
        child = widget.get_first_child()
        while child is not None:
            found.append(child)
            child = child.get_next_sibling()
        return found

    # ------------------------------------------------------------------
    # evaluation
    # ------------------------------------------------------------------

    def _request(self) -> Optional[EvaluateSshConfigDraftRequest]:
        user, host = split_target(self.target_entry.get_text())
        if not host or any(char.isspace() for char in host):
            return None
        try:
            return EvaluateSshConfigDraftRequest(
                text=self._text, host=host, user=user,
                allowed_exec=tuple(sorted(self._allowed_exec)),
            )
        except (TypeError, ValueError):
            return None

    def _schedule_evaluation(self, *, now: bool = False) -> None:
        if self._evaluate_source:
            GLib.source_remove(self._evaluate_source)
            self._evaluate_source = 0
        if now:
            self._evaluate()
        else:
            self._evaluate_source = GLib.timeout_add(_EVALUATE_DELAY_MS, self._evaluate)

    def _evaluate(self) -> bool:
        self._evaluate_source = 0
        request = self._request()
        if request is None:
            self._evaluation = None
            self._apply_evaluation()
            return False
        self._request_serial = serial = self._request_serial + 1
        draft_serial = self._serial
        lines = self._lines

        def done(result, error):
            if serial != self._request_serial:
                return          # a newer question was asked meanwhile
            if isinstance(error, SshPilotError) and error.code is ErrorCode.OPERATION_CANCELLED:
                return          # the daemon dropped it for a newer question
            if error is not None:
                logger.debug("draft evaluation failed", exc_info=error)
                self._evaluation = None
                self._save_after_evaluation = False
                self._apply_evaluation()
                self._show_banner(
                    _("OpenSSH could not be asked about this configuration."),
                    kind="evaluation",
                )
                return
            self._evaluation = result
            self._evaluated_serial = draft_serial
            self._evaluated_lines = lines
            self._apply_evaluation()
            if self._save_after_evaluation and draft_serial == self._serial:
                self._save_after_evaluation = False
                self.save()

        self._run(lambda: self._client.evaluate_ssh_config_draft(request), done)
        return False

    def _allow_exec(self) -> None:
        """Agree to the commands the banner is about: those, word for word,
        and for this target. Another new command, an edited one, or another
        target asks again (the daemon checks every command it is sent)."""
        evaluation = self._evaluation
        if evaluation is not None:
            self._allowed_exec.update(evaluation.pending_exec)
        self._schedule_evaluation(now=True)

    def _on_target_changed(self, *_args) -> None:
        # A command can depend on the target (%h, %r): agreement to run it
        # was for the target on screen at the time.
        self._allowed_exec.clear()
        self._kept_visible.clear()
        self._schedule_evaluation()

    def _apply_evaluation(self) -> None:
        """Show OpenSSH's answer on the rules and in the pages."""
        evaluation = self._evaluation
        current = evaluation is not None and self._evaluated_serial == self._serial
        lines = self._evaluated_lines if evaluation else self._lines
        answered = bool(evaluation and evaluation.evaluated)
        matched = set(evaluation.matched_blocks) if answered else set()
        states = {item.line: item for item in evaluation.lines} if answered else {}
        errors = {error.line: error for error in evaluation.errors if not error.file} if evaluation else {}
        if current:
            if evaluation.pending_exec:
                self._show_banner(
                    messages.pending_exec_text(evaluation), _("Run and Evaluate"),
                    self._allow_exec, kind="evaluation",
                )
            elif evaluation.errors:
                self._show_banner(messages.error_text(evaluation.errors[0]), kind="evaluation")
            elif self._banner_kind == "evaluation":
                self._banner_kind = ""
                self.banner.set_revealed(False)
        count = 0
        for uid, widgets in self._block_widgets.items():
            block = widgets["block"]
            header_line = lines.headers.get(uid, 0)
            is_matched = None
            if answered and not block.disabled:
                is_matched = block.kind == GLOBAL or header_line in matched
            label, style, tooltip = messages.block_status(
                block.disabled, is_matched, block.kind == GLOBAL
            )
            _set_pill(widgets["status"], label, style, tooltip)
            # Filtering by a host leaves the blocks OpenSSH applies to it
            # (and any being worked on); with no host every block shows.
            card = widgets["card"]
            if is_matched:
                count += 1
            card.set_visible(
                not answered or bool(is_matched) or uid in self._kept_visible
            )
            error = errors.get(header_line) if header_line else None
            widgets["warning"].set_label(messages.error_text(error) if error else "")
            widgets["warning"].set_visible(error is not None)
            for option in block.options:
                self._apply_option(option, lines, states, errors)
        _set_pill(
            self.matched_count,
            messages.matched_count(count) if answered else "", "success",
        )
        self.context_label.set_label(
            messages.context_text(evaluation, self._display_name) if evaluation else ""
        )
        self.context_label.set_visible(evaluation is not None)
        self._render_effective()
        self._render_trace()
        self._render_includes()

    def _apply_option(self, option: Option, lines: LineMap, states: dict, errors: dict) -> None:
        widgets = self._option_widgets.get(option.uid)
        if widgets is None:
            return
        line = lines.options.get(option.uid, 0)
        row, status = widgets["row"], widgets["status"]
        error = errors.get(line) if line else None
        item = states.get(line) if line else None
        for entry in (widgets["key"], widgets["value"]):
            (entry.add_css_class if error else entry.remove_css_class)("error")
        if error is not None:
            _set_pill(status, _("Rejected"), "warning", messages.error_text(error))
            row.remove_css_class("advcfg-unused")
            return
        if item is None:
            status.set_visible(False)
            row.remove_css_class("advcfg-unused")
            return
        _set_pill(
            status, messages.state_label(item.state),
            messages.STATE_STYLES[item.state], messages.state_tooltip(item),
        )
        if item.state in (State.SHADOWED, State.FORCED, State.INACTIVE):
            row.add_css_class("advcfg-unused")
        else:
            row.remove_css_class("advcfg-unused")

    # -- the pages ---------------------------------------------------------

    @staticmethod
    def _clear_list(listbox: Gtk.ListBox) -> None:
        while (child := listbox.get_first_child()) is not None:
            listbox.remove(child)

    def _line_owner(self, line: int) -> Optional[int]:
        """The uid of the option or block the draft has at *line*."""
        lines = self._evaluated_lines
        for uid, number in lines.options.items():
            if number == line:
                return uid
        for uid, number in lines.headers.items():
            if number == line and number:
                return uid
        return None

    def jump_to_line(self, line: int) -> None:
        uid = self._line_owner(line)
        widget = None
        if uid in self._option_widgets:
            widget = self._option_widgets[uid]["row"]
        elif uid in self._block_widgets:
            widget = self._block_widgets[uid]["card"]
        if widget is None:
            return
        widget.grab_focus()
        child = widget.get_first_child()
        (self._option_widgets.get(uid, {}).get("value") or child or widget).grab_focus()
        widget.add_css_class("advcfg-flash")
        GLib.timeout_add(900, lambda: (widget.remove_css_class("advcfg-flash"), False)[1])

    def _source_widget(self, key: str, evaluation: SshConfigDraftEvaluation, sources: dict):
        forced = {item.key for item in evaluation.forced}
        if key in forced:
            return _pill(messages.forced_source(), "purple")
        source = sources.get(key)
        if source is None:
            label = Gtk.Label(
                label=messages.default_source() if key in evaluation.defaults
                else messages.unknown_source()
            )
            label.add_css_class("dim-label")
            label.add_css_class("caption")
            return label
        text = messages.where(source.file, source.line, source.block)
        if source.file:
            label = Gtk.Label(label=text)
            label.add_css_class("caption")
            label.set_ellipsize(Pango.EllipsizeMode.START)
            label.set_max_width_chars(34)
            label.set_tooltip_text(text)
            return label
        button = Gtk.Button(label=text)
        button.add_css_class("flat")
        button.add_css_class("caption")
        button.set_valign(Gtk.Align.CENTER)
        button.set_tooltip_text(_("Show this line in the rules"))
        button.connect("clicked", lambda *_a, n=source.line: self.jump_to_line(n))
        return button

    def _render_effective(self) -> None:
        evaluation = self._evaluation
        self._clear_list(self._effective_list)
        answered = bool(evaluation and evaluation.evaluated)
        self.summary.set_visible(answered)
        self._effective_list.set_visible(answered)
        if not answered:
            if evaluation is None:
                note = _("Filter by a host to see what OpenSSH would use for it.")
            elif evaluation.pending_exec:
                note = messages.pending_exec_text(evaluation)
            else:
                note = "\n".join(messages.error_text(e) for e in evaluation.errors)
            self._effective_note.set_label(note)
            self._effective_note.set_visible(True)
            return
        self._effective_note.set_visible(False)
        values: Dict[str, List[str]] = {}
        for item in evaluation.values:
            values.setdefault(item.key, []).append(item.value)
        user, host = split_target(self.target_entry.get_text())
        command = "ssh " + (f"-l {user} " if user else "") + host

        def first(key, fallback=""):
            return (values.get(key) or [fallback])[0]

        proxy = first("proxyjump") or first("proxycommand")
        for key, text in (
            ("command", command),
            ("host", f"{first('hostname', host)} : {first('port', '22')}"),
            ("user", first("user")),
            ("proxy", proxy if proxy and proxy != "none" else "—"),
            ("keys", "\n".join(values.get("identityfile", ())) or "—"),
        ):
            self._summary_rows[key].set_subtitle(GLib.markup_escape_text(text or "—"))
        sources = {source.key: source for source in evaluation.sources}
        unused: Dict[str, int] = {}
        lines = self._evaluated_lines
        option_lines = {number: uid for uid, number in lines.options.items()}
        for item in evaluation.lines:
            if item.state in (State.SHADOWED, State.FORCED):
                _block, option = self.draft.find_option(option_lines.get(item.line, -1))
                if option is not None:
                    name = canonical_keyword(option.keyword)
                    unused[name] = unused.get(name, 0) + 1
        needle = self.search.get_text().strip().lower()
        for key in sorted(values):
            entries = values[key]
            configured = key in sources or key in {f.key for f in evaluation.forced} or (
                key not in evaluation.defaults
            )
            if self.only_configured.get_active() and not configured:
                continue
            if self.only_shadowed.get_active() and not unused.get(key):
                continue
            if needle and needle not in key and not any(needle in v.lower() for v in entries):
                continue
            row = Adw.ActionRow(title=key)
            row.set_subtitle(GLib.markup_escape_text("\n".join(entries) or "—"))
            row.set_subtitle_selectable(True)
            row.add_css_class("property")
            if not configured:
                row.add_css_class("dim-label")
            if unused.get(key):
                row.add_suffix(_pill(messages.shadowed_count(unused[key]), "warning"))
            row.add_suffix(self._source_widget(key, evaluation, sources))
            self._effective_list.append(row)

    def _render_trace(self) -> None:
        evaluation = self._evaluation
        self._clear_list(self._trace_list)
        if not (evaluation and evaluation.evaluated):
            return
        lines = self._evaluated_lines
        states = {item.line: item.state for item in evaluation.lines}
        matched = set(evaluation.matched_blocks)

        def add(title, subtitle, pill=None):
            row = Adw.ActionRow(title=GLib.markup_escape_text(title))
            row.set_subtitle(GLib.markup_escape_text(subtitle))
            if pill is not None:
                row.add_suffix(pill)
            self._trace_list.append(row)

        if evaluation.forced:
            add(
                messages.forced_source(),
                ", ".join(f"{item.key}={item.value}" for item in evaluation.forced),
                _pill(_("First"), "purple"),
            )
        for index, block in enumerate(self.draft.blocks):
            header = lines.headers.get(block.uid, 0)
            is_matched = None if block.disabled else (
                block.kind == GLOBAL or header in matched
            )
            label, style, _tip = messages.block_status(
                block.disabled, is_matched, block.kind == GLOBAL
            )
            name = {GLOBAL: _("Global"), HOST: "Host", MATCH: "Match"}[block.kind]
            title = f"#{index + 1}  {name} {block.patterns}".rstrip()
            counts: Dict[State, int] = {}
            for option in block.options:
                state = states.get(lines.options.get(option.uid, 0))
                if state is not None:
                    counts[state] = counts.get(state, 0) + 1
            parts = [
                f"{messages.state_label(state)}: {counts[state]}"
                for state in (State.APPLIED, State.ACCUMULATED, State.INCLUDE,
                              State.SHADOWED, State.FORCED)
                if counts.get(state)
            ]
            add(title, " · ".join(parts), _pill(label, style))
        if evaluation.system_config:
            supplied = sum(1 for s in evaluation.sources if s.file == evaluation.system_config)
            add(
                _("System-wide configuration"),
                evaluation.system_config + " · " + f"{messages.state_label(State.APPLIED)}: {supplied}",
                _pill(_("Last"), "muted"),
            )

    def _render_includes(self) -> None:
        evaluation = self._evaluation
        self._clear_list(self._include_list)
        if not (evaluation and evaluation.evaluated):
            return
        lines = self._evaluated_lines
        option_lines = {number: uid for uid, number in lines.options.items()}
        for include in evaluation.includes:
            _block, option = self.draft.find_option(option_lines.get(include.line, -1))
            pattern = option.value if option is not None else ""
            row = Adw.ActionRow(title=GLib.markup_escape_text(f"Include {pattern}"))
            row.set_subtitle(GLib.markup_escape_text(
                "\n".join(include.files) or _("No file matches.")
            ))
            row.set_subtitle_selectable(True)
            self._include_list.append(row)
        if evaluation.system_config:
            row = Adw.ActionRow(title=_("System-wide configuration"))
            row.set_subtitle(GLib.markup_escape_text(evaluation.system_config))
            row.set_subtitle_selectable(True)
            self._include_list.append(row)
        if not evaluation.includes and not evaluation.system_config:
            row = Adw.ActionRow(title=_("No other file is read for this target."))
            self._include_list.append(row)
