"""The EasyEnv page: widgets only.

Everything here runs on the GTK main thread and nothing here talks to the
network. The page shows what the plugin hands it (``show_signed_out`` and
``show``) and passes each click back to the plugin, which does the work off
the UI thread and calls ``show`` again.

The workspace cards follow the dashboard's own card (``workspaces/Card.tsx``
in the EasyEnv frontend): a coloured strip on top, the status badge, the
title, "N machines · by X · 3h ago", and the recipes' logos with the actions
beside them. Colours are the dashboard's.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

from . import connections, model  # noqa: E402

logger = logging.getLogger(__name__)

#: Seconds between refreshes while a workspace is starting, and otherwise.
#: Workspaces end on their own when their time is up, so the page keeps
#: asking even when nothing is starting, just not often.
FAST_REFRESH_SECONDS = 8
SLOW_REFRESH_SECONDS = 60

#: Logos shown on a card before the rest become "+N", as on the dashboard.
CARD_LOGOS = 6

CSS = """
.ee-card { border-radius: 10px; background-color: @card_bg_color;
           border: 1px solid alpha(currentColor, 0.08); }
.ee-card:hover { border-color: rgba(0, 119, 255, 0.55); }
.ee-strip { min-height: 3px; }
.ee-strip.ee-running { background-color: rgba(34, 197, 94, 0.85); }
.ee-strip.ee-starting { background-color: rgba(59, 130, 246, 0.85); }
.ee-strip.ee-not-started { background-color: rgba(245, 158, 11, 0.85); }
.ee-strip.ee-failed { background-color: rgba(239, 68, 68, 0.85); }
.ee-strip.ee-ended { background-color: rgba(107, 114, 128, 0.5); }
progressbar.ee-progress > trough { min-height: 3px; border-radius: 0;
                                   background-color: alpha(#3b82f6, 0.2); }
progressbar.ee-progress > trough > progress { min-height: 3px; border-radius: 0;
                                              background-color: #3b82f6; }
.ee-badge { border-radius: 6px; padding: 3px 10px; font-size: 12px; font-weight: 600; }
.ee-badge.ee-running { background-color: #E8F5E9; color: #062D1B; }
.ee-badge.ee-starting { background-color: #FFF4E5; color: #482909; }
.ee-badge.ee-not-started { background-color: #E8EAF6; color: #05205E; }
.ee-badge.ee-ended { background-color: #F3E8FF; color: #5B21B6; }
.ee-badge.ee-failed { background-color: #FDECEC; color: #8A1C1C; }
@media (prefers-color-scheme: dark) {
  .ee-badge.ee-running { background-color: #C6F1DA; }
  .ee-badge.ee-starting { background-color: #FCDEC0; }
  .ee-badge.ee-not-started { background-color: #D7E4FF; }
  .ee-badge.ee-ended { background-color: #2A1F47; color: #C4B5FD; }
  .ee-badge.ee-failed { background-color: #5C1D1D; color: #F5C2C2; }
}
.ee-card-title { font-weight: 600; font-size: 1.05em; }
.ee-logo { background-color: #e5e7eb; border-radius: 99px; padding: 3px; }
.ee-stack-group { font-weight: 700; opacity: 0.6; }
.ee-tile { border-radius: 10px; padding: 10px 12px; }
.ee-tile:checked { background-color: alpha(@accent_bg_color, 0.14);
                   box-shadow: inset 0 0 0 2px @accent_bg_color; color: inherit; }
.ee-logo.ee-stack { border-radius: 6px; background-color: alpha(currentColor, 0.08); }
.ee-more { background-color: alpha(currentColor, 0.1); border-radius: 99px;
           padding: 0 6px; font-size: 11px; font-weight: 600; }
.ee-stop { color: #FF6433; }
.ee-avatar { border-radius: 99px; color: #ffffff; font-weight: 700;
             min-width: 30px; min-height: 30px; }
.ee-avatar-small { min-width: 24px; min-height: 24px; font-size: 10px; }
.ee-c0{background:#e95420;} .ee-c1{background:#3776ab;} .ee-c2{background:#3c873a;}
.ee-c3{background:#76b900;} .ee-c4{background:#336791;} .ee-c5{background:#00add8;}
.ee-c6{background:#8250df;} .ee-c7{background:#d83b01;}
.ee-gate-logo { border-radius: 18px; background: @accent_bg_color; color: #ffffff;
                padding: 14px; }
.ee-banner { border-radius: 10px; padding: 10px 14px;
             background-color: alpha(@error_color, 0.12); }
.ee-banner.low { background-color: alpha(@warning_color, 0.14); }
.ee-banner.info { background-color: alpha(#3b82f6, 0.12); }
.ee-pill { border-radius: 99px; padding: 2px 10px; font-weight: 700;
           background-color: alpha(currentColor, 0.12); }
"""

_css_done = False


def _install_css():
    global _css_done
    if _css_done:
        return
    display = Gdk.Display.get_default()
    if display is None:
        return
    provider = Gtk.CssProvider()
    if hasattr(provider, "load_from_string"):
        provider.load_from_string(CSS)
    else:
        provider.load_from_data(CSS.encode("utf-8"))
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    _css_done = True


def _margins(widget, px):
    widget.set_margin_top(px)
    widget.set_margin_bottom(px)
    widget.set_margin_start(px)
    widget.set_margin_end(px)


def _clear(box):
    child = box.get_first_child()
    while child is not None:
        nxt = child.get_next_sibling()
        box.remove(child)
        child = nxt


def _label(text, *classes, xalign=0.0, wrap=False):
    lbl = Gtk.Label(label=text)
    lbl.set_xalign(xalign)
    lbl.set_wrap(wrap)
    for c in classes:
        lbl.add_css_class(c)
    return lbl


def _pill(text, style=""):
    lbl = _label(text, "ee-pill", "caption", xalign=0.5)
    if style:
        lbl.add_css_class(style)
    lbl.set_valign(Gtk.Align.CENTER)
    return lbl


def _spacer():
    box = Gtk.Box()
    box.set_hexpand(True)
    return box


def _spinner():
    spinner = Adw.Spinner() if hasattr(Adw, "Spinner") else Gtk.Spinner(spinning=True)
    spinner.set_valign(Gtk.Align.CENTER)
    return spinner


def _icon_button(icon, tooltip, callback, *classes):
    btn = Gtk.Button.new_from_icon_name(icon)
    btn.set_tooltip_text(tooltip)
    btn.set_valign(Gtk.Align.CENTER)
    btn.add_css_class("flat")
    for c in classes:
        btn.add_css_class(c)
    btn.connect("clicked", lambda _b: callback())
    return btn


def _text_button(text, callback, *classes):
    btn = Gtk.Button(label=text)
    btn.set_valign(Gtk.Align.CENTER)
    for c in classes:
        btn.add_css_class(c)
    btn.connect("clicked", lambda _b: callback())
    return btn


def _menu(items):
    """A ⋮ button whose popover holds plain buttons: (label, callback, destructive)."""
    button = Gtk.MenuButton(icon_name="view-more-symbolic")
    button.add_css_class("flat")
    button.set_valign(Gtk.Align.CENTER)
    button.set_tooltip_text("More")
    popover = Gtk.Popover()
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    _margins(box, 4)
    for text, callback, destructive in items:
        item = Gtk.Button()
        item.add_css_class("flat")
        lbl = _label(text)
        if destructive:
            lbl.add_css_class("error")
        item.set_child(lbl)

        def clicked(_b, cb=callback):
            popover.popdown()
            cb()

        item.connect("clicked", clicked)
        box.append(item)
    popover.set_child(box)
    button.set_popover(popover)
    return button


def open_uri(parent, url, notify):
    """The system browser. Buying hours ends in a card payment, which belongs
    where the person's password manager and saved cards are."""
    try:
        Gtk.UriLauncher.new(url).launch(parent, None, None, None)
        return
    except Exception:  # noqa: BLE001 - GTK older than 4.10
        pass
    try:
        Gio.AppInfo.launch_default_for_uri(url, None)
    except Exception:  # noqa: BLE001
        logger.warning("could not open %s", url, exc_info=True)
        notify(f"Open {url} in your browser")


def copy_text(widget, text):
    clipboard = widget.get_clipboard()
    try:
        clipboard.set(text)
    except Exception:  # noqa: BLE001 - PyGObject without the override
        clipboard.set_content(Gdk.ContentProvider.new_for_value(text))


def confirm(parent, heading, body, action, callback, destructive=True):
    dialog = Adw.AlertDialog(heading=heading, body=body)
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("go", action)
    dialog.set_response_appearance(
        "go", Adw.ResponseAppearance.DESTRUCTIVE if destructive
        else Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("cancel")
    dialog.set_close_response("cancel")
    dialog.connect("response", lambda _d, r: callback() if r == "go" else None)
    dialog.present(parent)


class Page:
    def __init__(self, plugin):
        self.plugin = plugin
        self.state = None
        self._list_key = None
        self._header_key = None
        self._timers = []          # (Gtk.Label, view) ticking each second
        self._tick_id = None
        self._refresh_id = None
        self._busy = set()         # workspace uuids with an action under way
        self._textures = {}        # (logo url, size) -> Gdk.Texture, or None when it failed
        _install_css()

        self.root = Gtk.Stack()
        self.root.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.root.add_named(self._build_loading(), "loading")
        self.root.add_named(self._build_gate(), "gate")
        self.root.add_named(self._build_dashboard(), "dashboard")
        self.root.set_visible_child_name("loading")
        self.root.connect("map", lambda *_a: self._on_map())
        self.root.connect("unmap", lambda *_a: self._stop_timers())

    # --- logos ----------------------------------------------------------------

    def logo_chip(self, url, title, size=20):
        """A recipe's logo on the dashboard's round grey chip."""
        image = Gtk.Image.new_from_icon_name("application-x-executable-symbolic")
        image.set_pixel_size(size)
        chip = Gtk.Box()
        chip.add_css_class("ee-logo")
        chip.set_valign(Gtk.Align.CENTER)
        chip.append(image)
        if title:
            chip.set_tooltip_text(title)
        self.set_logo(image, url, size)
        return chip

    def set_logo(self, image, url, size):
        if not url:
            image.set_from_icon_name("application-x-executable-symbolic")
            image.set_pixel_size(size)
            return
        key = (url, size)
        if key in self._textures:
            self._apply_texture(image, self._textures[key], size)
            return
        image.url = url

        def loaded(path):
            if key not in self._textures:
                self._textures[key] = self._texture(path, size)
            # A recycled list row may show another recipe by now.
            if getattr(image, "url", None) == url:
                self._apply_texture(image, self._textures[key], size)

        self.plugin.logo(url, loaded)

    @staticmethod
    def _texture(path, size):
        if not path:
            return None
        try:
            # Rendered at twice the size, so it stays sharp on a HiDPI screen.
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, size * 2, size * 2, True)
            return Gdk.Texture.new_for_pixbuf(pixbuf)
        except Exception:  # noqa: BLE001 - no SVG loader, or not an image
            try:
                return Gdk.Texture.new_from_filename(path)
            except Exception:  # noqa: BLE001
                return None

    @staticmethod
    def _apply_texture(image, texture, size):
        if texture is None:
            image.set_from_icon_name("application-x-executable-symbolic")
        else:
            image.set_from_paintable(texture)
        image.set_pixel_size(size)

    # --- the three faces ------------------------------------------------------

    def _build_loading(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_valign(Gtk.Align.CENTER)
        box.set_halign(Gtk.Align.CENTER)
        spinner = _spinner()
        spinner.set_size_request(32, 32)
        box.append(spinner)
        box.append(_label("Loading EasyEnv", "dim-label", xalign=0.5))
        return box

    def _build_gate(self):
        clamp = Adw.Clamp(maximum_size=440)
        clamp.set_valign(Gtk.Align.CENTER)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        _margins(col, 32)

        logo = Gtk.Image.new_from_icon_name("network-server-symbolic")
        logo.set_pixel_size(32)
        logo.add_css_class("ee-gate-logo")
        logo.set_halign(Gtk.Align.CENTER)
        col.append(logo)

        title = _label("Connect EasyEnv", "title-1", xalign=0.5)
        title.set_margin_top(6)
        col.append(title)
        col.append(_label(
            "Create workspaces on easyenv.io and open their machines as ordinary "
            "SSH connections. No VPN, no public IP, nothing else to install.",
            "dim-label", xalign=0.5, wrap=True))

        steps = Adw.PreferencesGroup()
        steps.set_margin_top(12)
        step1 = Adw.ActionRow(title="1. Copy your token",
                              subtitle="The dashboard shows it on its Integration page")
        open_btn = _text_button("Open dashboard", self._open_token_page, "pill")
        step1.add_suffix(open_btn)
        step1.set_activatable_widget(open_btn)
        steps.add(step1)

        self._token_row = Adw.PasswordEntryRow(title="2. Paste it here")
        self._token_row.connect("entry-activated", lambda _r: self._sign_in())
        self._token_row.connect("changed", lambda _r: self._gate_error.set_visible(False))
        steps.add(self._token_row)
        col.append(steps)

        self._gate_error = _label("", "error", xalign=0.5, wrap=True)
        self._gate_error.set_visible(False)
        col.append(self._gate_error)

        self._signin_btn = Gtk.Button(label="Sign in")
        self._signin_btn.add_css_class("suggested-action")
        self._signin_btn.add_css_class("pill")
        self._signin_btn.set_halign(Gtk.Align.CENTER)
        self._signin_btn.set_margin_top(6)
        self._signin_btn.connect("clicked", lambda _b: self._sign_in())
        col.append(self._signin_btn)

        col.append(_label(
            "The token is kept where the easyenv CLI keeps it "
            "(~/.config/easyenv/config.yaml, readable only by you), so the CLI "
            "is signed in too.", "dim-label", "caption", xalign=0.5, wrap=True))
        clamp.set_child(col)
        return clamp

    def _build_dashboard(self):
        """One bar on top: who you are (and every account thing behind it),
        the search, which workspaces to list, and New workspace."""
        view = Adw.ToolbarView()
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        _margins(bar, 12)
        bar.set_margin_bottom(0)

        # The account button's face is built once and updated in place, so
        # the breakpoints below keep hold of the widgets they hide.
        self._account_button = Gtk.MenuButton()
        self._account_button.set_tooltip_text("Account, hours and sign out")
        self._account_button.set_popover(Gtk.Popover())
        face = Gtk.Box(spacing=8)
        self._face_avatar = _label("EE", "ee-avatar", "ee-avatar-small", xalign=0.5)
        self._face_avatar.set_valign(Gtk.Align.CENTER)
        face.append(self._face_avatar)
        self._face_name = _label("", "heading")
        self._face_name.set_ellipsize(Pango.EllipsizeMode.END)
        self._face_name.set_width_chars(6)
        self._face_name.set_max_width_chars(16)
        self._face_name.set_valign(Gtk.Align.CENTER)
        face.append(self._face_name)
        self._face_hours = _pill("")
        face.append(self._face_hours)
        face.append(Gtk.Image.new_from_icon_name("pan-down-symbolic"))
        self._account_button.set_child(face)
        bar.append(self._account_button)

        self._search = Gtk.SearchEntry(placeholder_text="Search workspaces")
        self._search.set_hexpand(True)
        self._search.set_width_chars(12)
        self._search.connect("search-changed", lambda _e: self._render_list(force=True))
        bar.append(self._search)

        # Which workspaces to list: one button showing the current choice and
        # its count, the three choices behind it. Three linked buttons with
        # counts took a third of the bar.
        self._show = "active"
        self._filter = Gtk.MenuButton()
        self._filter.set_tooltip_text("Which workspaces to list")
        face = Gtk.Box(spacing=6)
        face.append(Gtk.Image.new_from_icon_name("view-list-symbolic"))
        self._filter_label = _label("Active")
        face.append(self._filter_label)
        face.append(Gtk.Image.new_from_icon_name("pan-down-symbolic"))
        self._filter.set_child(face)
        popover = Gtk.Popover()
        items = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        _margins(items, 4)
        self._filter_items = {}
        for key, label in model.FILTERS:
            item = Gtk.Button()
            item.add_css_class("flat")
            row = Gtk.Box(spacing=10)
            check = Gtk.Image.new_from_icon_name("object-select-symbolic")
            row.append(check)
            name = _label(label)
            name.set_hexpand(True)
            row.append(name)
            count = _label("", "dim-label", "numeric")
            row.append(count)
            item.set_child(row)
            item.connect("clicked", self._filter_chosen, key, popover)
            items.append(item)
            self._filter_items[key] = (check, count, label)
        popover.set_child(items)
        self._filter.set_popover(popover)
        bar.append(self._filter)

        self._refresh_btn = _icon_button("view-refresh-symbolic", "Refresh",
                                         self.plugin.refresh)
        bar.append(self._refresh_btn)
        new_btn = Gtk.Button()
        new_btn.set_child(Adw.ButtonContent(icon_name="list-add-symbolic",
                                            label="New workspace"))
        new_btn.add_css_class("suggested-action")
        new_btn.connect("clicked", lambda _b: self._open_create_dialog())
        bar.append(new_btn)
        new_icon = Gtk.Button.new_from_icon_name("list-add-symbolic")
        new_icon.set_tooltip_text("New workspace")
        new_icon.add_css_class("suggested-action")
        new_icon.set_visible(False)
        new_icon.connect("clicked", lambda _b: self._open_create_dialog())
        bar.append(new_icon)
        view.add_top_bar(bar)
        self._narrow_setters = [(new_btn, False), (new_icon, True)]
        self._narrower_setters = [(self._face_name, False)]
        self._narrowest_setters = [(self._face_hours, False)]

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        clamp = Adw.Clamp(maximum_size=1400, tightening_threshold=1400)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        _margins(body, 12)
        body.set_margin_bottom(24)

        # Out of hours, or nearly: said above the workspaces, because that is
        # where someone is looking when Start does not work.
        self._banner, self._banner_label = self._make_banner()
        self._banner.append(_text_button("Buy hours", self._buy_hours,
                                         "suggested-action", "pill"))
        body.append(self._banner)

        # No usable SSH key: said before anyone presses SSH and reads
        # "Permission denied (publickey)" in a terminal.
        self._key_banner, self._key_label = self._make_banner("info")
        self._key_add = _text_button("Add this computer's key", self.plugin.add_this_key,
                                     "suggested-action", "pill")
        self._key_banner.append(self._key_add)
        self._key_banner.append(_text_button("SSH keys", self._open_keys_page, "flat"))
        body.append(self._key_banner)

        self._grid = Gtk.FlowBox()
        self._grid.set_selection_mode(Gtk.SelectionMode.NONE)
        self._grid.set_homogeneous(True)
        self._grid.set_min_children_per_line(1)
        self._grid.set_max_children_per_line(4)
        self._grid.set_column_spacing(14)
        self._grid.set_row_spacing(14)
        self._grid.set_valign(Gtk.Align.START)
        body.append(self._grid)

        self._empty = Adw.StatusPage(icon_name="network-server-symbolic")
        self._empty.set_visible(False)
        body.append(self._empty)

        self._status = _label("", "dim-label", "caption", wrap=True)
        body.append(self._status)

        clamp.set_child(body)
        scroller.set_child(clamp)
        view.set_content(scroller)

        # sshPilot opens at 1024px with a 300px sidebar, which leaves the page
        # about 720px: in one row at full size the bar pushed the window wider
        # than the screen. Below these widths the bar sheds labels instead:
        # New workspace's label first, then the account's name, then its hours.
        bin_ = Adw.BreakpointBin()
        bin_.set_size_request(360, 240)
        bin_.set_child(view)
        narrow = self._narrow_setters
        narrower = narrow + self._narrower_setters
        for condition, setters in (("max-width: 900sp", narrow),
                                   ("max-width: 660sp", narrower),
                                   ("max-width: 540sp", narrower + self._narrowest_setters)):
            breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(condition))
            for widget, visible in setters:
                value = GObject.Value(bool, visible)
                breakpoint.add_setter(widget, "visible", value)
            bin_.add_breakpoint(breakpoint)
        return bin_

    @staticmethod
    def _make_banner(kind=""):
        banner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        banner.add_css_class("ee-banner")
        if kind:
            banner.add_css_class(kind)
        icon = Gtk.Image.new_from_icon_name(
            "dialog-information-symbolic" if kind == "info" else "dialog-warning-symbolic")
        icon.set_valign(Gtk.Align.CENTER)
        banner.append(icon)
        label = _label("", wrap=True)
        label.set_hexpand(True)
        banner.append(label)
        banner.set_visible(False)
        return banner, label

    # --- what the plugin calls -------------------------------------------------

    def show_signed_out(self, message=""):
        self.state = None
        self._list_key = self._header_key = None
        self._stop_timers()
        self._signin_btn.set_sensitive(True)
        self._signin_btn.set_label("Sign in")
        self._gate_error.set_text(message or "")
        self._gate_error.set_visible(bool(message))
        self.root.set_visible_child_name("gate")

    def signing_in_failed(self, message):
        self.show_signed_out(message)

    def show(self, state):
        """state: {account, accounts, workspaces, fetched_at, key_status, local_key}"""
        self.state = state
        self._token_row.set_text("")
        self.root.set_visible_child_name("dashboard")
        self._render_header()
        self._render_key_banner()
        self._render_list()
        self._schedule()

    def set_status(self, text):
        self._status.set_text(text or "")

    def set_busy(self, uuid, on):
        (self._busy.add if on else self._busy.discard)(uuid)
        self._render_list(force=True)

    def set_refreshing(self, on):
        self._refresh_btn.set_sensitive(not on)

    # --- links ------------------------------------------------------------------

    def _open(self, url):
        open_uri(self.root.get_root(), url, self.plugin.notify)

    def _open_token_page(self):
        self._open(self.plugin.token_url())

    def _open_keys_page(self):
        self._open(self.plugin.keys_url())

    def _buy_hours(self):
        self._open(self.plugin.buy_hours_url())

    def open_upgrade(self):
        self._open(self.plugin.upgrade_url())

    def _sign_in(self):
        token = self._token_row.get_text().strip()
        if not token:
            self._gate_error.set_text("Paste the token from the dashboard first.")
            self._gate_error.set_visible(True)
            return
        self._signin_btn.set_sensitive(False)
        self._signin_btn.set_label("Checking the token...")
        self.plugin.sign_in(token)

    # --- header ---------------------------------------------------------------

    def _render_header(self):
        account = self.state.get("account") or {}
        accounts = self.state.get("accounts") or []
        key = json.dumps([account, accounts], sort_keys=True, default=str)
        credit = account.get("credit") or {}
        self._render_banner(credit)
        # Rebuilt only when something in it changed: rebuilding on every
        # refresh destroyed the account picker under the pointer and closed
        # its popover before anyone could choose.
        if key == self._header_key:
            return
        self._header_key = key
        self._face_avatar.set_text(account.get("initials") or "EE")
        for c in list(self._face_avatar.get_css_classes()):
            if c.startswith("ee-c"):
                self._face_avatar.remove_css_class(c)
        self._face_avatar.add_css_class(model.color_class(account.get("title")))
        self._face_name.set_text(account.get("title") or "EasyEnv")
        self._account_button.set_tooltip_text(
            f"{account.get('title') or 'EasyEnv'}: account, hours and sign out")
        self._face_hours.set_text(self._hours_text(credit))
        for c in ("error", "warning", "success"):
            self._face_hours.remove_css_class(c)
        style = self._hours_style(credit)
        if style:
            self._face_hours.add_css_class(style)
        # Hidden by default when there is nothing to say; the breakpoints only
        # ever hide it further.
        self._face_hours.set_opacity(1 if credit.get("text") else 0)
        self._account_button.set_popover(self._account_menu(account, accounts, credit))

    @staticmethod
    def _hours_text(credit):
        if credit.get("state") == "unlimited":
            return "Unlimited"
        return credit.get("text") or ""

    @staticmethod
    def _hours_style(credit):
        return {"empty": "error", "low": "warning",
                "unlimited": "success"}.get(credit.get("state"), "")

    def _hours_pill(self, credit):
        pill = _pill(self._hours_text(credit), self._hours_style(credit))
        pill.set_tooltip_text("Lab time left on this account")
        return pill

    def _account_menu(self, account, accounts, credit):
        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        _margins(box, 10)
        box.set_size_request(280, -1)

        who = Gtk.Box(spacing=10)
        avatar = _label(account.get("initials") or "EE", "ee-avatar", xalign=0.5)
        avatar.add_css_class(model.color_class(account.get("title")))
        avatar.set_valign(Gtk.Align.CENTER)
        who.append(avatar)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        name = _label(account.get("title") or "", "heading")
        name.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        name.set_max_width_chars(28)
        text.append(name)
        facts = Gtk.Box(spacing=6)
        if account.get("plan"):
            facts.append(_pill(account["plan"].upper(), "accent"))
        if credit.get("text"):
            facts.append(self._hours_pill(credit))
        text.append(facts)
        who.append(text)
        box.append(who)

        def item(label, callback, *classes, icon=None):
            button = Gtk.Button()
            button.add_css_class("flat")
            content = Gtk.Box(spacing=10)
            if icon:
                content.append(Gtk.Image.new_from_icon_name(icon))
            lbl = _label(label)
            for c in classes:
                lbl.add_css_class(c)
            content.append(lbl)
            button.set_child(content)

            def clicked(_b):
                popover.popdown()
                callback()

            button.connect("clicked", clicked)
            return button

        if credit.get("state") != "unlimited":
            buy = _text_button("Buy hours", lambda: (popover.popdown(), self._buy_hours()),
                               "suggested-action")
            buy.set_margin_top(4)
            box.append(buy)

        if len(accounts) > 1:
            box.append(Gtk.Separator())
            box.append(_label("Switch account", "caption-heading", "dim-label"))
            scroller = Gtk.ScrolledWindow()
            scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            scroller.set_max_content_height(260)
            scroller.set_propagate_natural_height(True)
            rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            for a in accounts:
                current = a["uuid"] == account.get("uuid")
                row = item(a["title"], lambda uuid=a["uuid"]: self.plugin.switch_account(uuid),
                           icon="object-select-symbolic" if current else "avatar-default-symbolic")
                row.set_sensitive(not current)
                rows.append(row)
            scroller.set_child(rows)
            box.append(scroller)

        box.append(Gtk.Separator())
        box.append(item("SSH keys on your profile", self._open_keys_page,
                        icon="dialog-password-symbolic"))
        box.append(item("Open the dashboard", lambda: self._open(self.plugin.web_url()),
                        icon="web-browser-symbolic"))
        box.append(item("Sign out", self._confirm_sign_out, "error",
                        icon="system-log-out-symbolic"))
        popover.set_child(box)
        return popover

    def _filter_chosen(self, _button, key, popover):
        popover.popdown()
        if key != self._show:
            self._show = key
            self._render_list(force=True)

    def _render_banner(self, credit):
        state = credit.get("state")
        if state == "empty":
            self._banner.remove_css_class("low")
            self._banner_label.set_text(
                "This account has no lab hours left, so workspaces cannot start. "
                "Buy more to keep going.")
        elif state == "low":
            self._banner.add_css_class("low")
            self._banner_label.set_text(f"Lab hours are running low: {credit['text']}.")
        self._banner.set_visible(state in ("empty", "low"))

    def _render_key_banner(self):
        status = self.state.get("key_status")
        local = self.state.get("local_key")
        if status == "upload" and local:
            name = local[0].rsplit("/", 1)[-1]
            self._key_label.set_text(
                f"SSH goes through EasyEnv's gateway, which needs this computer's key "
                f"on your profile. Add {name} (the public half only).")
        elif status == "none":
            self._key_label.set_text(
                "SSH goes through EasyEnv's gateway, which needs an SSH key on your "
                "profile, and this computer has none in ~/.ssh. Create one with "
                "ssh-keygen, or add the one you use.")
        self._key_add.set_visible(status == "upload")
        self._key_banner.set_visible(status in ("upload", "none"))

    def _confirm_sign_out(self):
        confirm(self.root, "Sign out of EasyEnv?",
                "The token is removed from this computer, which signs the easyenv "
                "CLI out too. Saved connections stay.",
                "Sign out", self.plugin.sign_out)

    # --- workspaces -------------------------------------------------------------

    def _render_list(self, force=False):
        if not self.state:
            return
        everything = self.state.get("workspaces") or []
        search = self._search.get_text()
        views = model.visible(everything, search, show=self._show)
        counts = model.filter_counts(everything, search)
        for key, (check, count, label) in self._filter_items.items():
            check.set_opacity(1 if key == self._show else 0)
            count.set_text(str(counts[key]))
            if key == self._show:
                self._filter_label.set_text(f"{label} {counts[key]}")
        key = json.dumps([[v["uuid"], v["title"], v["state"], v["progress"],
                           [(m["uuid"], m["title"], m["state"]) for m in v["machines"]]]
                          for v in views] + sorted(self._busy) + [self._show])
        if key == self._list_key and not force:
            self._tick()
            return
        self._list_key = key
        self._grid.remove_all()
        self._timers = []
        for v in views:
            child = Gtk.FlowBoxChild()
            child.set_focusable(False)
            child.set_child(self._card(v))
            self._grid.append(child)

        self._grid.set_visible(bool(views))
        self._empty.set_visible(not views)
        if not views:
            if self._search.get_text().strip():
                self._empty.set_title("Nothing matches")
                self._empty.set_description("Try another search.")
            elif self._show == "ended":
                self._empty.set_title("Nothing has ended")
                self._empty.set_description("Workspaces that stop or run out of time show here.")
            elif everything:
                self._empty.set_title("Nothing running")
                self._empty.set_description(
                    "Every workspace here has ended. Start a new one, or look under Ended.")
            else:
                self._empty.set_title("No workspaces yet")
                self._empty.set_description(
                    "Press New workspace to get machines you can SSH into.")
        self._tick()

    def _card(self, v):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        card.add_css_class("ee-card")
        card.set_overflow(Gtk.Overflow.HIDDEN)
        card.set_size_request(270, 160)
        card.set_cursor(Gdk.Cursor.new_from_name("pointer"))
        card.set_tooltip_text("Click for machines, sizes and stacks")
        # Buttons on the card claim their own clicks, so this sees only the rest.
        click = Gtk.GestureClick()
        click.connect("released", lambda *_a: self._open_details(v))
        card.add_controller(click)

        if v["state"] == model.STARTING and v["progress"]:
            strip = Gtk.ProgressBar(fraction=v["progress"])
            strip.add_css_class("ee-progress")
        else:
            strip = Gtk.Box()
            strip.add_css_class("ee-strip")
            strip.add_css_class(v["style"])
        card.append(strip)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        inner.set_vexpand(True)
        inner.set_margin_top(14)
        inner.set_margin_bottom(14)
        inner.set_margin_start(16)
        inner.set_margin_end(10)
        card.append(inner)

        top = Gtk.Box(spacing=8)
        badge = Gtk.Box(spacing=6)
        badge.add_css_class("ee-badge")
        badge.add_css_class(v["style"])
        badge.set_valign(Gtk.Align.CENTER)
        if v["state"] == model.STARTING:
            spinner = _spinner()
            spinner.set_size_request(12, 12)
            badge.append(spinner)
        badge.append(Gtk.Label(label=v["label"]))
        top.append(badge)
        when = _label("", "dim-label", "caption")
        when.set_valign(Gtk.Align.CENTER)
        top.append(when)
        self._timers.append((when, v))
        top.append(_spacer())
        top.append(self._card_menu(v))
        inner.append(top)

        title = _label(v["title"], "ee-card-title")
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_max_width_chars(24)
        title.set_tooltip_text(v["title"])
        inner.append(title)
        sub = _label(model.subtitle(v, datetime.now(timezone.utc)), "dim-label", "caption")
        sub.set_ellipsize(Pango.EllipsizeMode.END)
        sub.set_max_width_chars(30)
        inner.append(sub)

        bottom = Gtk.Box(spacing=6)
        bottom.set_vexpand(True)
        bottom.set_valign(Gtk.Align.END)
        bottom.set_margin_top(6)
        # The grid is homogeneous, so the widest card sets every card's
        # width: a workspace with ten logos turned three columns into two.
        # In a scroller that does not pass its width on, the row takes what
        # is left and clips the rest.
        tech = Gtk.ScrolledWindow()
        tech.set_policy(Gtk.PolicyType.EXTERNAL, Gtk.PolicyType.NEVER)
        tech.set_propagate_natural_height(True)
        tech.set_hexpand(True)
        tech.set_child(self._tech_row(v))
        bottom.append(tech)
        if v["uuid"] in self._busy:
            bottom.append(_spinner())
        else:
            self._add_actions(bottom, v)
        inner.append(bottom)
        return card

    def _tech_row(self, v):
        """Recipe logos, then stack logos after a thin rule, as many as fit
        in CARD_LOGOS; the rest become "+N". The tooltip names them all."""
        row = Gtk.Box(spacing=3)
        row.set_valign(Gtk.Align.CENTER)
        items = [(t, u, False) for t, u in v["logos"]] + [(t, u, True) for t, u in v["stacks"]]
        shown = items[:CARD_LOGOS]
        for i, (title, url, is_stack) in enumerate(shown):
            if is_stack and i > 0 and not shown[i - 1][2]:
                rule = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
                rule.set_margin_start(3)
                rule.set_margin_end(3)
                row.append(rule)
            chip = self.logo_chip(url, title)
            if is_stack:
                chip.add_css_class("ee-stack")
            row.append(chip)
        if len(items) > CARD_LOGOS:
            more = _label(f"+{len(items) - CARD_LOGOS}", "ee-more", xalign=0.5)
            more.set_valign(Gtk.Align.CENTER)
            row.append(more)
        lines = []
        if v["logos"]:
            lines.append("Recipes: " + ", ".join(t for t, _u in v["logos"]))
        if v["stacks"]:
            lines.append("Stacks: " + ", ".join(t for t, _u in v["stacks"]))
        row.set_tooltip_text("\n".join(lines) or None)
        return row

    def _card_menu(self, v):
        items = [("Details", lambda: self._open_details(v), False)]
        running = self._running_machines(v)
        if len(running) == 1:
            items.append(("Copy ssh command", lambda: self.copy_command(running[0]), False))
        items.append(("Open in dashboard", lambda: self._open(self.plugin.workspace_url(v["uuid"])), False))
        if v["state"] in (model.ENDED, model.FAILED, model.NOT_STARTED):
            items.append(("Run again as new" if v["state"] != model.NOT_STARTED else "Clone",
                          lambda: self.plugin.run_again(v), False))
        items.append(("Delete", lambda: self._confirm_delete(v), True))
        return _menu(items)

    @staticmethod
    def _running_machines(v):
        if v["state"] != model.RUNNING:
            return []
        return [m for m in v["machines"] if m["state"] == model.RUNNING]

    def _add_actions(self, box, v):
        state = v["state"]
        if state == model.RUNNING:
            box.append(_text_button("Stop", lambda: self._confirm_stop(v), "flat", "ee-stop"))
            running = self._running_machines(v)
            if len(running) == 1:
                ssh = _text_button("SSH", lambda: self.plugin.ssh(v, running[0]),
                                   "suggested-action")
                ssh.set_tooltip_text(f"Open a terminal on {running[0]['title']}")
                box.append(ssh)
            elif running:
                box.append(self._ssh_chooser(v, running))
        elif state == model.STARTING:
            box.append(_text_button("Cancel", lambda: self._confirm_stop(v), "flat"))
        elif state == model.NOT_STARTED:
            box.append(_text_button("Start", lambda: self.plugin.start(v), "suggested-action"))
        else:
            box.append(_text_button("Run again", lambda: self.plugin.run_again(v), "flat"))

    def _ssh_chooser(self, v, machines):
        """SSH for a workspace with several machines: pick one."""
        button = Gtk.MenuButton()
        button.set_child(Adw.ButtonContent(icon_name="pan-down-symbolic", label="SSH"))
        button.add_css_class("suggested-action")
        button.set_valign(Gtk.Align.CENTER)
        popover = Gtk.Popover()
        rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        _margins(rows, 8)
        for m in machines:
            row = Gtk.Box(spacing=8)
            row.append(self.logo_chip(m["logo"], m["recipe"]))
            name = _label(m["title"])
            name.set_hexpand(True)
            name.set_ellipsize(Pango.EllipsizeMode.END)
            name.set_max_width_chars(24)
            row.append(name)
            row.append(_icon_button("edit-copy-symbolic", "Copy ssh command",
                                    lambda m=m: (popover.popdown(), self.copy_command(m))))

            def go(m=m):
                popover.popdown()
                self.plugin.ssh(v, m)

            row.append(_text_button("SSH", go, "suggested-action"))
            rows.append(row)
        popover.set_child(rows)
        button.set_popover(popover)
        return button

    def copy_command(self, m):
        copy_text(self.root, connections.ssh_command_line(m["uuid"], m["user"]))
        self.plugin.notify("ssh command copied")

    def _confirm_stop(self, v):
        confirm(self.root, f"Stop {v['title']}?",
                "Its machines are shut down and removed. A stopped workspace "
                "cannot be started again, only run again as a new one.",
                "Stop", lambda: self.plugin.stop(v))

    def _confirm_delete(self, v):
        confirm(self.root, f"Delete {v['title']}?",
                "The workspace and its machines are removed from EasyEnv.",
                "Delete", lambda: self.plugin.delete(v))

    # --- timers -----------------------------------------------------------------

    def _on_map(self):
        if self.state is None:
            self.plugin.refresh()
        self._schedule()

    def _schedule(self):
        """One-second tick while a running workspace shows its time, and the
        next refresh, both only while the page is on screen."""
        if not self.root.get_mapped() or not self.state:
            return
        views = self.state.get("workspaces") or []
        if self._tick_id is None and any(v["state"] == model.RUNNING for v in views):
            self._tick_id = GLib.timeout_add_seconds(1, self._tick_cb)
        if self._refresh_id is not None:
            GLib.source_remove(self._refresh_id)
        starting = any(v["state"] == model.STARTING for v in views)
        self._refresh_id = GLib.timeout_add_seconds(
            FAST_REFRESH_SECONDS if starting else SLOW_REFRESH_SECONDS, self._refresh_cb)

    def _stop_timers(self):
        for attr in ("_tick_id", "_refresh_id"):
            source = getattr(self, attr)
            if source is not None:
                GLib.source_remove(source)
                setattr(self, attr, None)

    def _refresh_cb(self):
        self._refresh_id = None
        self.plugin.refresh()
        return GLib.SOURCE_REMOVE

    def _tick_cb(self):
        if not self._tick():
            self._tick_id = None
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _tick(self):
        now = datetime.now(timezone.utc)
        ticking = False
        for label, v in self._timers:
            label.set_text(model.time_text(v, now))
            ticking = ticking or v["state"] == model.RUNNING
        return ticking

    # --- dialogs -------------------------------------------------------------------

    def _open_create_dialog(self):
        from .dialogs import NewWorkspaceDialog
        NewWorkspaceDialog(self).present()

    def _open_details(self, v):
        from .dialogs import DetailsDialog
        DetailsDialog(self, v).present()
