"""The layout preset cards on Settings › Interface (see layout_presets.py)."""

from gettext import gettext as _

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gtk, Pango

from . import icon_utils
from .layout_presets import apply_view, current_view, set_sidebar_shown, sidebar_shown

# Group colours of the sidebar sketch: (dot colour, connection rows below it).
_SKETCH_GROUPS = (('#ed333b', 1), ('#ff7800', 1), ('#9141ac', 0), ('#f6d32d', 0))


def _bar(css_class, width=-1, height=3, halign=Gtk.Align.FILL):
    bar = Gtk.Box()
    bar.add_css_class(css_class)
    bar.set_size_request(width, height)
    bar.set_halign(halign)
    return bar


def _start_page_sketch():
    """App logo, search entry and a few result lines."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
    box.set_valign(Gtk.Align.CENTER)
    box.set_hexpand(True)
    box.set_vexpand(True)
    box.set_margin_start(10)
    box.set_margin_end(10)
    logo = icon_utils.new_image_from_icon_name('io.github.mfat.sshpilot', 18)
    box.append(logo)
    box.append(_bar('layout-sketch-search', 56, 7, Gtk.Align.CENTER))
    lines = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
    lines.set_halign(Gtk.Align.CENTER)
    for _i in range(3):
        lines.append(_bar('layout-sketch-faint', 64, 2))
    box.append(lines)
    return box


def _sidebar_sketch():
    sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
    sidebar.add_css_class('layout-sketch-sidebar')
    sidebar.set_size_request(40, -1)
    sidebar.append(_bar('layout-sketch-strong', 16, 3, Gtk.Align.CENTER))
    for color, children in _SKETCH_GROUPS:
        row = Gtk.Box(spacing=3)
        dot = _bar('layout-sketch-dot', 4, 4, Gtk.Align.CENTER)
        dot.add_css_class(f'layout-sketch-dot-{color[1:]}')
        row.append(dot)
        row.append(_bar('layout-sketch-line', 18, 3, Gtk.Align.CENTER))
        sidebar.append(row)
        for _i in range(children):
            child = _bar('layout-sketch-faint', 16, 3, Gtk.Align.START)
            child.set_margin_start(6)
            sidebar.append(child)
    return sidebar


def _header_sketch(*widgets, center=False):
    header = Gtk.Box(spacing=4)
    header.set_size_request(-1, 12)
    header.set_margin_start(7)
    header.set_margin_end(7)
    header.set_halign(Gtk.Align.CENTER if center else Gtk.Align.FILL)
    for widget in widgets:
        widget.set_valign(Gtk.Align.CENTER)
        header.append(widget)
    return header


def _sketch(view_id, with_sidebar):
    """A miniature of the main window as it opens."""
    if view_id == 'startpage':
        body = _start_page_sketch()
    else:
        body = Gtk.Label(xalign=0, yalign=0)
        body.add_css_class('layout-sketch-terminal')
        body.set_use_markup(True)
        body.set_markup(
            '<span foreground="#4e9a06">user@laptop</span>'
            '<span foreground="#d3d7cf">:~$ ▯</span>'
        )
        body.set_vexpand(True)
        body.set_hexpand(True)
        body.set_ellipsize(Pango.EllipsizeMode.END)
    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    content.set_hexpand(True)
    if view_id == 'terminal':
        content.append(_header_sketch(_bar('layout-sketch-tab', 40, 7), center=True))
    elif not with_sidebar:
        content.append(_header_sketch(
            _bar('layout-sketch-button', 6, 5), _bar('layout-sketch-button', 6, 5)
        ))
    content.append(body)

    frame = Gtk.Box()
    frame.add_css_class('layout-sketch')
    frame.set_overflow(Gtk.Overflow.HIDDEN)
    if with_sidebar:
        frame.append(_sidebar_sketch())
    frame.append(content)
    aspect = Gtk.AspectFrame(ratio=1.6, obey_child=False)
    aspect.set_child(frame)
    return aspect


class LayoutPresetChooser(Gtk.Box):
    """View cards (Start Page / Terminal) plus a sidebar switch below them."""

    def __init__(self, config):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.add_css_class('layout-presets')
        self._config = config
        self._syncing = False
        self._cards = {}
        self._indicators = {}
        self._sketch_slots = {}
        cards_box = Gtk.Box(spacing=12, homogeneous=True)
        views = (
            ('startpage', _("Start Page"), _("Search and recent connections")),
            ('terminal', _("Terminal"), _("Opens a local shell")),
        )
        first_card = first_indicator = None
        for view_id, title, caption in views:
            card = Gtk.ToggleButton()
            card.add_css_class('layout-card')
            card.set_tooltip_text(caption)
            indicator = Gtk.CheckButton()
            indicator.set_can_target(False)
            indicator.set_can_focus(False)
            indicator.set_valign(Gtk.Align.START)
            if first_card is None:
                first_card, first_indicator = card, indicator
            else:
                card.set_group(first_card)
                indicator.set_group(first_indicator)

            title_label = Gtk.Label(label=title, xalign=0, wrap=True)
            title_label.add_css_class('heading')
            caption_label = Gtk.Label(label=caption, xalign=0, wrap=True)
            caption_label.add_css_class('caption')
            caption_label.add_css_class('dim-label')
            labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            labels.append(title_label)
            labels.append(caption_label)
            footer = Gtk.Box(spacing=8)
            footer.set_margin_start(4)
            footer.set_margin_end(4)
            footer.set_margin_bottom(4)
            footer.append(indicator)
            footer.append(labels)

            slot = Gtk.Box()
            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            # Top-aligned, so a wrapped title elsewhere cannot shift this sketch.
            content.set_valign(Gtk.Align.START)
            content.append(slot)
            content.append(footer)
            card.set_child(content)
            card.update_property([Gtk.AccessibleProperty.LABEL], [title])
            card.update_property([Gtk.AccessibleProperty.DESCRIPTION], [caption])
            card.connect('toggled', self._on_card_toggled, view_id)
            cards_box.append(card)
            self._cards[view_id] = card
            self._indicators[view_id] = indicator
            self._sketch_slots[view_id] = slot
        self.append(cards_box)

        sidebar_list = Gtk.ListBox()
        sidebar_list.add_css_class('boxed-list')
        sidebar_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.sidebar_switch = Adw.SwitchRow()
        self.sidebar_switch.set_title(_("Show Sidebar"))
        self.sidebar_switch.set_subtitle(_("Open the main window with the sidebar visible"))
        self.sidebar_switch.connect('notify::active', self._on_sidebar_toggled)
        sidebar_list.append(self.sidebar_switch)
        self.append(sidebar_list)
        self._shown_sidebar = None
        self.sync()

    def sync(self):
        """Show the current settings: the view card (none for a restored
        session), the sidebar switch, and sketches with or without it."""
        view = current_view(self._config)
        shown = sidebar_shown(self._config)
        self._syncing = True
        try:
            for view_id, card in self._cards.items():
                active = view_id == view
                card.set_active(active)
                self._indicators[view_id].set_active(active)
            self.sidebar_switch.set_active(shown)
        finally:
            self._syncing = False
        if shown != self._shown_sidebar:
            self._shown_sidebar = shown
            for view_id, slot in self._sketch_slots.items():
                old = slot.get_first_child()
                if old is not None:
                    slot.remove(old)
                sketch = _sketch(view_id, shown)
                sketch.set_hexpand(True)
                slot.append(sketch)

    def _on_card_toggled(self, card, view_id):
        self._indicators[view_id].set_active(card.get_active())
        if self._syncing or not card.get_active():
            return
        apply_view(self._config, view_id)

    def _on_sidebar_toggled(self, switch, _param):
        if self._syncing:
            return
        set_sidebar_shown(self._config, switch.get_active())
        self.sync()
