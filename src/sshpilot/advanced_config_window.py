"""Advanced Configuration: the SSH configuration as OpenSSH reads it.

Sidebar: the blocks, to go to one and to choose the host OpenSSH is asked
about (the one the picked block names, or a typed one, or none). Middle: the configuration as ordered blocks (the global options, then
every ``Host`` and ``Match`` block), editable and reorderable; the blocks
that do not apply to the host fold to their header, in place. Right: what
OpenSSH resolves for the host, which line supplies each value, the order the
blocks are read in, the text itself, and the files ``Include`` lines read.

Nothing here decides what applies. Every answer comes from the daemon, which
asks OpenSSH (``ssh -G``) about the draft in the context a connection would
read the saved file in: see :mod:`sshpilot.core.ssh_config_draft`. The window
never touches the file: it loads and saves text through the daemon, like the
SSH Config Editor, which stays available beside it.

The window is three parts with one job each:

* :class:`~sshpilot.advanced_config_model.DraftEditor` (no GTK): the draft,
  its text, every edit and the undo history;
* :class:`~sshpilot.advanced_config_rules_view.RulesView`: the cards on the
  left, updated in place;
* :class:`~sshpilot.advanced_config_results_view.ResultsView`: the pages on
  the right, filled only when on screen.

This module is the controller between them: it turns gestures into edits,
asks the daemon, and hands each answer to the two views.
"""
from __future__ import annotations

import logging
import threading
from gettext import gettext as _
from typing import Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

from .advanced_config_model import (  # noqa: E402
    first_host_name,
    DraftEditor,
    Refusal,
    can_place,  # noqa: F401 - re-exported for callers and tests
)
from .advanced_config_results_view import Answer, ResultsView  # noqa: E402
from .advanced_config_rules_view import RulesView  # noqa: E402
from .api.errors import ErrorCode, SshPilotError  # noqa: E402
from .api.models.connections import (  # noqa: E402
    EvaluateSshConfigDraftRequest,
    SaveSshConfigTextRequest,
    SshConfigDraftEvaluation,
)
from .gtk import advanced_config_messages as messages  # noqa: E402
from .ssh_config_blocks import LineMap  # noqa: E402

logger = logging.getLogger(__name__)

_EVALUATE_DELAY_MS = 250
_HISTORY_DELAY_MS = 700
#: Asked about when saving: OpenSSH reads every line whatever the host, so a
#: name no block mentions is enough to learn whether it accepts the text.
_CHECK_HOST = "sshpilot-config-check"

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
.advcfg-options { margin-left: 28px; }
.advcfg-options:dir(rtl) { margin-left: 0; margin-right: 28px; }
.advcfg-footer { margin-top: 4px; }
.advcfg-block.selected { outline: 2px solid @accent_color; outline-offset: -1px; }
.advcfg-block.selected > .advcfg-header { background-color: alpha(@accent_color, 0.16); }
.advcfg-block.disabled { opacity: 0.6; }
.advcfg-block { transition: opacity 200ms ease-out; }
.advcfg-block.skipped { opacity: 0.45; }
.advcfg-block.skipped > .advcfg-header { border-radius: 12px; border-bottom-color: transparent; }
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


def split_target(text: str) -> tuple:
    """``(user, host)`` of ``[user@]host``; the user is None when absent."""
    text = text.strip()
    user, separator, host = text.rpartition("@")
    return (user or None, host) if separator else (None, text)


class AdvancedConfigWindow(Adw.Window):
    """The Advanced Configuration window.

    *client* is the daemon client; *on_saved* runs after each save.
    """

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
        self._on_saved = on_saved
        if parent is not None:
            self.set_transient_for(parent)
        self.set_default_size(1400, 760)
        self.set_title(_("Advanced Configuration"))

        self.editor = DraftEditor()
        self._revision = ""
        self._display_name = ""
        self._writable = True
        self._loaded = False
        #: The last answer and the draft (line map, serial) it was about.
        self._evaluation: Optional[SshConfigDraftEvaluation] = None
        self._evaluated_lines = LineMap({}, {})
        self._evaluated_serial = 0
        self._request_serial = 0
        self._evaluate_source = 0
        self._history_source = 0
        self._allowed_exec: set = set()
        self._notes_open: set = set()
        #: Blocks added or edited for the host in question: they stay open
        #: even when they do not apply to it.
        self._kept_visible: set = set()
        self.selected_uid: Optional[int] = None
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

        # Two banners, so that one never hides the other: what happened to
        # the file (read-only, changed on disk, could not be saved), and
        # what OpenSSH says about the draft.
        self.file_banner = Adw.Banner()
        self.file_banner.connect("button-clicked", self._on_file_banner_button)
        self._file_banner_action: Optional[Callable[[], None]] = None
        view.add_top_bar(self.file_banner)
        self.banner = Adw.Banner()
        self.banner.connect("button-clicked", self._on_answer_banner_button)
        self._answer_banner_action: Optional[Callable[[], None]] = None
        view.add_top_bar(self.banner)

        self.rules = RulesView(self)
        self.results = ResultsView(self)
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, wide_handle=True)
        paned.set_shrink_start_child(False)
        paned.set_shrink_end_child(False)
        paned.set_position(620)
        paned.set_start_child(self.rules)
        paned.set_end_child(self.results)
        paned.set_hexpand(True)
        self.sidebar = self._build_sidebar()
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        content.append(self.sidebar)
        content.append(paned)
        view.set_content(content)
        self.sidebar_button = Gtk.ToggleButton(
            icon_name="sidebar-show-symbolic", active=True,
            tooltip_text=_("Toggle Sidebar"),
        )
        self.sidebar_button.bind_property(
            "active", self.sidebar, "visible", GObject.BindingFlags.SYNC_CREATE
        )
        header.pack_start(self.sidebar_button)

    def _build_sidebar(self) -> Gtk.Widget:
        # The blocks, in the file's order. Picking one goes to it, and the
        # host it names (the first plain name on its Host line) becomes the
        # host OpenSSH is asked about; a Match, global or pattern-only block
        # names none, so nothing is asked. A name typed in the field takes
        # the list's place (one no block spells out can still match a
        # pattern) and searches the list. Selecting a block in the rules
        # themselves never changes the host.
        self.target_entry = Gtk.SearchEntry()
        self.target_entry.set_placeholder_text(_("Search or type a host"))
        for side in ("top", "bottom", "start", "end"):
            getattr(self.target_entry, f"set_margin_{side}")(8)
        self.target_entry.connect("search-changed", self._on_target_typed)
        self._block_list = Gtk.ListBox()
        self._block_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._block_list.add_css_class("navigation-sidebar")
        self._block_list.set_filter_func(self._block_row_shown)
        self._block_list.connect("row-selected", self._on_block_picked)
        self._all_row = Gtk.ListBoxRow()
        self._all_row.set_child(Gtk.Label(label=_("All Blocks"), xalign=0))
        self._all_row._uid = None
        self._all_row._text = ""
        self._block_list.append(self._all_row)
        self._block_rows: dict = {}
        #: The block picked in the list (None: All Blocks). A typed name
        #: takes its place: there is one host in question, shown one way.
        self._picked_uid: Optional[int] = None
        #: True while the window itself moves the list's selection.
        self._target_syncing = False
        #: The field's text as last acted on. "search-changed" arrives a
        #: moment after the text changes, so text the window put there
        #: itself is told apart by this, not by a flag.
        self._typed_seen = ""
        self._scroll_after_answer: Optional[int] = None
        self._block_list.select_row(self._all_row)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        scroller.set_child(self._block_list)
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        column.append(self.target_entry)
        column.append(scroller)
        column.set_size_request(210, -1)
        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        outer.append(column)
        outer.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        return outer

    # ------------------------------------------------------------------
    # convenience for callers and tests
    # ------------------------------------------------------------------

    @property
    def draft(self):
        return self.editor.draft

    @property
    def raw_pending(self) -> bool:
        return self.results.raw_pending

    @property
    def dirty(self) -> bool:
        return self.editor.dirty or self.results.raw_pending

    def target_text(self) -> str:
        """The host OpenSSH is asked about: the one typed in the sidebar,
        else the one the block picked there names. Empty: none."""
        return self.target_entry.get_text().strip() or first_host_name(
            self.editor.find_block(self._picked_uid)
        )

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

    def _load(self) -> None:
        self.save_button.set_sensitive(False)
        self._run(self._client.get_ssh_config_text, self._on_loaded)

    def _on_loaded(self, result, error) -> None:
        if error is not None:
            logger.debug("Advanced Configuration could not load", exc_info=error)
            self._show_file_banner(_("The SSH configuration could not be read."))
            return
        self._revision = result.revision
        self._display_name = result.display_name
        self._writable = bool(result.writable)
        self._title.set_subtitle(result.display_name)
        self.editor.load(result.text)
        self._loaded = True
        self._allowed_exec.clear()
        self._notes_open.clear()
        self._kept_visible.clear()
        self.results.discard_raw()
        self._show_file_banner(
            "" if self._writable
            else _("This SSH configuration is read-only: changes cannot be saved.")
        )
        self._show_draft(evaluate_now=True)
        self._check_at_open()

    def _check_at_open(self) -> None:
        """Ask OpenSSH about the file as it is, before any host is looked at.

        A configuration OpenSSH already rejects is said so at once.
        """
        serial = self.editor.serial
        try:
            request = EvaluateSshConfigDraftRequest(text=self.editor.text, host=_CHECK_HOST)
        except (TypeError, ValueError):
            return

        def done(result, error) -> None:
            if error is not None or result is None:
                return
            if result.errors and serial == self.editor.serial and self._evaluation is None:
                self._show_answer_banner(messages.error_text(result.errors[0]))

        self._run(lambda: self._client.evaluate_ssh_config_draft(request), done)

    def save(self) -> None:
        """Save what is on screen, after asking OpenSSH whether it accepts it."""
        if self._saving or not self.dirty or not self._writable:
            return
        if self.results.raw_pending:
            # The rules are locked while text is being typed, so the text
            # is the newer of the two: it is what gets saved.
            self.apply_raw()
            if not self.dirty:
                return
        self._saving = True
        self._update_chrome()
        text = self.editor.text
        try:
            request = EvaluateSshConfigDraftRequest(
                text=text, host=_CHECK_HOST, allowed_exec=tuple(sorted(self._allowed_exec)),
            )
        except (TypeError, ValueError):
            self._write(text)
            return

        def checked(result, error) -> None:
            # A draft with a Match exec command nobody agreed to run cannot
            # be checked without running it; and when the check itself
            # fails, the save is not held hostage to it.
            if error is None and result is not None and result.errors:
                self._confirm_rejected(text, result)
            else:
                self._write(text)

        self._run(lambda: self._client.evaluate_ssh_config_draft(request), checked)

    def _confirm_rejected(self, text: str, evaluation: SshConfigDraftEvaluation) -> None:
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
                self._write(text)
            else:
                self._saving = False
                self._update_chrome()

        alert.connect("response", answered)
        self._save_alert = alert
        alert.present(self)

    def _write(self, text: str) -> None:
        request = SaveSshConfigTextRequest(text=text, expected_revision=self._revision)
        self._run(
            lambda: self._client.save_ssh_config_text(request),
            lambda result, error: self._on_saved_result(text, result, error),
        )

    def _on_saved_result(self, text: str, result, error) -> None:
        self._saving = False
        if error is not None:
            code = error.code if isinstance(error, SshPilotError) else None
            if code is ErrorCode.STALE_EDITOR:
                self._show_file_banner(
                    _("The SSH configuration changed on disk since it was opened."),
                    _("Reload"), self._load,
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
        self._schedule_evaluation(now=True)  # Match exec commands are saved now
        if callable(self._on_saved):
            try:
                self._on_saved()
            except Exception:
                logger.debug("on_saved failed", exc_info=True)

    def _on_reload(self, *_args) -> None:
        if self.dirty:
            self._confirm_discard(self._load)
        else:
            self._load()

    def _on_copy(self, *_args) -> None:
        self.get_clipboard().set(self.editor.text)
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
        if self.editor.find_block(self.selected_uid) is None:
            self.selected_uid = None
        self._kept_visible &= {block.uid for block in self.editor.blocks}
        self.rules.sync(self.editor.blocks, self._notes_open, self.editor.has_global)
        self.rules.select(self.selected_uid)
        self.results.set_text(self.editor.text)
        self._sync_block_list()
        self._update_chrome()
        self._show_answer()                     # the last answer, until the new one
        self._schedule_evaluation(now=evaluate_now)

    def _update_chrome(self) -> None:
        busy = self._saving
        self.undo_button.set_sensitive(self.editor.can_undo and not busy)
        self.redo_button.set_sensitive(self.editor.can_redo and not busy)
        self.save_button.set_sensitive(self.dirty and self._writable and not busy)
        self._title.set_title(_("Advanced Configuration") + (" •" if self.dirty else ""))
        # One editor at a time: while text typed on the Text page waits to
        # be applied, the rules are not edited beside it (the two would
        # overwrite each other).
        self.rules.set_sensitive(not self.results.raw_pending)

    def _sync_block_list(self) -> None:
        """Make the sidebar's rows match the blocks, in place: a row is
        kept for as long as its block exists, and only relabelled."""
        blocks = self.editor.blocks
        rows = self._block_rows
        wanted = {block.uid for block in blocks}
        self._target_syncing = True
        try:
            for uid in [uid for uid in rows if uid not in wanted]:
                self._block_list.remove(rows.pop(uid))
            for index, block in enumerate(blocks):
                row = rows.get(block.uid)
                if row is None:
                    label = Gtk.Label(xalign=0)
                    label.add_css_class("monospace")
                    label.set_ellipsize(Pango.EllipsizeMode.END)
                    row = rows[block.uid] = Gtk.ListBoxRow()
                    row.set_child(label)
                    row._uid = block.uid
                    row._text = None
                    self._block_list.insert(row, index + 1)
                elif row.get_index() != index + 1:
                    self._block_list.remove(row)
                    self._block_list.insert(row, index + 1)
                text = messages.block_title(block.kind, block.patterns)
                if row._text != text:
                    row._text = text
                    row.get_child().set_label(text)
                    row.set_tooltip_text(text)
            if self._picked_uid not in wanted:
                self._picked_uid = None
            typed = bool(self.target_entry.get_text().strip())
            chosen = None if typed else rows.get(self._picked_uid, self._all_row)
            if self._block_list.get_selected_row() is not chosen:
                self._block_list.select_row(chosen)
        finally:
            self._target_syncing = False
        if self.target_entry.get_text().strip():
            self._block_list.invalidate_filter()

    def _block_row_shown(self, row) -> bool:
        typed = self.target_entry.get_text().strip().lower()
        return not typed or row._uid is None or typed in (row._text or "").lower()

    def _target_changed(self, *, idle: bool = False) -> None:
        # A command can depend on the host (%h, %r): agreement to run it
        # was for the host in question at the time. Blocks kept open were
        # worked on for that host too.
        self._allowed_exec.clear()
        self._kept_visible.clear()
        self._schedule_evaluation(idle=idle)

    def _on_block_picked(self, _listbox, row) -> None:
        if self._target_syncing or row is None:
            return
        before = self.target_text()
        self._picked_uid = row._uid
        self._set_typed("")
        if row._uid is not None:
            self.select_block(row._uid)
            self.rules.scroll_to(row._uid)
        self._scroll_after_answer = row._uid
        if self.target_text() != before:
            # Asked from the idle loop, so the highlight is painted first.
            self._target_changed(idle=True)

    def _set_typed(self, text: str) -> None:
        """Put *text* in the field as the window's own doing."""
        self._typed_seen = text
        if self.target_entry.get_text() != text:
            self.target_entry.set_text(text)
            self._block_list.invalidate_filter()

    def _on_target_typed(self, *_args) -> None:
        typed = self.target_entry.get_text().strip()
        if typed == self._typed_seen:
            return
        self._typed_seen = typed
        self._picked_uid = None
        self._target_syncing = True
        try:
            self._block_list.select_row(None if typed else self._all_row)
        finally:
            self._target_syncing = False
        self._block_list.invalidate_filter()
        self._target_changed()

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
        self.results.set_text(self.editor.text)
        self._schedule_history()
        self._update_chrome()
        self._schedule_evaluation()

    def add_block(self, kind: str) -> None:
        block = self.editor.add_block(kind)
        if block is None:
            return
        self._kept_visible.add(block.uid)
        self.selected_uid = block.uid
        self._after_structure()
        GLib.idle_add(lambda: (self.rules.focus_block(block.uid), False)[1])

    def move_block(self, uid: int, index: int) -> bool:
        return self._after_structure(self.editor.move_block(uid, index))

    def move_block_by(self, uid: int, delta: int) -> bool:
        return self._after_structure(self.editor.move_block_by(uid, delta))

    def move_selected(self, delta: int) -> bool:
        """Alt+Up / Alt+Down: move the selected block one place."""
        if self.selected_uid is not None and not self.results.raw_pending:
            self.move_block_by(self.selected_uid, delta)
        return True

    def toggle_block(self, uid: int) -> None:
        self._kept_visible.add(uid)
        self._after_structure(self.editor.toggle_block(uid))

    def duplicate_block(self, uid: int) -> None:
        copy = self.editor.duplicate_block(uid)
        if copy is not None:
            self._kept_visible.add(copy.uid)
            self.selected_uid = copy.uid
            self._after_structure()

    def delete_block(self, uid: int) -> None:
        self._after_structure(self.editor.delete_block(uid))

    def open_note(self, uid: int) -> None:
        self._notes_open.add(uid)
        self._show_draft()
        self.rules.focus_note(uid)

    def add_option(self, block_uid: int, key: str = "", value: str = "") -> None:
        option = self.editor.add_option(block_uid, key, value)
        if option is None:
            return
        self._kept_visible.add(block_uid)
        self._after_structure()
        GLib.idle_add(lambda: (self.rules.focus_option(option.uid, bool(key)), False)[1])

    def delete_option(self, uid: int) -> None:
        self._after_structure(self.editor.delete_option(uid))

    def move_option(self, uid: int, block_uid: int, index: Optional[int]) -> bool:
        return self._after_structure(self.editor.move_option(uid, block_uid, index))

    def patterns_typed(self, uid: int, text: str) -> None:
        self._kept_visible.add(uid)
        changed = self.editor.set_patterns(uid, text)
        self._after_typing(changed)
        if changed:
            self._sync_block_list()

    def note_typed(self, uid: int, text: str) -> None:
        self._kept_visible.add(uid)
        self._after_typing(self.editor.set_note(uid, text))

    def option_typed(self, uid: int, field: str, text: str) -> None:
        owner, _option = self.editor.find_option(uid)
        if owner is not None:
            self._kept_visible.add(owner.uid)
        self._after_typing(self.editor.set_option(uid, field, text))

    # -- drops ---------------------------------------------------------------

    def drop_block(self, uid: int, onto_uid: int, before: bool) -> bool:
        blocks = self.editor.blocks
        source, target = self.editor.find_block(uid), self.editor.find_block(onto_uid)
        if source is None or target is None:
            return False
        index = blocks.index(target) + (0 if before else 1)
        if blocks.index(source) < index:
            index -= 1
        return self.move_block(uid, index)

    def drop_option(self, uid: int, onto_uid: int, before: bool) -> bool:
        block, option = self.editor.find_option(onto_uid)
        if option is None:
            return False
        return self.move_option(uid, block.uid, block.options.index(option) + (0 if before else 1))

    def drop_option_on_block(self, uid: int, block_uid: int) -> bool:
        return self.move_option(uid, block_uid, None)

    # -- the Text page -------------------------------------------------------

    def raw_text_changed(self) -> None:
        self._update_chrome()

    def apply_raw(self) -> None:
        text = self.results.raw_text()
        self.results.discard_raw()
        self.editor.replace_text(text)
        self._notes_open.clear()
        self._after_structure()

    def discard_raw(self) -> None:
        self.results.discard_raw()
        self._update_chrome()

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

    def select_block(self, uid: Optional[int]) -> None:
        """Select a block: the one Alt+arrows move. The host in question is
        the one chosen in the sidebar and does not follow."""
        if uid == self.selected_uid:
            return
        self.selected_uid = uid if self.editor.find_block(uid) is not None else None
        self.rules.select(self.selected_uid)

    def reveal_line(self, line: int) -> None:
        """Show the option or block the draft has at *line* (from a source
        on the Effective page)."""
        uid = self.editor.owner_of_line(line, self._evaluated_lines)
        if uid is None:
            return
        owner, _option = self.editor.find_option(uid)
        self.select_block(owner.uid if owner is not None else uid)
        self.rules.reveal(uid)

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
                allowed_exec=tuple(sorted(self._allowed_exec)),
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

    def _evaluate(self) -> bool:
        self._evaluate_source = 0
        # Every question, the empty one included, outdates the ones before:
        # an answer still on its way for another host must not come back.
        self._request_serial = serial = self._request_serial + 1
        request = self._request()
        if request is None:
            self._evaluation = None
            self._show_answer()
            return False
        draft_serial, lines = self.editor.serial, self.editor.lines

        def done(result, error) -> None:
            if serial != self._request_serial:
                return          # a newer question was asked meanwhile
            if isinstance(error, SshPilotError) and error.code is ErrorCode.OPERATION_CANCELLED:
                return          # the daemon dropped it for a newer question
            if error is not None:
                logger.debug("draft evaluation failed", exc_info=error)
                self._evaluation = None
                self._show_answer()
                self._show_answer_banner(
                    _("OpenSSH could not be asked about this configuration.")
                )
                return
            self._evaluation = result
            self._evaluated_serial = draft_serial
            self._evaluated_lines = lines
            self._show_answer()

        self._run(lambda: self._client.evaluate_ssh_config_draft(request), done)
        return False

    def _allow_exec(self) -> None:
        """Agree to the commands the banner is about: those, word for word,
        and for this host. Another new command, an edited one, or another
        host asks again (the daemon checks every command it is sent)."""
        if self._evaluation is not None:
            self._allowed_exec.update(self._evaluation.pending_exec)
        self._schedule_evaluation(now=True)

    def _show_answer(self) -> None:
        """Hand OpenSSH's answer (or its absence) to the two views."""
        evaluation = self._evaluation
        lines = self._evaluated_lines if evaluation is not None else self.editor.lines
        if evaluation is None:
            self._show_answer_banner("")
        elif self._evaluated_serial == self.editor.serial:
            if evaluation.pending_exec:
                self._show_answer_banner(
                    messages.pending_exec_text(evaluation),
                    _("Run and Evaluate"), self._allow_exec,
                )
            elif evaluation.errors:
                self._show_answer_banner(messages.error_text(evaluation.errors[0]))
            else:
                self._show_answer_banner("")
        matched = self.rules.show_answer(
            self.editor.blocks, evaluation, lines, keep_open=self._kept_visible,
        )
        for uid, row in self._block_rows.items():
            card = self.rules.cards.get(uid)
            row.get_child().set_opacity(0.45 if card is not None and card.folds else 1.0)
        if self._scroll_after_answer is not None and evaluation is not None:
            # The block just picked has moved: blocks above it folded.
            # Once, for that pick: later answers follow edits, and must not
            # pull the view away from what is being edited.
            uid, self._scroll_after_answer = self._scroll_after_answer, None
            GLib.timeout_add(260, lambda: (self.rules.scroll_to(uid), False)[1])
        user, host = split_target(self.target_text())
        self.results.show(
            Answer(evaluation, list(self.editor.blocks), lines, user, host), matched
        )
