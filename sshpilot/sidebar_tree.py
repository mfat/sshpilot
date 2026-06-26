"""
Native GTK4 tree view for the sidebar using Gtk.TreeListModel + Gtk.ListView.

This is an alternative to the Gtk.ListBox-based sidebar. It is read-only
(no drag-and-drop) and toggled via a toolbar button.
"""

import logging
from gi.repository import Gtk, Gio, GObject, Pango

logger = logging.getLogger(__name__)


class SidebarGroupItem(GObject.Object):
    __gtype_name__ = 'SidebarGroupItem'

    def __init__(self, group_id, group_info):
        super().__init__()
        self.group_id = group_id
        self.group_info = group_info


class SidebarConnectionItem(GObject.Object):
    __gtype_name__ = 'SidebarConnectionItem'

    def __init__(self, connection):
        super().__init__()
        self.connection = connection


class _TreeRow(Gtk.Box):
    """Reusable row widget for the ListView factory (groups and connections)."""

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        self.set_hexpand(True)

        self.expander = Gtk.TreeExpander()
        self.expander.set_hexpand(True)
        self.append(self.expander)

        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        content.set_margin_start(4)
        content.set_margin_end(8)
        content.set_margin_top(5)
        content.set_margin_bottom(5)
        content.set_hexpand(True)

        self.icon = Gtk.Image()
        self.icon.set_pixel_size(16)
        content.append(self.icon)

        label_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        label_box.set_valign(Gtk.Align.CENTER)
        label_box.set_hexpand(True)

        self.label = Gtk.Label()
        self.label.set_xalign(0)
        self.label.set_hexpand(True)
        self.label.set_ellipsize(Pango.EllipsizeMode.END)
        label_box.append(self.label)

        self.sublabel = Gtk.Label()
        self.sublabel.set_xalign(0)
        self.sublabel.set_hexpand(True)
        self.sublabel.set_ellipsize(Pango.EllipsizeMode.END)
        self.sublabel.add_css_class('caption')
        self.sublabel.add_css_class('dim-label')
        label_box.append(self.sublabel)

        content.append(label_box)
        self.expander.set_child(content)


def _on_setup(factory, list_item):
    list_item.set_child(_TreeRow())


def _on_bind(factory, list_item, window):
    tree_row = list_item.get_item()   # Gtk.TreeListRow
    if tree_row is None:
        return
    item = tree_row.get_item()        # SidebarGroupItem | SidebarConnectionItem
    if item is None:
        return
    row = list_item.get_child()       # _TreeRow

    row.expander.set_list_row(tree_row)

    if isinstance(item, SidebarGroupItem):
        row.icon.set_from_icon_name('folder-symbolic')
        row.label.set_text(item.group_info.get('name', ''))
        row.label.remove_css_class('dim-label')
        count = len(item.group_info.get('connections', []))
        if count:
            row.sublabel.set_text(f'{count} connection{"s" if count != 1 else ""}')
            row.sublabel.set_visible(True)
        else:
            row.sublabel.set_text('')
            row.sublabel.set_visible(False)
    else:
        conn = item.connection
        row.icon.set_from_icon_name('computer-symbolic')
        row.label.set_text(conn.nickname or '')
        row.label.remove_css_class('dim-label')
        subtitle = ''
        if conn.username:
            subtitle = f'{conn.username}@{conn.hostname or conn.host}'
        elif conn.hostname or conn.host:
            subtitle = conn.hostname or conn.host
        row.sublabel.set_text(subtitle)
        row.sublabel.set_visible(bool(subtitle))


def _on_unbind(factory, list_item):
    row = list_item.get_child()
    if row and hasattr(row, 'expander') and row.expander:
        row.expander.set_list_row(None)


def _on_activate(listview, position, window):
    model = listview.get_model()
    tree_row = model.get_item(position)
    if tree_row is None:
        return
    item = tree_row.get_item()
    if isinstance(item, SidebarConnectionItem):
        try:
            window._return_to_tab_view_if_welcome()
            window._cycle_connection_tabs_or_open(item.connection)
        except Exception as e:
            logger.error('Tree view connection activate error: %s', e)
    elif isinstance(item, SidebarGroupItem):
        tree_row.set_expanded(not tree_row.get_expanded())


def build_tree_model(window):
    """Build a Gtk.TreeListModel from the window's current groups/connections."""
    gm = window.group_manager
    conns_by_nickname = {
        c.nickname: c
        for c in window.connection_manager.get_connections()
    }

    root = Gio.ListStore.new(GObject.Object)

    # Root-level groups in order
    for gid in (gm.get_ordered_siblings(None) or []):
        ginfo = gm.groups.get(gid)
        if ginfo:
            root.append(SidebarGroupItem(gid, ginfo))

    # Ungrouped connections
    for nickname in (gm.root_connections or []):
        conn = conns_by_nickname.get(nickname)
        if conn:
            root.append(SidebarConnectionItem(conn))

    def create_children(item):
        if not isinstance(item, SidebarGroupItem):
            return None
        gid = item.group_id
        ginfo = item.group_info
        children = Gio.ListStore.new(GObject.Object)

        # Child groups in order
        for cgid in (gm.get_ordered_siblings(gid) or []):
            cginfo = gm.groups.get(cgid)
            if cginfo:
                children.append(SidebarGroupItem(cgid, cginfo))

        # Connections in this group
        for nickname in (ginfo.get('connections') or []):
            conn = conns_by_nickname.get(nickname)
            if conn:
                children.append(SidebarConnectionItem(conn))

        return children if children.get_n_items() > 0 else None

    return Gtk.TreeListModel.new(root, False, False, create_children)


def build_tree_list_view(window, tree_model):
    """Build a Gtk.ListView backed by the given TreeListModel."""
    factory = Gtk.SignalListItemFactory()
    factory.connect('setup', _on_setup)
    factory.connect('bind', lambda f, li: _on_bind(f, li, window))
    factory.connect('unbind', _on_unbind)

    selection = Gtk.SingleSelection.new(tree_model)
    selection.set_autoselect(False)

    listview = Gtk.ListView.new(selection, factory)
    listview.add_css_class('navigation-sidebar')
    listview.set_single_click_activate(False)
    listview.connect('activate', lambda lv, pos: _on_activate(lv, pos, window))

    return listview
