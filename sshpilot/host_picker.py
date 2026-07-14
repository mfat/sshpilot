"""Reusable host-picker popover.

A searchable list of saved connections (the "host lookup" popover used by the
command snippets sidebar). Call :func:`show_host_picker` with an anchor widget
and an ``on_selected(connection)`` callback to reuse it anywhere.
"""

from gi.repository import Gtk, GLib
from gettext import gettext as _


def show_host_picker(window, anchor, on_selected, *, toast=None,
                     connections=None):
    """Pop up a searchable list of the saved connections, anchored at *anchor*.

    Args:
        window: the main window (provides ``connection_manager`` and
            ``active_terminals``). May be ``None`` when *connections* is given.
        anchor: the widget the popover points to.
        on_selected: callable invoked with the chosen ``connection`` when the
            user picks a row.
        toast: optional callable(str) used to warn when there are no hosts.
        connections: optional pre-filtered connection list; defaults to every
            connection in the window's ``connection_manager``.

    Returns the ``Gtk.Popover`` (already scheduled to pop up), or ``None``.
    """
    if connections is None:
        cm = getattr(window, 'connection_manager', None) if window is not None else None
        if cm is None:
            return None
        connections = list(getattr(cm, 'connections', []))
    else:
        connections = list(connections)

    if not connections:
        if toast:
            toast(_('No connections in inventory'))
        return None

    active_terminals = (getattr(window, 'active_terminals', {})
                        if window is not None else {})

    popover = Gtk.Popover()
    popover.set_parent(anchor)
    popover.set_has_arrow(True)

    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    outer.set_margin_top(8)
    outer.set_margin_bottom(8)
    outer.set_margin_start(8)
    outer.set_margin_end(8)
    outer.set_size_request(280, -1)

    search_entry = Gtk.SearchEntry()
    search_entry.set_placeholder_text(_('Filter hosts…'))
    outer.append(search_entry)

    scrolled = Gtk.ScrolledWindow()
    scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scrolled.set_size_request(-1, min(300, len(connections) * 56 + 8))

    list_box = Gtk.ListBox()
    list_box.set_selection_mode(Gtk.SelectionMode.SINGLE)
    list_box.add_css_class('boxed-list')

    for conn in connections:
        is_open = conn in active_terminals
        list_row = Gtk.ListBoxRow()
        row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row_box.set_margin_top(6)
        row_box.set_margin_bottom(6)
        row_box.set_margin_start(8)
        row_box.set_margin_end(8)

        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        info.set_hexpand(True)
        display_name = getattr(conn, 'display_name', None) or conn.nickname
        lbl = Gtk.Label(label=display_name)
        lbl.set_halign(Gtk.Align.START)
        lbl.add_css_class('heading')
        info.append(lbl)
        host = getattr(conn, 'hostname', '') or getattr(conn, 'host', '')
        user = getattr(conn, 'username', '')
        subtitle = f"{user}@{host}" if user and host else host
        if subtitle:
            lbl2 = Gtk.Label(label=subtitle)
            lbl2.set_halign(Gtk.Align.START)
            lbl2.add_css_class('caption')
            lbl2.add_css_class('dim-label')
            info.append(lbl2)
        row_box.append(info)

        if is_open:
            dot = Gtk.Image.new_from_icon_name('media-record-symbolic')
            dot.set_pixel_size(10)
            dot.set_valign(Gtk.Align.CENTER)
            dot.add_css_class('success')
            row_box.append(dot)

        list_row.set_child(row_box)
        list_row._connection = conn
        list_box.append(list_row)

    def _filter(list_row):
        q = search_entry.get_text().lower().strip()
        if not q:
            return True
        conn = getattr(list_row, '_connection', None)
        if conn is None:
            return False
        host = getattr(conn, 'hostname', '') or getattr(conn, 'host', '')
        display_name = getattr(conn, 'display_name', None) or conn.nickname
        return q in display_name.lower() or q in conn.nickname.lower() or q in host.lower()

    list_box.set_filter_func(_filter)
    search_entry.connect('search-changed', lambda _e: list_box.invalidate_filter())

    def _on_activated(_lb, list_row):
        conn = getattr(list_row, '_connection', None)
        if conn:
            popover.popdown()
            on_selected(conn)

    list_box.connect('row-activated', _on_activated)
    scrolled.set_child(list_box)
    outer.append(scrolled)
    popover.set_child(outer)
    # set_parent() alone leaves the popover attached to the anchor forever,
    # which warns at finalization; detach once closed.
    popover.connect('closed', lambda p: GLib.idle_add(p.unparent))

    def _popup():
        popover.popup()
        search_entry.grab_focus()

    GLib.idle_add(_popup)
    return popover
