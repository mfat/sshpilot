"""Group context shared by the two frontend search presentations."""

from html import escape


def connection_group_context(manager, connection):
    """Return every membership's breadcrumb and inherited sidebar color."""
    if manager is None:
        return ()
    result = []
    for group_id in manager.get_connection_groups(connection):
        names, seen, color = [], set(), None
        while group_id and group_id not in seen:
            seen.add(group_id)
            group = manager.groups.get(group_id)
            if group is None:
                break
            names.append(str(group.get("name", "")))
            color = group.get("color") or color
            group_id = group.get("parent_id")
        if names:
            result.append((" › ".join(reversed(names)), color))
    return tuple(result)


def make_group_context_label(manager, connection):
    from gi.repository import Gdk, Gtk

    context = connection_group_context(manager, connection)
    if not context:
        return None
    parts = []
    for name, color in context:
        rgba = Gdk.RGBA()
        marker = ""
        if color and rgba.parse(str(color)):
            rgb = "#{:02x}{:02x}{:02x}".format(
                round(rgba.red * 255), round(rgba.green * 255), round(rgba.blue * 255)
            )
            marker = f'<span foreground="{rgb}">●</span> '
        parts.append(marker + escape(name))
    label = Gtk.Label(xalign=0)
    label.set_markup(" · ".join(parts))
    label.add_css_class("caption")
    label.set_ellipsize(3)
    label.set_hexpand(True)
    label.set_tooltip_text("\n".join(name for name, _color in context))
    return label
