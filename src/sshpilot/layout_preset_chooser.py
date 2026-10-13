"""The layout preset cards on Settings › Appearance (see layout_presets.py)."""

from gettext import gettext as _

import gi

gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, Pango

from . import icon_utils
from .layout_presets import apply_preset, current_preset

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


def _sketch(preset_id):
    """A miniature of the main window as *preset_id* opens it."""
    frame = Gtk.Box()
    frame.add_css_class('layout-sketch')
    frame.set_overflow(Gtk.Overflow.HIDDEN)
    if preset_id == 'sidebar':
        frame.append(_sidebar_sketch())
        frame.append(_start_page_sketch())
    elif preset_id == 'startpage':
        frame.set_orientation(Gtk.Orientation.VERTICAL)
        frame.append(_header_sketch(
            _bar('layout-sketch-button', 6, 5), _bar('layout-sketch-button', 6, 5)
        ))
        frame.append(_start_page_sketch())
    else:
        frame.set_orientation(Gtk.Orientation.VERTICAL)
        frame.append(_header_sketch(_bar('layout-sketch-tab', 48, 7), center=True))
        terminal = Gtk.Label(xalign=0, yalign=0)
        terminal.add_css_class('layout-sketch-terminal')
        terminal.set_use_markup(True)
        terminal.set_markup(
            '<span foreground="#4e9a06">user@laptop</span>'
            '<span foreground="#d3d7cf">:~$ ▯</span>'
        )
        terminal.set_vexpand(True)
        terminal.set_ellipsize(Pango.EllipsizeMode.END)
        frame.append(terminal)
    aspect = Gtk.AspectFrame(ratio=1.6, obey_child=False)
    aspect.set_child(frame)
    return aspect


class LayoutPresetChooser(Gtk.Box):
    """Three cards that each set the main window's startup layout."""

    def __init__(self, config):
        super().__init__(spacing=12, homogeneous=True)
        self.add_css_class('layout-presets')
        self._config = config
        self._syncing = False
        self._cards = {}
        self._indicators = {}
        presets = (
            ('sidebar', _("Sidebar and Start Page"), _("Default")),
            ('startpage', _("Start Page Only"), _("Sidebar hidden")),
            ('terminal', _("Plain Terminal"), _("Opens a local shell")),
        )
        first_card = first_indicator = None
        for preset_id, title, caption in presets:
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

            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            # Top-aligned, so a wrapped title elsewhere cannot shift this sketch.
            content.set_valign(Gtk.Align.START)
            content.append(_sketch(preset_id))
            content.append(footer)
            card.set_child(content)
            card.update_property([Gtk.AccessibleProperty.LABEL], [title])
            card.update_property([Gtk.AccessibleProperty.DESCRIPTION], [caption])
            card.connect('toggled', self._on_card_toggled, preset_id)
            self.append(card)
            self._cards[preset_id] = card
            self._indicators[preset_id] = indicator
        self.sync()

    def sync(self):
        """Select the card the current settings match (none when custom)."""
        selected = current_preset(self._config)
        self._syncing = True
        try:
            for preset_id, card in self._cards.items():
                active = preset_id == selected
                card.set_active(active)
                self._indicators[preset_id].set_active(active)
        finally:
            self._syncing = False

    def _on_card_toggled(self, card, preset_id):
        self._indicators[preset_id].set_active(card.get_active())
        if self._syncing or not card.get_active():
            return
        apply_preset(self._config, preset_id)
