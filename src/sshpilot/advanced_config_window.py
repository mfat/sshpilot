"""Advanced Configuration: the SSH configuration as OpenSSH reads it.

Sidebar: the blocks of the configuration in file order (the order OpenSSH
reads them in), grouped by file; dragged to reorder. Content: the block
selected, in one of three views:

* **Structured**: the block's options as fields, each empty one showing in
  grey what OpenSSH will use instead and where that comes from;
* **Effective**: what OpenSSH uses for the host being checked (``ssh -G``),
  and the line behind each value;
* **Raw**: the text of the file, the block's lines marked.

The host being checked is the one typed above the views, else the host the
selected block names. Copy Command and Test Connection are about it.

Nothing here decides what applies. Every answer comes from the daemon, which
asks OpenSSH (``ssh -G``) about the draft in the context a connection would
read the saved file in: see :mod:`sshpilot.core.ssh_config_draft`. The window
never touches the file: it loads and saves text through the daemon.

The window is a few parts with one job each:

* :class:`~sshpilot.advanced_config_model.DraftEditor` (no GTK): the draft,
  its text, every edit and the undo history;
* :class:`~sshpilot.advanced_config_sidebar.BlockSidebar`: the blocks;
* :class:`~sshpilot.advanced_config_structured_view.StructuredView`,
  :class:`~sshpilot.advanced_config_effective_view.EffectiveView` and
  :class:`~sshpilot.advanced_config_raw_view.RawView`: the three views,
  updated in place.

This module is the controller between them: it turns gestures into edits,
asks the daemon, and hands each answer to the views.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from gettext import gettext as _
from typing import Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

from . import advanced_config_fields as fields  # noqa: E402
from .advanced_config_effective_view import Answer, EffectiveView  # noqa: E402
from .advanced_config_model import (  # noqa: E402
    first_host_name,
    plain_host_names,
    sample_hosts,
    DraftEditor,
    Refusal,
    can_place,  # noqa: F401 - re-exported for callers and tests
)
from .advanced_config_raw_view import RawView  # noqa: E402
from .advanced_config_sidebar import BlockSidebar, block_matches  # noqa: E402
from .advanced_config_structured_view import AnswerView, StructuredView  # noqa: E402
from .advanced_config_widgets import pill, set_pill  # noqa: E402
from .api.errors import ErrorCode, SshPilotError  # noqa: E402
from .api.models.common import SessionId  # noqa: E402
from .api.models.connections import (  # noqa: E402
    EvaluateSshConfigDraftRequest,
    SaveSshConfigTextRequest,
    SshConfigDraftEvaluation,
)
from .gtk import advanced_config_messages as messages  # noqa: E402
from .ssh_config_blocks import GLOBAL, HOST, LineMap  # noqa: E402
from .ssh_config_document import declared_hosts  # noqa: E402

logger = logging.getLogger(__name__)

_EVALUATE_DELAY_MS = 250
_HISTORY_DELAY_MS = 700
#: Asked about when saving: OpenSSH reads every line whatever the host, so a
#: name no block mentions is enough to learn whether it accepts the text.
_CHECK_HOST = "sshpilot-config-check"

#: Below this width the sidebar folds away and the view switcher moves to
#: the bottom of the window.
_NARROW_BELOW = 760
_MIN_WIDTH, _MIN_HEIGHT = 360, 420

#: How long the draft must have stood before its hosts are asked about ahead
#: of time, and how many of them are: beyond that, a host is asked about
#: when it is selected.
_PREFETCH_DELAY_MS = 350
_PREFETCH_HOSTS = 150

#: How long Test Connection gives the host, prompts included, in seconds.
_TEST_TIMEOUT = 120

PAGES = ("structured", "effective", "raw")

_CSS = """
.advcfg-pill { font-size: 0.82em; font-weight: 600; padding: 2px 9px; border-radius: 999px; }
.advcfg-pill.success { background-color: alpha(@success_color, 0.16); color: @success_color; }
.advcfg-pill.warning { background-color: alpha(@warning_color, 0.18); color: @warning_color; }
.advcfg-pill.error { background-color: alpha(@error_color, 0.16); color: @error_color; }
.advcfg-pill.accent { background-color: alpha(@accent_color, 0.15); color: @accent_color; }
.advcfg-pill.purple { background-color: alpha(@purple_3, 0.18); color: @purple_3; }
.advcfg-pill.muted { background-color: alpha(currentColor, 0.08); color: alpha(currentColor, 0.65); }
.advcfg-context { padding: 6px 12px; }
.advcfg-block-list > row.advcfg-drop-above { box-shadow: inset 0 2px 0 0 @accent_bg_color; }
.advcfg-block-list > row.advcfg-drop-below { box-shadow: inset 0 -2px 0 0 @accent_bg_color; }
.advcfg-block-list:drop(active) { box-shadow: none; }
.advcfg-block-list > row.advcfg-file-row { min-height: 0; }
.advcfg-flash { outline: 2px solid @accent_color; outline-offset: 2px; }
.advcfg-drag-handle { opacity: 0.45; }
row:hover .advcfg-drag-handle { opacity: 0.9; }
.advcfg-chip { padding: 2px 10px; border-radius: 999px; font-size: 0.9em; }
.advcfg-command-bar { padding: 6px 6px 6px 14px; }
entry.warning { color: @warning_color; }
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


def split_target(text: str) -> tuple:
    """``(user, host)`` of ``[user@]host``; the user is None when absent."""
    text = text.strip()
    user, separator, host = text.rpartition("@")
    return (user or None, host) if separator else (None, text)


class AdvancedConfigBase:
    """Advanced Configuration, whatever shows it.

    The controller and its widgets, without a place on screen of their own:
    :class:`AdvancedConfigWindow` is it as a window, and
    :class:`AdvancedConfigView` as a widget for the main window, with the
    list of blocks handed to the main sidebar. The few things that differ
    between the two are the ``_host_*`` methods and ``embedded``.

    *client* is the daemon client; *on_saved* runs after each save.
    """

    #: True when shown inside another window: no title buttons, and the
    #: list of blocks is shown by the host, not beside the views.
    embedded = False

    def __init__(
        self,
        parent: Optional[Gtk.Window],
        client,
        *,
        on_saved: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__()
        _ensure_css()
        self._client = client
        #: The window this one was opened from: its client bridge carries
        #: the prompts of a connection test.
        self._parent_window = parent
        self._on_saved = on_saved
        self._saved_listeners: list = []
        self._closed_listeners: list = []
        self._told_closed = False
        #: The file on screen, as the daemon names it ("" is the root), and
        #: every file of the configuration (the root first).
        self._file = ""
        self._files: tuple = ()
        #: The line map of the text as loaded: a line number of the saved
        #: file finds its block through it however the draft has changed.
        self._loaded_lines = LineMap({}, {})
        #: Where :meth:`go_to` was asked to go, until the file is on screen.
        self._pending_target: Optional[dict] = None
        self._pending_block = None
        #: Answers had for the draft as it is: see :meth:`_answer_cache`.
        self._answers: dict = {}
        self._answers_key: Optional[tuple] = None
        self._ask_lock = threading.Lock()
        self._prefetching = False
        self._prefetch_source = 0
        #: True while a move of the Raw view's cursor is selecting a block.
        self._selecting_from_raw = False
        #: The host typed above the views: the one OpenSSH is asked about,
        #: whichever block is selected. Empty: the selected block's own.
        self._pinned_host = ""
        #: The pinned host was typed in the host-to-check field by the user
        #: (not put there by the window: a source followed, a search, go_to).
        self._pin_typed = False
        self._check_syncing = False
        #: The host to check was put there by the search (an exact name).
        self._check_from_search = False
        self._testing = False
        self._host_setup(parent)

        self.editor = DraftEditor()
        self._revision = ""
        self._display_name = ""
        self._writable = True
        self._loaded = False
        #: The last answer and the draft (line map, serial) it was about.
        self._evaluation: Optional[SshConfigDraftEvaluation] = None
        #: In place of an evaluation while the block picked names no host
        #: of its own: ``(header lines of the related blocks, the hosts of
        #: the configuration it applies to, the line map)``.
        self._related: Optional[tuple] = None
        self._evaluated_lines = LineMap({}, {})
        self._evaluated_serial = 0
        self._request_serial = 0
        self._evaluate_source = 0
        self._history_source = 0
        self._allowed_exec: set = set()
        self.selected_uid: Optional[int] = None
        #: Where the selected block stood: a draft read anew (Apply, Reload)
        #: selects the block at the same place.
        self._selected_index = 0
        self._saving = False
        self._closing = False

        self._build()
        keys = Gtk.ShortcutController()
        keys.set_scope(Gtk.ShortcutScope.LOCAL)
        for trigger, delta in (("<Alt>Up", -1), ("<Alt>Down", 1)):
            keys.add_shortcut(Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string(trigger),
                Gtk.CallbackAction.new(lambda *_a, d=delta: self.move_selected(d)),
            ))
        self.add_controller(keys)
        # Undo and redo are the window's, wherever the keyboard is: a field
        # has an undo of its own for what was typed into it, which knows
        # nothing of a block deleted or moved, and would answer Ctrl+Z
        # first. So the keys are taken on the way down, before any field
        # sees them. (The Raw view keeps its own: text typed there is not
        # in the draft until it is applied.)
        history_keys = Gtk.ShortcutController()
        history_keys.set_scope(Gtk.ShortcutScope.LOCAL)
        history_keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        for trigger, step in (
            ("<Control>z", self.undo), ("<Meta>z", self.undo),
            ("<Control><Shift>z", self.redo), ("<Meta><Shift>z", self.redo),
            ("<Control>y", self.redo),
        ):
            history_keys.add_shortcut(Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string(trigger),
                Gtk.CallbackAction.new(lambda *_a, s=step: self._history_key(s)),
            ))
        self.add_controller(history_keys)
        self._load()

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------

    def _build(self) -> None:
        self._toasts = Adw.ToastOverlay()
        self._host_set_content(self._toasts)
        view = Adw.ToolbarView()
        self._toasts.set_child(view)

        self.structured = StructuredView(self)
        self.effective = EffectiveView(self)
        self.raw = RawView(self)
        self.stack = Adw.ViewStack()
        self.stack.set_vexpand(True)
        for name, widget, title, icon in (
            ("structured", self.structured, _("Structured"), "document-properties-symbolic"),
            ("effective", self.effective, _("Effective"), "emblem-ok-symbolic"),
            ("raw", self.raw, _("Raw"), "text-x-generic-symbolic"),
        ):
            self.stack.add_titled_with_icon(widget, name, title, icon)
        self.stack.connect("notify::visible-child-name", self._on_page_shown)
        #: Effective was on screen when it was hidden (see _sync_effective_page).
        self._effective_left = False

        header = Adw.HeaderBar()
        if self.embedded:
            # A bar of the main window's content: its buttons are its own.
            header.set_show_start_title_buttons(False)
            header.set_show_end_title_buttons(False)
        self.switcher = Adw.ViewSwitcher(stack=self.stack)
        self.switcher.set_policy(Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(self.switcher)
        self.sidebar_button = Gtk.ToggleButton(
            icon_name="sidebar-show-symbolic", active=True,
            tooltip_text=_("Toggle Sidebar"),
        )
        # Embedded, the list of blocks is in the host's sidebar.
        self.sidebar_button.set_visible(not self.embedded)
        header.pack_start(self.sidebar_button)
        # The keys are named beside the words: the buttons are small, and
        # an undo nobody finds is no undo.
        self.undo_button = Gtk.Button(
            icon_name="edit-undo-symbolic", tooltip_text=_("Undo") + " (Ctrl+Z)"
        )
        self.undo_button.connect("clicked", lambda *_a: self.undo())
        self.redo_button = Gtk.Button(
            icon_name="edit-redo-symbolic", tooltip_text=_("Redo") + " (Ctrl+Shift+Z)"
        )
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

        # Two banners, so that one never hides the other: what happened to
        # the file (read-only, changed on disk, could not be saved), and
        # what OpenSSH says about the draft.
        self.file_banner = Adw.Banner()
        self.file_banner.connect("button-clicked", self._on_file_banner_button)
        self._file_banner_action: Optional[Callable[[], None]] = None
        view.add_top_bar(self.file_banner)
        self.banner = Adw.Banner()
        # As text: it shows commands and OpenSSH's own words, which are
        # not markup and may well look like it.
        self.banner.set_use_markup(False)
        self.banner.connect("button-clicked", self._on_answer_banner_button)
        self._answer_banner_action: Optional[Callable[[], None]] = None
        view.add_top_bar(self.banner)
        view.add_top_bar(self._build_context_bar())

        self.switcher_bar = Adw.ViewSwitcherBar(stack=self.stack)
        view.add_bottom_bar(self.switcher_bar)

        self.sidebar = BlockSidebar(self)
        self.target_entry = self.sidebar.target_entry
        #: The list of blocks: what a host puts in a sidebar of its own.
        self.sidebar_column = self.sidebar
        if self.embedded:
            view.set_content(self.stack)
            self.split = None
        else:
            self.split = Adw.OverlaySplitView()
            self.split.set_sidebar(self.sidebar)
            self.split.set_content(self.stack)
            self.split.set_min_sidebar_width(220)
            self.split.set_max_sidebar_width(300)
            self.split.bind_property(
                "show-sidebar", self.sidebar_button, "active",
                GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE,
            )
            view.set_content(self.split)

        self.set_size_request(*self._host_min_size())
        narrow = Adw.Breakpoint.new(
            Adw.BreakpointCondition.parse(f"max-width: {_NARROW_BELOW}px")
        )
        narrow.add_setter(self.switcher, "visible", False)
        narrow.add_setter(self.switcher_bar, "reveal", True)
        if self.split is not None:
            narrow.add_setter(self.split, "collapsed", True)
        self.add_breakpoint(narrow)

    def _build_context_bar(self) -> Gtk.Widget:
        """The block on screen, the host being checked, and what can be
        done with that host."""
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.add_css_class("advcfg-context")
        self.block_title = Gtk.Label(xalign=0)
        self.block_title.add_css_class("title-4")
        self.block_title.add_css_class("monospace")
        self.block_title.set_ellipsize(Pango.EllipsizeMode.END)
        bar.append(self.block_title)
        self.file_pill = pill(style="muted")
        bar.append(self.file_pill)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        bar.append(spacer)
        self.check_entry = Gtk.Entry()
        self.check_entry.set_width_chars(16)
        self.check_entry.set_valign(Gtk.Align.CENTER)
        self.check_entry.set_icon_from_icon_name(
            Gtk.EntryIconPosition.PRIMARY, "network-server-symbolic"
        )
        self.check_entry.set_tooltip_text(_(
            "The host OpenSSH is asked about. Empty: the host the selected block names."
        ))
        self.check_entry.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Host to check")]
        )
        self.check_entry.connect("changed", self._on_check_typed)
        self.check_entry.connect("icon-release", self._on_check_icon)
        bar.append(self.check_entry)
        self.test_status = pill(style="muted")
        bar.append(self.test_status)
        self.copy_command_button = Gtk.Button(
            icon_name="edit-copy-symbolic", tooltip_text=_("Copy SSH Command")
        )
        self.copy_command_button.set_valign(Gtk.Align.CENTER)
        self.copy_command_button.connect("clicked", lambda *_a: self.copy_command())
        bar.append(self.copy_command_button)
        self.test_button = Gtk.Button()
        self.test_button.set_child(Adw.ButtonContent(
            icon_name="media-playback-start-symbolic", label=_("Test Connection"),
        ))
        self.test_button.set_valign(Gtk.Align.CENTER)
        self.test_button.connect("clicked", lambda *_a: self.test_connection())
        bar.append(self.test_button)
        return bar

    # ------------------------------------------------------------------
    # convenience for callers and tests
    # ------------------------------------------------------------------

    @property
    def draft(self):
        return self.editor.draft

    @property
    def raw_pending(self) -> bool:
        return self.raw.pending

    @property
    def dirty(self) -> bool:
        return self.editor.dirty or self.raw.pending

    def target_text(self) -> str:
        """The host OpenSSH is asked about: the one typed above the views,
        else the one the selected block names. Empty: none."""
        return self._typed_host() or first_host_name(
            self.editor.find_block(self.selected_uid)
        )

    def _typed_host(self) -> str:
        """The host asked about whichever block is selected, if there is
        one: typed above the views, or one a caller asked to keep in
        question (``go_to`` with *pin*, a source followed from Effective).
        Empty otherwise: the selected block then says."""
        return self._pinned_host

    def show_page(self, name: str) -> None:
        """Make one of the three views the one on screen (Effective only
        while it is offered: see :meth:`_sync_effective_page`)."""
        if name == "effective" and not self.stack.get_page(self.effective).get_visible():
            return
        if name in PAGES and self.stack.get_visible_child_name() != name:
            self.stack.set_visible_child_name(name)

    def _sync_effective_page(self) -> None:
        """Offer Effective only while there is a host to ask OpenSSH about.

        ``ssh -G`` answers for one host. A block that names none (``Host *``,
        a pattern, a Match rule, the global options) has nothing to show
        there until a host is typed, so the page is hidden for it. Effective
        on screen when it goes is left for Structured, and comes back when
        the page does."""
        page = self.stack.get_page(self.effective)
        offered = bool(self.target_text())
        if page.get_visible() == offered:
            return
        if not offered and self.stack.get_visible_child_name() == "effective":
            self.stack.set_visible_child_name("structured")
            self._effective_left = True     # after the switch, which clears it
        page.set_visible(offered)
        if offered and self._effective_left:
            self._effective_left = False
            self.stack.set_visible_child_name("effective")

    # -- the search field ------------------------------------------------

    def search_changed(self) -> None:
        """The sidebar's search: keep the blocks found, and select the one
        the search is most likely about (the one that names the host typed,
        else the first found)."""
        self.sidebar.refilter()
        words = self.sidebar.search_words()
        typed = self.target_entry.get_text().strip()
        # The exact name of a host the configuration names is the host
        # checked: the second name of "Host a b" shows what OpenSSH
        # resolves for b, not for a. Anything else typed is a search only,
        # and a host the search put there goes with it.
        named = typed if typed in plain_host_names(self.editor.blocks) else ""
        if named or self._check_from_search:
            self._check_from_search = bool(named)
            if named != self._pinned_host:
                self._set_typed(named)
                self._target_changed()
        if not words:
            return
        found = [block for block in self.editor.blocks if block_matches(block, words)]
        if not found:
            return
        best = next(
            (block for block in found
             if block.kind == HOST and typed in block.patterns.split()),
            found[0],
        )
        if self.editor.find_block(self.selected_uid) in found and best is found[0]:
            return                      # what is selected is among the found
        self.select_block(best.uid)

    # -- the host being checked -------------------------------------------

    def _set_typed(self, text: str) -> None:
        """Put a host to check above the views, as the window's own doing."""
        self._check_syncing = True
        try:
            if self.check_entry.get_text() != text:
                self.check_entry.set_text(text)
        finally:
            self._check_syncing = False
        self._pinned_host = text.strip()
        self._pin_typed = False
        self._sync_context()

    def _on_check_typed(self, entry) -> None:
        self._sync_check_icon()
        if self._check_syncing:
            return
        host = entry.get_text().strip()
        self._check_from_search = False     # typed here: the user's own
        if host == self._pinned_host:
            return
        self._pinned_host = host
        self._pin_typed = bool(host)
        self._sync_context()
        self._target_changed()

    def _sync_check_icon(self) -> None:
        has_text = bool(self.check_entry.get_text())
        self.check_entry.set_icon_from_icon_name(
            Gtk.EntryIconPosition.SECONDARY, "edit-clear-symbolic" if has_text else None
        )

    def _on_check_icon(self, entry, position) -> None:
        if position == Gtk.EntryIconPosition.SECONDARY:
            entry.set_text("")

    # ------------------------------------------------------------------
    # background work
    # ------------------------------------------------------------------

    def _run(self, work: Callable[[], object],
             done: Callable[[object, Optional[BaseException]], None]) -> None:
        def worker():
            try:
                result, error = work(), None
            except BaseException as exc:  # noqa: BLE001 - reported to the UI
                result, error = None, exc
            GLib.idle_add(lambda: (None if self._closing else done(result, error), False)[1])

        threading.Thread(target=worker, daemon=True, name="sshpilot-advanced-config").start()

    # ------------------------------------------------------------------
    # loading and saving (through the daemon)
    # ------------------------------------------------------------------

    def _load(self, file: Optional[str] = None) -> None:
        """Load a file of the configuration: the one on screen again, or
        *file* as the daemon named it ("" is the root)."""
        wanted = self._file if file is None else file
        self.save_button.set_sensitive(False)
        self._run(
            (lambda: self._client.get_ssh_config_text(wanted)) if wanted
            else self._client.get_ssh_config_text,
            self._on_loaded,
        )

    def _on_loaded(self, result, error) -> None:
        if error is not None:
            logger.debug("Advanced Configuration could not load", exc_info=error)
            self._show_file_banner(_("The SSH configuration could not be read."))
            self._pending_target = None
            self._sync_files()
            return
        same_file = self._loaded and str(getattr(result, "file", "") or "") == self._file
        self._file = str(getattr(result, "file", "") or "")
        self._files = tuple(getattr(result, "files", ()) or ())
        self._revision = result.revision
        self._display_name = result.display_name
        self._writable = bool(result.writable)
        self.editor.load(result.text)
        self._forget_answers()      # another text, whatever its serial says
        self._loaded_lines = self.editor.lines
        self._loaded = True
        self._allowed_exec.clear()
        self.raw.discard()
        if not same_file:
            self._selected_index = self._first_index()
        self.selected_uid = None            # the blocks are new: chosen again
        self._show_file_banner(
            "" if self._writable
            else _("This SSH configuration is read-only: changes cannot be saved.")
        )
        self._show_draft(evaluate_now=True)
        self._check_at_open()
        self._apply_pending_target()
        self._apply_pending_block()

    def _first_index(self) -> int:
        """The block selected when a file opens: its first Host block (the
        global options say little on their own), else its first block."""
        for index, block in enumerate(self.editor.blocks):
            if block.kind != GLOBAL:
                return index
        return 0

    def _check_at_open(self) -> None:
        """Ask OpenSSH about the file as it is, before any host is looked
        at: a configuration OpenSSH already rejects is said so at once."""
        serial = self.editor.serial
        try:
            request = EvaluateSshConfigDraftRequest(
                text=self.editor.text, host=_CHECK_HOST, file=self._file
            )
        except (TypeError, ValueError):
            return

        def done(result, error) -> None:
            if error is not None or result is None:
                return
            if result.errors and serial == self.editor.serial and self._evaluation is None:
                self._show_answer_banner(messages.error_text(result.errors[0]))

        self._run(lambda: self._ask_once(request), done)

    def save(self) -> None:
        """Save what is on screen, after asking OpenSSH whether it accepts it."""
        if self._saving or not self.dirty or not self._writable:
            return
        if self.raw.pending:
            # The other views wait while text is being typed, so the text
            # is the newer of the two: it is what gets saved.
            self.apply_raw()
            if not self.dirty:
                return
        self._saving = True
        self._update_chrome()
        # The text, the file it is of and the revision it was loaded at are
        # taken together, now: the save is of this document, whatever is on
        # screen by the time OpenSSH has been asked.
        text, file, revision = self.editor.text, self._file, self._revision
        try:
            # No agreed Match exec command goes with this question: it is
            # about a made-up host, and an agreement is for the host it was
            # given for. A draft that needs one run is saved unchecked.
            request = EvaluateSshConfigDraftRequest(text=text, host=_CHECK_HOST, file=file)
        except (TypeError, ValueError):
            self._write(text, file, revision)
            return

        def checked(result, error) -> None:
            if error is None and result is not None and result.errors:
                self._confirm_rejected(text, file, revision, result)
            else:
                self._write(text, file, revision)

        self._run(lambda: self._ask_once(request), checked)

    def _confirm_rejected(
        self, text: str, file: str, revision: str, evaluation: SshConfigDraftEvaluation
    ) -> None:
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

        def answered(_dialog, response: str) -> None:
            self._save_alert = None
            if response == "save":
                self._write(text, file, revision)
            else:
                self._save_over()

        alert.connect("response", answered)
        self._save_alert = alert
        alert.present(self)

    def _write(self, text: str, file: str, revision: str) -> None:
        if file != self._file or revision != self._revision:
            # Not the document on screen any more: its text is not written
            # over another file, nor at another revision.
            self._save_over()
            return
        request = SaveSshConfigTextRequest(text=text, expected_revision=revision, file=file)
        self._run(
            lambda: self._client.save_ssh_config_text(request),
            lambda result, error: self._on_saved_result(text, result, error),
        )

    def _save_over(self) -> None:
        """A save has ended, written or not: what waited for it goes on."""
        self._saving = False
        self._update_chrome()
        self._sync_files()
        self._apply_pending_target()

    def _on_saved_result(self, text: str, result, error) -> None:
        self._saving = False
        if error is not None:
            self._pending_target = None
            code = error.code if isinstance(error, SshPilotError) else None
            if code is ErrorCode.STALE_EDITOR:
                self._show_file_banner(
                    _("The SSH configuration changed on disk since it was opened."),
                    # Through the menu's Reload: it asks before dropping the
                    # draft this save was meant to keep.
                    _("Reload"), self._on_reload,
                )
            elif code is ErrorCode.SSH_CONFIG_READ_ONLY:
                self._writable = False
                self._show_file_banner(
                    _("This SSH configuration is read-only: changes cannot be saved.")
                )
            else:
                logger.debug("Advanced Configuration could not save", exc_info=error)
                self._show_file_banner(
                    _("The SSH configuration could not be saved."), _("Dismiss"), lambda: None,
                )
            self._update_chrome()
            return
        self._revision = result.revision
        self.editor.mark_saved(text)
        self._show_file_banner("")          # an earlier failure is history
        self._toasts.add_toast(Adw.Toast.new(_("SSH configuration saved")))
        self._update_chrome()
        self._sync_files()
        self._apply_pending_target()         # a place asked for during the save
        self._schedule_evaluation(now=True)  # Match exec commands are saved now
        for listener in [self._on_saved, *self._saved_listeners]:
            if not callable(listener):
                continue
            try:
                listener()
            except Exception:
                logger.debug("on_saved failed", exc_info=True)

    def _on_reload(self, *_args) -> None:
        if self._saving:
            return                  # the save is of the text on screen
        if self.dirty:
            self._confirm_discard(lambda: self._load())
        else:
            self._load()

    # ------------------------------------------------------------------
    # the files of the configuration
    # ------------------------------------------------------------------

    def _sync_files(self) -> None:
        """Show which file is on screen, in the sidebar and the Raw view."""
        self.sidebar.sync(self.editor.blocks, self._files, self._file, self._display_name)
        self.sidebar.select(self.selected_uid)
        self.raw.set_files(self._files, self._file)
        set_pill(self.file_pill, self._display_name, "muted",
                 _("The config file this block is in"))

    def open_file(self, file: str) -> None:
        """Put another file of the configuration on screen: *file* as the
        daemon named it ("" is the root). Unsaved changes are asked about
        first; keeping them leaves the file on screen where it is."""
        if file == self._file:
            return
        if self._saving:
            # Not while a save is under way: it is of the file on screen. A
            # place asked for (go_to) is gone to when the save is over.
            self._sync_files()
            return

        def stay() -> None:
            self._pending_target = None
            self._sync_files()

        if self.dirty:
            self._confirm_discard(lambda: self._load(file), stay)
        else:
            self._load(file)

    def _file_named(self, source: str) -> str:
        """The daemon's name for *source*, a path as a connection reports
        its source file. The root, and a path the configuration does not
        read, are the root (""): there is nowhere else to look."""
        for ref in self._files[1:]:
            if ref.file == source:
                return ref.file
        return ""

    # ------------------------------------------------------------------
    # going to a place (the entry point for the rest of the app)
    # ------------------------------------------------------------------

    def add_saved_listener(self, callback: Callable[[], None]) -> None:
        """Run *callback* after each save: for a caller that shows values
        the save may have changed."""
        self._saved_listeners.append(callback)

    def go_to(
        self,
        *,
        host: str = "",
        line: int = 0,
        option: str = "",
        file: str = "",
        pin: bool = False,
    ) -> None:
        """Show a place of the configuration. See
        ``docs/architecture/advanced-configuration-navigation.md``.

        May be called before the window has loaded: it goes there once the
        file is on screen.
        """
        self._pending_target = dict(
            host=str(host or "").strip(), line=int(line or 0),
            option=str(option or "").strip(), file=str(file or ""), pin=bool(pin),
        )
        if self._loaded:
            self._apply_pending_target()

    def start_block(self, patterns: str, options=()) -> None:
        """Begin a new Host block with this Host line and these options.

        What the connection editor hands over when a pattern is typed where
        a connection's name goes: the block is added to the draft, selected
        and left unsaved, for the user to place and complete. *options* is
        ``(keyword, value)`` pairs. May be called before the window has
        loaded: the block is added once the file is on screen.
        """
        self._pending_block = (
            str(patterns or "").strip(),
            tuple((str(key), str(value)) for key, value in options),
        )
        if self._loaded:
            self._apply_pending_block()

    def _apply_pending_block(self) -> None:
        pending = getattr(self, "_pending_block", None)
        if pending is None:
            return
        self._pending_block = None
        patterns, options = pending
        block = self.editor.add_block(HOST)
        if block is None:
            return
        self.editor.set_patterns(block.uid, patterns)
        # A new block starts with one empty option: the first one fills it.
        for index, (key, value) in enumerate(options):
            if index == 0 and block.options:
                self.editor.set_option(block.options[0].uid, "key", key)
                self.editor.set_option(block.options[0].uid, "value", value)
            else:
                self.editor.add_option(block.uid, key, value)
        self.selected_uid = block.uid
        self.show_page("structured")
        self._after_structure()
        GLib.idle_add(lambda: (self.structured.focus_patterns(), False)[1])

    def _apply_pending_target(self) -> None:
        target = self._pending_target
        if target is None:
            return
        wanted = self._file_named(target["file"])
        if wanted != self._file:
            self.open_file(wanted)          # comes back here once it has loaded
            return
        self._pending_target = None
        host, line, option = target["host"], target["line"], target["option"]
        if target["pin"] and host:
            self._set_typed(host)
            self._target_changed()
        uid = self.editor.owner_of_line(line, self._loaded_lines) if line else None
        if uid is None and host:
            uid = self._block_declaring(host)
        if uid is None:
            if line:
                # No block owns the line (a comment, a line OpenSSH cannot
                # place): it is shown as text.
                self.show_page("raw")
                self.raw.show_line(line)
            elif host:
                self._toasts.add_toast(Adw.Toast.new(
                    _("No Host block names “{host}” in this file.").format(host=host)
                ))
            return
        owner, _found = self.editor.find_option(uid)
        block = owner if owner is not None else self.editor.find_block(uid)
        if block is None:
            return
        if option:
            wanted_key = option.lower()
            uid = next(
                (item.uid for item in block.options if item.keyword == wanted_key),
                uid,
            )
        if self.stack.get_visible_child_name() == "raw":
            self.show_page("structured")
        self.select_block(block.uid)

        def show() -> bool:
            # Once laid out: a window just opened has no sizes yet.
            if self.selected_uid == block.uid:
                self.structured.reveal(uid)
            return False

        GLib.timeout_add(150, show)

    def _block_declaring(self, host: str) -> Optional[int]:
        for block in self.editor.blocks:
            if block.kind == HOST and not block.disabled and host in declared_hosts(
                block.patterns.split()
            ):
                return block.uid
        return None

    def _on_copy(self, *_args) -> None:
        # What is on screen: text typed in the Raw view and not applied is
        # the newer of the two. Copying leaves it as it is, unapplied.
        self.get_clipboard().set(self.raw.text() if self.raw.pending else self.editor.text)
        self._toasts.add_toast(Adw.Toast.new(_("Configuration copied")))

    def _confirm_discard(
        self, proceed: Callable[[], None], stay: Optional[Callable[[], None]] = None
    ) -> None:
        alert = Adw.AlertDialog(
            heading=_("Discard unsaved changes?"),
            body=_("The changes made here have not been saved to the SSH configuration."),
        )
        alert.add_response("cancel", _("Cancel"))
        alert.add_response("discard", _("Discard"))
        alert.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
        alert.set_default_response("cancel")
        alert.set_close_response("cancel")
        alert.connect(
            "response",
            lambda _d, r: proceed() if r == "discard" else (stay() if stay else None),
        )
        alert.present(self)

    def add_closed_listener(self, callback: Callable[[], None]) -> None:
        """Run *callback* when the window closes for good. (Not GTK's
        "destroy": that waits for the last reference to the window, and
        whoever wants to know that it closed is holding one.)"""
        self._closed_listeners.append(callback)

    def _tell_closed(self) -> None:
        first = not self._told_closed
        self._closing = self._told_closed = True
        if first:
            for listener in self._closed_listeners:
                try:
                    listener()
                except Exception:
                    logger.debug("closed listener failed", exc_info=True)

    def _on_close_request(self, *_args) -> bool:
        """Whether closing has to wait: True while unsaved changes are
        asked about. Discarding closes through :meth:`_host_close`."""
        if self._closing or not self.dirty:
            self._tell_closed()
            return False

        def close():
            self._closing = True
            self._host_close()

        self._confirm_discard(close, self._host_close_refused)
        return True

    def _host_close_refused(self) -> None:
        """The user kept the unsaved changes: nothing closes."""

    def _host_dirty_changed(self) -> None:
        """The draft became unsaved, or saved: for a host that shows it."""

    # -- banners ------------------------------------------------------------

    def _show_file_banner(
        self, text: str, button: str = "", action: Optional[Callable[[], None]] = None
    ) -> None:
        self.file_banner.set_title(text)
        self.file_banner.set_button_label(button or None)
        self._file_banner_action = action
        self.file_banner.set_revealed(bool(text))

    def _on_file_banner_button(self, *_args) -> None:
        action, self._file_banner_action = self._file_banner_action, None
        self.file_banner.set_revealed(False)
        if callable(action):
            action()

    def _show_answer_banner(
        self, text: str, button: str = "", action: Optional[Callable[[], None]] = None
    ) -> None:
        self.banner.set_title(text)
        self.banner.set_button_label(button or None)
        self._answer_banner_action = action
        self.banner.set_revealed(bool(text))

    def _on_answer_banner_button(self, *_args) -> None:
        action = self._answer_banner_action
        if callable(action):
            action()

    # ------------------------------------------------------------------
    # showing the draft
    # ------------------------------------------------------------------

    def _show_draft(self, *, evaluate_now: bool = False) -> None:
        """Put the editor's draft on screen (in place) and ask about it."""
        blocks = self.editor.blocks
        if self.editor.find_block(self.selected_uid) is None:
            # The block selected is gone (deleted, or the draft read anew):
            # the one at its place is selected, so there always is one.
            self.selected_uid = None
            if blocks:
                index = min(max(self._selected_index, 0), len(blocks) - 1)
                self.selected_uid = blocks[index].uid
        block = self.editor.find_block(self.selected_uid)
        if block is not None:
            self._selected_index = blocks.index(block)
        self._sync_files()
        self.structured.show(block)
        self.raw.set_text(self.editor.text)
        self._mark_selection_in_raw(scroll=False)
        self._sync_context()
        self._update_chrome()
        self._show_answer()                     # the last answer, until the new one
        self._schedule_evaluation(now=evaluate_now)

    def _update_chrome(self) -> None:
        busy = self._saving
        self.undo_button.set_sensitive(self.editor.can_undo and not busy)
        self.redo_button.set_sensitive(self.editor.can_redo and not busy)
        self.save_button.set_sensitive(self.dirty and self._writable and not busy)
        self._host_dirty_changed()
        # One editor at a time: while text typed in the Raw view waits to
        # be applied, the blocks are not edited beside it (the two would
        # overwrite each other).
        pending = self.raw.pending
        self.structured.set_sensitive(not pending)
        self.sidebar.list.set_sensitive(not pending)
        self.sidebar.new_button.set_sensitive(not pending)
        self._sync_test_button()

    def _sync_context(self) -> None:
        """The bar above the views: the block, and the host it is checked
        with (typed, or the block's own name in grey)."""
        block = self.editor.find_block(self.selected_uid)
        title = messages.block_list_title(block.kind, block.patterns) if block else ""
        if self.block_title.get_label() != title:
            self.block_title.set_label(title)
        own = first_host_name(block)
        self.check_entry.set_placeholder_text(own or _("Host to check"))
        host = split_target(self.target_text())[1]
        self.copy_command_button.set_sensitive(bool(host))
        self._sync_test_button()

    def _sync_test_button(self) -> None:
        host = split_target(self.target_text())[1]
        evaluation = self._evaluation
        if self._testing:
            tip = _("Testing…")
        elif not host:
            tip = _("Choose a block that names a host, or type one to check")
        elif evaluation is not None and evaluation.pending_exec:
            tip = _("Allow the Match exec command first: the test would run it")
        else:
            tip = _("Connect to {host} with the configuration shown here, saved or not").format(
                host=host
            )
        self.test_button.set_sensitive(self._test_ready(host))
        self.test_button.set_tooltip_text(tip)

    def _target_changed(self, *, idle: bool = False) -> None:
        # A command can depend on the host (%h, %r): agreement to run it
        # was for the host in question at the time.
        self._allowed_exec.clear()
        set_pill(self.test_status, "", "muted")
        self._schedule_evaluation(idle=idle, now=not idle and self._answer_ready())

    def _toast_refusal(self, refusal) -> None:
        if refusal is Refusal.GLOBAL_ORDER:
            self._toasts.add_toast(Adw.Toast.new(_(
                "Global options must stay above every Host and Match block: "
                "OpenSSH reads an option below a Host line as that host's."
            )))

    # ------------------------------------------------------------------
    # edits (called by the views and by keys)
    # ------------------------------------------------------------------

    def _after_structure(self, result=True) -> bool:
        """An edit that changed the draft's shape: redraw in place."""
        if isinstance(result, Refusal):
            self._toast_refusal(result)
            return False
        if not result:
            return False
        self._cancel_history_timer()
        self._show_draft(evaluate_now=True)
        return True

    def _after_typing(self, changed: bool) -> None:
        if not changed:
            return
        self.raw.set_text(self.editor.text)
        self._mark_selection_in_raw(scroll=False)
        # The sidebar names blocks by their patterns, user and address.
        self.sidebar.sync(self.editor.blocks, self._files, self._file, self._display_name)
        self._sync_context()
        self._schedule_history()
        self._update_chrome()
        self._schedule_evaluation()

    def add_block(self, kind: str) -> None:
        block = self.editor.add_block(kind)
        if block is None:
            return
        self.selected_uid = block.uid
        self.show_page("structured")
        self._after_structure()
        GLib.idle_add(lambda: (self.structured.focus_patterns(), False)[1])

    def move_block(self, uid: int, index: int) -> bool:
        return self._after_structure(self.editor.move_block(uid, index))

    def move_block_by(self, uid: int, delta: int) -> bool:
        return self._after_structure(self.editor.move_block_by(uid, delta))

    def move_selected(self, delta: int) -> bool:
        """Alt+Up / Alt+Down: move the selected block one place."""
        if self.selected_uid is not None and not self.raw.pending:
            self.move_block_by(self.selected_uid, delta)
        return True

    def drop_block(self, uid: int, onto_uid: int, before: bool) -> bool:
        """A block dropped beside another in the sidebar."""
        blocks = self.editor.blocks
        source, target = self.editor.find_block(uid), self.editor.find_block(onto_uid)
        if source is None or target is None:
            return False
        index = blocks.index(target) + (0 if before else 1)
        if blocks.index(source) < index:
            index -= 1
        return self.move_block(uid, index)

    def toggle_block(self, uid: int) -> None:
        if not self._after_structure(self.editor.toggle_block(uid)):
            self._show_draft()              # refused: the switch goes back

    def duplicate_block(self, uid: int) -> None:
        copy = self.editor.duplicate_block(uid)
        if copy is not None:
            self.selected_uid = copy.uid
            self._after_structure()

    def delete_block(self, uid: int) -> None:
        self._after_structure(self.editor.delete_block(uid))

    def add_option(self, block_uid: int, key: str = "", value: str = "", *,
                   keep: bool = False) -> None:
        """Add a line to a block. *keep*: added with Add Option, a plain
        line whatever its keyword."""
        option = self.editor.add_option(block_uid, key, value)
        if option is None:
            return
        if keep:
            self.structured.keep(option.uid)
        self._after_structure()
        GLib.idle_add(lambda: (self.structured.focus_option(option.uid, key=not key), False)[1])

    def delete_option(self, uid: int) -> None:
        self._after_structure(self.editor.delete_option(uid))

    def patterns_typed(self, uid: int, text: str) -> None:
        self._after_typing(self.editor.set_patterns(uid, text))

    def note_typed(self, uid: int, text: str) -> None:
        self._after_typing(self.editor.set_note(uid, text))

    def option_typed(self, uid: int, field: str, text: str) -> None:
        self._after_typing(self.editor.set_option(uid, field, text))

    def choice_made(self, block_uid: Optional[int], keyword: str, uid: Optional[int],
                    value: Optional[str]) -> None:
        """A yes/no field set to yes, no, or not set (None: the line goes)."""
        if self.editor.find_block(block_uid) is None:
            return
        if value is None:
            if uid is not None:
                self._after_structure(self.editor.delete_option(uid))
            return
        if uid is None:
            self.editor.add_option(block_uid, fields.SPELLING[keyword], value)
            self._after_structure()
            return
        if self.editor.set_option(uid, "value", value):
            self.editor.commit_typing()         # one choice, one undo step
            self._after_structure()

    # -- key files ---------------------------------------------------------------

    def browse_identity(self, uid: int) -> None:
        """Choose the key file of an IdentityFile line."""
        dialog = Gtk.FileDialog(title=_("Choose a Key File"))
        folder = os.path.expanduser("~/.ssh")
        if os.path.isdir(folder):
            dialog.set_initial_folder(Gio.File.new_for_path(folder))
        root = self.get_root() if hasattr(self, "get_root") else None

        def chosen(source, result) -> None:
            try:
                file = source.open_finish(result)
            except GLib.Error:
                return                      # dismissed
            path = file.get_path() if file is not None else None
            if not path:
                return
            if self.editor.set_option(uid, "value", fields.home_relative(path)):
                self.editor.commit_typing()
                self._after_structure()

        dialog.open(root if isinstance(root, Gtk.Window) else None, None, chosen)

    def fix_key(self, path: str) -> None:
        if fields.fix_key_permissions(path):
            text = _("Only you can read {path} now.").format(path=fields.home_relative(path))
        else:
            text = _("The permissions of {path} could not be changed.").format(
                path=fields.home_relative(path)
            )
        self._toasts.add_toast(Adw.Toast.new(text))

    # -- the host being checked: copy, test, the route -------------------------

    def copy_command(self) -> None:
        user, host = split_target(self.target_text())
        if not host:
            return
        command = "ssh " + (f"{user}@" if user else "") + host
        self.get_clipboard().set(command)
        self._toasts.add_toast(Adw.Toast.new(_("Copied “{command}”").format(command=command)))

    def test_connection(self) -> None:
        """Connect to the host being checked, as the configuration shown
        here would have it (saved or not), through the app's own launch
        path: the daemon registers a target that reads this draft, and a
        one-shot ``true`` runs on it like any other remote command. Prompts
        (password, passphrase, one-time code, host key) appear as for any
        connection, over this window."""
        user, host = split_target(self.target_text())
        if not self._test_ready(host):
            return
        from .api.models.broadcast import (
            BroadcastCommandRequest,
            BroadcastExecutionPolicy,
            HostCommandState,
        )

        try:
            request = EvaluateSshConfigDraftRequest(
                # Text typed in the Raw view and not applied is the newer.
                text=self.raw.text() if self.raw.pending else self.editor.text,
                host=host, user=user,
                allowed_exec=tuple(sorted(self._allowed_exec)), file=self._file,
            )
        except (TypeError, ValueError):
            return
        self._testing = True
        set_pill(self.test_status, _("Testing…"), "accent")
        self._sync_test_button()
        client = self._client
        presenter = self._test_presenter()

        def bind(operation_id) -> bool:
            if presenter is not None and not self._closing:
                try:
                    presenter.set_session(SessionId(str(operation_id)))
                except Exception:
                    logger.debug("test prompt presenter bind failed", exc_info=True)
            return False

        def work():
            details = client.open_draft_test_connection(request)
            summary = client.start_broadcast_command(BroadcastCommandRequest(
                (details.id,), "true",
                BroadcastExecutionPolicy(
                    concurrency_limit=1, timeout_seconds=float(_TEST_TIMEOUT),
                ),
            ))
            operation_id = summary.operation.operation_id
            GLib.idle_add(bind, operation_id)
            finished = {"succeeded", "failed", "cancelled"}
            # Long enough to type a password or a code into a prompt.
            deadline = time.monotonic() + _TEST_TIMEOUT + 15
            while summary.operation.state.value not in finished:
                if time.monotonic() >= deadline or self._closing:
                    client.cancel_broadcast_command(operation_id)
                    return False, ""
                time.sleep(0.1)
                summary = client.get_broadcast_command(operation_id)
            target = summary.targets[0] if summary.targets else None
            if target is not None and target.state is HostCommandState.SUCCEEDED \
                    and target.exit_code == 0:
                return True, ""
            # OpenSSH's own last word, as it said it.
            lines = [line.strip() for line in (target.stderr if target else "").splitlines()
                     if line.strip()]
            return False, lines[-1] if lines else ""

        def done(result, error) -> None:
            self._testing = False
            if error is not None:
                logger.debug("connection test failed", exc_info=error)
                result = (False, "")
            ok, detail = result
            if ok:
                set_pill(self.test_status, messages.test_result_text(True), "success")
            else:
                text = messages.test_result_text(False, detail)
                set_pill(self.test_status, messages.test_result_text(False), "error", text)
                self._toasts.add_toast(Adw.Toast.new(text))
            self._sync_test_button()

        self._run(work, done)

    def _test_ready(self, host: str) -> bool:
        evaluation = self._evaluation
        return bool(
            host and not self._testing
            and callable(getattr(self._client, "open_draft_test_connection", None))
            and not (evaluation is not None and evaluation.pending_exec)
        )

    def _test_presenter(self):
        """The prompts of a test, over this window: made once, bound to each
        test's operation when it starts."""
        presenter = getattr(self, "_test_prompts", None)
        if presenter is not None:
            return presenter
        bridge = getattr(self._parent_window, "client_bridge", None)
        if bridge is None:
            return None
        try:
            from .daemon_interaction_dialogs import DaemonInteractionDialogs

            presenter = DaemonInteractionDialogs(self._client, bridge, self)
        except Exception:
            logger.debug("test prompt presenter unavailable", exc_info=True)
            return None
        self._test_prompts = presenter
        return presenter

    # -- the Raw view -------------------------------------------------------

    def raw_text_changed(self) -> None:
        self._update_chrome()

    def _selected_lines(self, lines: Optional[LineMap] = None) -> tuple:
        """The first and last line the selected block has in the text."""
        block = self.editor.find_block(self.selected_uid)
        if block is None:
            return (0, 0)
        lines = lines or self.editor.lines
        owned = [lines.headers.get(block.uid, 0)] + [
            lines.options.get(option.uid, 0) for option in block.options
        ]
        owned = [line for line in owned if line]
        return (min(owned), max(owned)) if owned else (0, 0)

    def _mark_selection_in_raw(self, *, scroll: bool = True) -> None:
        self.raw.mark_block(
            *self._selected_lines(), scroll=scroll and not self._selecting_from_raw
        )

    def raw_cursor_moved(self, line: int) -> None:
        """The cursor is on *line* of the Raw view: the block that line
        belongs to is the selected one."""
        uid = self.editor.owner_of_line(line)
        if uid is None:
            return
        owner, _option = self.editor.find_option(uid)
        block_uid = owner.uid if owner is not None else uid
        if block_uid == self.selected_uid:
            return
        self._selecting_from_raw = True     # the text is where the eye is
        try:
            self.select_block(block_uid)
        finally:
            self._selecting_from_raw = False

    def apply_raw(self) -> None:
        text = self.raw.text()
        self.raw.discard()
        self.editor.replace_text(text)
        self._after_structure()

    def discard_raw(self) -> None:
        self.raw.discard()
        self._update_chrome()

    def _on_page_shown(self, *_args) -> None:
        name = self.stack.get_visible_child_name()
        # Another page picked while Effective was hidden is where to stay.
        self._effective_left = False
        self.effective.set_shown(name == "effective")
        self.raw.set_shown(name == "raw")
        if name == "raw":
            self._mark_selection_in_raw()

    # -- history -------------------------------------------------------------

    def _cancel_history_timer(self) -> None:
        if self._history_source:
            GLib.source_remove(self._history_source)
            self._history_source = 0

    def _schedule_history(self) -> None:
        """A snapshot once typing pauses, so undo steps over whole edits."""
        self._cancel_history_timer()

        def fire():
            self._history_source = 0
            self.editor.commit_typing()
            self._update_chrome()
            return False

        self._history_source = GLib.timeout_add(_HISTORY_DELAY_MS, fire)

    def _history_key(self, step: Callable[[], None]) -> bool:
        """Ctrl+Z and its kin. False leaves the key to whoever has it: the
        Raw view's text, which has an undo of its own."""
        if self.raw.owns_focus(self.get_focus()):
            return False
        step()
        return True

    def undo(self) -> None:
        self._cancel_history_timer()
        if self.editor.undo():
            self._show_draft(evaluate_now=True)

    def redo(self) -> None:
        self._cancel_history_timer()
        if self.editor.redo():
            self._show_draft(evaluate_now=True)

    # ------------------------------------------------------------------
    # selection
    # ------------------------------------------------------------------

    def block_picked(self, uid: Optional[int]) -> None:
        """A block clicked (or reached with the keys) in the sidebar.

        Picking a block means "show me this one": a host the window put in
        question on its own (a source followed from Effective, a host name
        searched for, ``go_to`` with *pin*) gives way to the block's own,
        so Effective follows the selection. A host the user typed in the
        host-to-check field stays: the field shows it, and clearing it
        hands the question back to the selection."""
        self.select_block(uid)
        if self._pinned_host and not self._pin_typed:
            self._check_from_search = False
            self._set_typed("")
            self._target_changed(idle=not self._answer_ready())

    def select_block(self, uid: Optional[int]) -> None:
        """Select a block, from the sidebar or the Raw view: the one shown,
        the one Alt+arrows move, and, unless a host is typed above the
        views, the one that says which host OpenSSH is asked about."""
        if uid == self.selected_uid:
            return
        before = self._question()
        block = self.editor.find_block(uid)
        self.selected_uid = block.uid if block is not None else None
        if block is not None:
            self._selected_index = self.editor.blocks.index(block)
        self.sidebar.select(self.selected_uid)
        self.structured.show(block)
        self._mark_selection_in_raw()
        self._sync_context()
        if self._question() != before:
            # An answer had already is shown with the selection, in the same
            # frame. One that must be asked for is asked from the idle
            # loop, so the selection is painted first.
            self._target_changed(idle=not self._answer_ready())
        else:
            self._show_answer()             # the block's own lines and status

    def source_file(self, path: str) -> Optional[str]:
        """The file of the configuration a source names, as the daemon
        names it ("" is the root), or None when it is none of them (the
        system-wide file, OpenSSH's own): there is nothing to open then."""
        if not path:
            return None
        for ref in self._files[1:]:
            if ref.file == path:
                return ref.file
        evaluation = self._evaluation
        system = evaluation.system_config if evaluation is not None else ""
        if self._file and path != system:
            # Seen from an included file, the one other file a line can be
            # in is the root, which the daemon names by no path.
            return ""
        return None

    def reveal_source(self, path: str, line: int) -> None:
        """Show line *line* of another file of the configuration (a source
        in the Effective view). Unsaved changes are asked about first, as
        for any other file."""
        file = self.source_file(path)
        if file is None:
            return
        host = self.target_text()       # the host being read about stays
        self.go_to(file=file, line=line, host=host, pin=bool(host))

    def reveal_line(self, line: int) -> None:
        """Show the option or block the draft has at *line* (a source in
        the Effective view)."""
        uid = self.editor.owner_of_line(line, self._evaluated_lines)
        if uid is None:
            return
        owner, _option = self.editor.find_option(uid)
        # The line belongs to the answer on screen: the host it is about
        # stays the one checked, as a typed one, whichever block the line
        # turns out to be in.
        if not self._typed_host():
            self._set_typed(self.target_text())
        self.show_page("structured")
        self.select_block(owner.uid if owner is not None else uid)
        GLib.idle_add(lambda: (self.structured.reveal(uid), False)[1])

    # ------------------------------------------------------------------
    # asking OpenSSH
    # ------------------------------------------------------------------

    def _request(self) -> Optional[EvaluateSshConfigDraftRequest]:
        user, host = split_target(self.target_text())
        if not host or any(char.isspace() for char in host):
            return None
        try:
            return EvaluateSshConfigDraftRequest(
                text=self.editor.text, host=host, user=user,
                allowed_exec=tuple(sorted(self._allowed_exec)), file=self._file,
            )
        except (TypeError, ValueError):
            return None

    def _schedule_evaluation(self, *, now: bool = False, idle: bool = False) -> None:
        if self._evaluate_source:
            GLib.source_remove(self._evaluate_source)
            self._evaluate_source = 0
        if not self._loaded:
            return
        if now:
            self._evaluate()
        elif idle:
            self._evaluate_source = GLib.idle_add(self._evaluate)
        else:
            self._evaluate_source = GLib.timeout_add(_EVALUATE_DELAY_MS, self._evaluate)

    # -- answers already had ------------------------------------------------
    #
    # Asking OpenSSH takes a moment, and a selection must show at once what
    # applies. So an answer is kept for as long as the draft it is about
    # stands, and the hosts the configuration names are asked about ahead
    # of time, in the background: a selection then finds its answer
    # waiting. One question is put to the daemon at a time, since a newer
    # one from this window would make it drop the one before.

    def _answer_cache(self) -> dict:
        """The answers had for the draft as it is: ``(host, user) -> answer``.
        Another draft, file or set of agreed commands starts afresh."""
        key = (self.editor.serial, self._file, tuple(sorted(self._allowed_exec)))
        if key != self._answers_key:
            self._answers_key, self._answers = key, {}
        return self._answers

    def _forget_answers(self) -> None:
        self._answers_key, self._answers = None, {}

    def _ask(self, request, cache: dict, key: tuple):
        """Ask the daemon (from a worker thread) and keep an answer that is
        one. *cache* is the draft's own: a draft since replaced is not
        given this answer."""
        with self._ask_lock:
            answer = self._client.evaluate_ssh_config_draft(request)
        if answer.evaluated:
            cache[key] = answer
        return answer

    def _ask_once(self, request):
        """Ask the daemon a question whose answer is not kept (the check a
        save makes), in turn with the others."""
        with self._ask_lock:
            return self._client.evaluate_ssh_config_draft(request)

    def _schedule_prefetch(self) -> None:
        if self._prefetch_source:
            GLib.source_remove(self._prefetch_source)
        self._prefetch_source = GLib.timeout_add(_PREFETCH_DELAY_MS, self._prefetch)

    def _prefetch(self) -> bool:
        """Ask about every host the configuration names that has no answer
        yet, quietly. Stops at the first answer that is not one (a command
        waiting for consent, a rejected draft): every host would get it."""
        self._prefetch_source = 0
        if self._prefetching or not self._loaded or self._closing:
            return False
        cache = self._answer_cache()
        text, file = self.editor.text, self._file
        # Nothing agreed goes along: an agreement to run a Match exec command
        # was given for the host on screen, and these are other hosts (and
        # made-up names). A draft that needs one run stops this at once.
        allowed: tuple = ()
        wanted = list(plain_host_names(self.editor.blocks))
        for block in self.editor.blocks:
            if not block.disabled:
                wanted.extend(name for name in sample_hosts(block) if name not in wanted)
        names = [name for name in wanted[:_PREFETCH_HOSTS] if (name, None) not in cache]
        if not names:
            return False
        self._prefetching = True

        def work() -> None:
            try:
                for name in names:
                    if cache is not self._answers or self._closing:
                        return              # the draft moved on
                    try:
                        request = EvaluateSshConfigDraftRequest(
                            text=text, host=name, allowed_exec=allowed, file=file
                        )
                    except (TypeError, ValueError):
                        continue
                    if not self._ask(request, cache, (name, None)).evaluated:
                        return
            except Exception:
                logger.debug("prefetch stopped", exc_info=True)
            finally:
                self._prefetching = False
            # The route through jump hosts names hosts asked about here.
            GLib.idle_add(lambda: (None if self._closing else self._show_answer(), False)[1])

        threading.Thread(
            target=work, daemon=True, name="sshpilot-advanced-config-prefetch"
        ).start()
        return False

    def _evaluate(self) -> bool:
        self._evaluate_source = 0
        # Every question, the empty one included, outdates the ones before:
        # an answer still on its way for another host must not come back.
        self._request_serial = serial = self._request_serial + 1
        request = self._request()
        draft_serial, lines = self.editor.serial, self.editor.lines
        cache = self._answer_cache()

        def show(result) -> None:
            if isinstance(result, tuple):       # a pattern block's answer
                self._evaluation, self._related = None, (*result, lines)
            else:
                self._evaluation, self._related = result, None
                self._evaluated_serial = draft_serial
                self._evaluated_lines = lines
            self._show_answer()
            self._schedule_prefetch()

        work = None
        if request is not None:
            key = (request.host, request.user)
            if key in cache:
                show(cache[key])                # had already: shown at once
                return False
            work = lambda: self._ask(request, cache, key)  # noqa: E731
        else:
            block = self._pattern_block()
            if block is not None:
                had, work = self._related_work(block, serial, cache)
                if had is not None:
                    show(had)
                    return False
        if work is None:
            self._evaluation = self._related = None
            self._show_answer()
            self._schedule_prefetch()
            return False

        def done(result, error) -> None:
            if serial != self._request_serial:
                return          # a newer question was asked meanwhile
            if isinstance(error, SshPilotError) and error.code is ErrorCode.OPERATION_CANCELLED:
                return          # the daemon dropped it for a newer question
            if error is not None:
                logger.debug("draft evaluation failed", exc_info=error)
                self._evaluation = self._related = None
                self._show_answer()
                self._show_answer_banner(
                    _("OpenSSH could not be asked about this configuration.")
                )
                return
            if isinstance(result, tuple) and result[0] is None:
                return                          # given up for a newer question
            show(result)

        self._run(work, done)
        return False

    def _answer_ready(self) -> bool:
        """Whether the question as it stands has its answer already."""
        cache = self._answer_cache()
        request = self._request()
        if request is not None:
            return (request.host, request.user) in cache
        block = self._pattern_block()
        if block is None:
            return True                         # nothing to ask
        listed = plain_host_names(self.editor.blocks)
        candidates = listed + [name for name in sample_hosts(block) if name not in listed]
        return all((name, None) in cache for name in candidates)

    def _question(self) -> tuple:
        """What OpenSSH is asked: a host, or a pattern block's hosts."""
        block = self._pattern_block()
        return (self.target_text(), block.uid if block is not None else None)

    def _pattern_block(self):
        """The selected block when it names no host of its own (a pattern,
        a Match rule) and no host is typed."""
        if self._typed_host():
            return None
        block = self.editor.find_block(self.selected_uid)
        if block is None or block.disabled or block.kind == GLOBAL:
            return None
        if not block.patterns.strip() or first_host_name(block):
            return None
        return block

    def _related_work(self, block, serial: int, cache: dict) -> tuple:
        """The question a pattern block stands for: which blocks apply to a
        host it applies to? ``(answer had already or None, work to get it)``.

        A pattern is no host, and OpenSSH is only ever asked about a host.
        So it is asked about each host the configuration spells out (and a
        name made up from the pattern, for a pattern no listed host fits):
        the hosts whose answer has the block are the ones it applies to,
        and every block in those answers is related to it. An answer that
        is not one (a command waiting for consent, a rejected draft) is
        returned as it is, to be shown like any other.
        """
        # Asked without any agreed Match exec command: the agreement was
        # for one host, and this asks about every host of the configuration.
        text, allowed = self.editor.text, ()
        file = self._file
        header = self.editor.lines.headers.get(block.uid, 0)
        listed = plain_host_names(self.editor.blocks)
        candidates = listed + [name for name in sample_hosts(block) if name not in listed]

        def gather(answer_for) -> object:
            related: set = set()
            hosts = []
            for name in candidates:
                answer = answer_for(name)
                if answer is None:
                    continue
                if not answer.evaluated:
                    return answer
                if header in answer.matched_blocks:
                    related.update(answer.matched_blocks)
                    if name in listed:
                        hosts.append(name)
            return (related, tuple(hosts))

        if all((name, None) in cache for name in candidates):
            return gather(lambda name: cache[(name, None)]), None

        class _Superseded(Exception):
            pass

        def ask(name):
            if (name, None) in cache:
                return cache[(name, None)]
            if serial != self._request_serial:
                raise _Superseded()
            try:
                request = EvaluateSshConfigDraftRequest(
                    text=text, host=name, allowed_exec=allowed, file=file
                )
            except (TypeError, ValueError):
                return None
            return self._ask(request, cache, (name, None))

        def work():
            try:
                return gather(ask)
            except _Superseded:
                return (None, ())

        return None, work

    def _allow_exec(self) -> None:
        """Agree to the commands the banner is about: those, word for word,
        and for this host. Another new command, an edited one, or another
        host asks again (the daemon checks every command it is sent). The
        agreement goes with questions about the host on screen and no
        other: not the hosts asked about ahead of time, not a pattern
        block's hosts, not the save's check."""
        if self._evaluation is not None:
            self._allowed_exec.update(self._evaluation.pending_exec)
        self._schedule_evaluation(now=True)

    def _show_answer(self) -> None:
        """Hand OpenSSH's answer (or its absence) to the views."""
        self._sync_effective_page()
        evaluation = self._evaluation
        related = self._related if evaluation is None else None
        lines = self._evaluated_lines if evaluation is not None else self.editor.lines
        if related is not None:
            lines = related[2]
        if evaluation is None:
            self._show_answer_banner("")
        elif self._evaluated_serial == self.editor.serial:
            if evaluation.pending_exec and self._pattern_block() is not None:
                # A pattern block is asked about through many hosts: there
                # is no one host to agree to run the command for.
                self._show_answer_banner(messages.pending_exec_many_hosts_text())
            elif evaluation.pending_exec:
                self._show_answer_banner(
                    messages.pending_exec_text(evaluation, self.target_text()),
                    _("Run and Evaluate"), self._allow_exec,
                )
            elif evaluation.errors:
                self._show_answer_banner(messages.error_text(evaluation.errors[0]))
            else:
                self._show_answer_banner("")
        answered = bool(evaluation and evaluation.evaluated)
        matched = set(evaluation.matched_blocks) if answered else set()
        if related is not None:
            answered, matched = True, related[0]
        # Whether the selected block applies is said in its own view. The
        # sidebar lists every block alike, whatever applies (the user found
        # blocks dimmed for another host unexplained).
        applies_selected = None
        block = self.editor.find_block(self.selected_uid)
        if answered and block is not None and not block.disabled:
            applies_selected = block.kind == GLOBAL or lines.headers.get(block.uid, 0) in matched
        self._sync_test_button()        # a Match exec command may wait for consent
        user, host = split_target(self.target_text())
        self.structured.show_answer(AnswerView(
            evaluation, lines, host=host, applies=applies_selected,
            related_hosts=related[1] if related is not None else None,
            current=self._evaluated_serial == self.editor.serial,
        ))
        block_lines = frozenset()
        if block is not None:
            block_lines = frozenset(
                line for line in [lines.headers.get(block.uid, 0)] + [
                    lines.options.get(option.uid, 0) for option in block.options
                ] if line
            )
        self.effective.show(Answer(
            evaluation, list(self.editor.blocks), lines, user, host,
            note=messages.related_note(related[1]) if related is not None else "",
            block_lines=block_lines,
        ))


class AdvancedConfigWindow(AdvancedConfigBase, Adw.Window):
    """Advanced Configuration as a window of its own."""

    def _host_setup(self, parent) -> None:
        if parent is not None:
            self.set_transient_for(parent)
        self.set_default_size(1280, 820)
        self.set_title(_("Advanced Configuration"))
        self.connect("close-request", self._on_close_request)

    def _host_set_content(self, widget) -> None:
        self.set_content(widget)

    def _host_min_size(self):
        return _MIN_WIDTH, _MIN_HEIGHT

    def _host_close(self) -> None:
        self.close()

    def _host_dirty_changed(self) -> None:
        title = _("Advanced Configuration") + (" •" if self.dirty else "")
        if self.get_title() != title:
            self.set_title(title)


class AdvancedConfigView(AdvancedConfigBase, Adw.BreakpointBin):
    """Advanced Configuration as a widget of the main window.

    The same editor as the window. The host puts :attr:`sidebar_column`
    (the list of blocks) where it lists things, shows this widget, and sets:

    * ``host_present``: bring the widget to the front (``present``);
    * ``host_dirty``: called with True when the draft has unsaved changes
      and False when it has none, each time that changes;
    * ``host_close``: stop showing it.

    The draft lives as long as the widget: the host may hide it with
    changes unsaved and show it again. :meth:`reload` reads the
    configuration again when that loses nothing; the host calls it when the
    file may have changed.
    """

    embedded = True

    def _host_setup(self, parent) -> None:
        self.host_present = None
        self.host_dirty = None
        self.host_close = None
        self.host_close_refused = None
        self._dirty_told = False
        #: True from :meth:`reload` until its read has come back, and the
        #: draft's serial when it was asked for.
        self._reloading = False
        self._reload_serial = 0

    def _host_dirty_changed(self) -> None:
        dirty = bool(self.dirty)
        if dirty != self._dirty_told:
            self._dirty_told = dirty
            if callable(self.host_dirty):
                self.host_dirty(dirty)

    def _block_line(self, uid) -> int:
        """A line of the text that *uid*'s block owns: its header, or its
        first option for the options above all blocks. 0 when there is none."""
        block = self.editor.find_block(uid)
        if block is None:
            return 0
        lines = self.editor.lines
        line = lines.headers.get(block.uid) or 0
        if not line:
            line = next(
                (lines.options[o.uid] for o in block.options if o.uid in lines.options), 0
            )
        return int(line or 0)

    def reload(self) -> None:
        """Read the configuration again and show the block that was
        selected, if it is still there. Not while there is a draft to
        lose: a file changed under unsaved changes is said at the save."""
        if not self._loaded or self._saving or self.dirty or self.raw.pending:
            return
        # Until the read is back the view counts as not loaded: a place to
        # go to or a block to begin, asked for meanwhile, waits for the
        # text it will be applied to instead of being read away with the
        # old one.
        self._reloading = True
        self._reload_serial = self.editor.serial
        self._loaded = False
        block = self.editor.find_block(self.selected_uid)
        if block is not None and self._pending_target is None:
            line = self._block_line(block.uid)
            if line:
                self._pending_target = dict(
                    host="", line=int(line), option="", file=self._file, pin=False,
                )
        self._load()

    def _on_loaded(self, result, error) -> None:
        reloading, self._reloading = self._reloading, False
        if reloading and (error is not None or self.editor.serial != self._reload_serial):
            # The file could not be read again, or the draft was edited
            # while it was being read: what is on screen stays, and what
            # was asked for meanwhile is done on it.
            self._loaded = True
            self._apply_pending_target()
            self._apply_pending_block()
            return
        super()._on_loaded(result, error)

    def _host_set_content(self, widget) -> None:
        self.set_child(widget)

    def _host_min_size(self):
        # Far below a window's: the view is as wide as the main window
        # leaves it.
        return 360, 300

    def _host_close(self) -> None:
        if callable(self.host_close):
            self.host_close()

    def _host_close_refused(self) -> None:
        if callable(self.host_close_refused):
            self.host_close_refused()

    def get_focus(self):
        """The widget the keyboard is in, as a window says it: asked of the
        window this one is shown in."""
        root = self.get_root()
        return root.get_focus() if root is not None else None

    def present(self) -> None:
        if callable(self.host_present):
            self.host_present()

    def close(self) -> None:
        """Ask to close, as closing a window does."""
        if not self._on_close_request():
            self._host_close()
