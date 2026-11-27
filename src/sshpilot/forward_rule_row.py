"""A port-forwarding rule as one row of the connection editor's list.

This computer is always on the left and the server on the right; the arrow
between them points the way a forwarded connection travels (right for Local
and Dynamic, left for Remote). The two sides face each other across the arrow
-- the left one set flush right, its icon next to the arrow -- so the arrow
keeps one length and one gap on either side while lining up down the list.
Each side shows the machine over the address
it plays a part with: an ear where the forward listens, a target where the
connection is delivered. The type and its ssh flag sit on a line above,
edit and delete on the right.

``describe_forward`` is the pure part (what to show for a rule); the widget
builders below only lay it out.
"""
from __future__ import annotations

from dataclasses import dataclass
from gettext import gettext as _
from typing import Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gtk, Pango  # noqa: E402

#: The machine a side belongs to.
THIS_COMPUTER = "local"
SERVER = "server"

#: What a side does in the forward.
LISTENS = "listen"
DELIVERS = "target"

_DEFAULT_BIND = "localhost"


@dataclass(frozen=True)
class Side:
    address: str
    role: str  # LISTENS or DELIVERS


@dataclass(frozen=True)
class ForwardView:
    #: CSS suffix and colour: local, remote, dynamic, remote-socks.
    kind: str
    title: str
    #: The ssh option this rule amounts to, e.g. ``-L 5432:db.internal:5432``.
    flag: str
    this_computer: Side
    server: Side
    #: True when connections travel from this computer to the server.
    outbound: bool


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def _host(host: str) -> str:
    """*host* as ssh reads it in a forward: an IPv6 address in brackets, as
    the config writer (``_format_forward_host``) puts it."""
    if ":" in host and not (host.startswith("[") and host.endswith("]")):
        return f"[{host}]"
    return host


def _address(host: str, port: str) -> str:
    return f"{_host(host)}:{port}"


def _bind_prefix(addr: str) -> str:
    """``addr:`` for the flag, or nothing when ssh's default (localhost) is
    meant -- the editor fills that in, and repeating it only lengthens the
    flag."""
    return f"{_host(addr)}:" if addr and addr != _DEFAULT_BIND else ""


def describe_forward(rule: dict) -> Optional[ForwardView]:
    """What the row shows for *rule*; None for a type it does not know."""
    kind = _text(rule.get("type"))
    addr = _text(rule.get("listen_addr"))
    port = _text(rule.get("listen_port"))
    shown_bind = addr or _DEFAULT_BIND

    if kind == "local":
        host = _text(rule.get("remote_host"))
        hport = _text(rule.get("remote_port"))
        return ForwardView(
            kind="local",
            title=_("Local"),
            flag=f"-L {_bind_prefix(addr)}{port}:{_address(host, hport)}",
            this_computer=Side(_address(shown_bind, port), LISTENS),
            server=Side(_address(host, hport), DELIVERS),
            outbound=True,
        )

    if kind == "remote":
        # Same fallbacks as the rule editor: the destination is local_host /
        # local_port, older rules kept it in remote_host / remote_port, and a
        # rule with neither (or flagged socks) is ssh's single-argument -R.
        host = _text(rule.get("local_host")) or _text(rule.get("remote_host"))
        hport = _text(rule.get("local_port")) or _text(rule.get("remote_port"))
        if rule.get("socks") or not (host or hport):
            return ForwardView(
                kind="remote-socks",
                title=_("Remote SOCKS"),
                flag=f"-R {_bind_prefix(addr)}{port}",
                this_computer=Side(_("any host"), DELIVERS),
                server=Side(_("SOCKS {address}").format(address=_address(shown_bind, port)), LISTENS),
                outbound=False,
            )
        return ForwardView(
            kind="remote",
            title=_("Remote"),
            flag=f"-R {_bind_prefix(addr)}{port}:{_address(host, hport)}",
            this_computer=Side(_address(host, hport), DELIVERS),
            server=Side(_address(shown_bind, port), LISTENS),
            outbound=False,
        )

    if kind == "dynamic":
        return ForwardView(
            kind="dynamic",
            title=_("Dynamic"),
            flag=f"-D {_bind_prefix(addr)}{port}",
            this_computer=Side(_("SOCKS {address}").format(address=_address(shown_bind, port)), LISTENS),
            server=Side(_("any host"), DELIVERS),
            outbound=True,
        )

    return None


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------

_CSS = """
.forward-rule { padding: 10px 8px 10px 14px; }
.forward-title { font-weight: 600; }
.forward-strip {
  padding: 8px 14px;
  border-radius: 10px;
  background-color: alpha(currentColor, 0.04);
  border: 1px solid alpha(currentColor, 0.06);
}
.forward-local { color: @blue_3; }
.forward-remote { color: @orange_4; }
.forward-dynamic { color: @green_5; }
.forward-remote-socks { color: @purple_3; }
"""
_css_installed = False

#: Arrow length in px: the same on every row, so the rows line up.
ARROW_WIDTH = 36
#: Widest the left side's address gets, in characters: past it the address
#: ends in an ellipsis (the full text is its tooltip), so one long address
#: does not widen every row through the size group.
LEFT_ADDRESS_CHARS = 20


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


def _image(name: str, size: int) -> Gtk.Image:
    from sshpilot import icon_utils

    image = icon_utils.new_image_from_icon_name(name)
    image.set_pixel_size(size)
    return image


def _draw_arrow(area: Gtk.DrawingArea, cr, width: int, height: int, outbound: bool) -> None:
    colour = area.get_color()
    cr.set_source_rgba(colour.red, colour.green, colour.blue, colour.alpha)
    cr.set_line_width(1.6)
    cr.set_line_cap(1)  # round
    cr.set_line_join(1)  # round
    mid = height / 2
    head = 4
    if outbound:
        cr.move_to(1, mid)
        cr.line_to(width - 1.5, mid)
        cr.move_to(width - 1.5 - head, mid - head)
        cr.line_to(width - 1.5, mid)
        cr.line_to(width - 1.5 - head, mid + head)
    else:
        cr.move_to(width - 1, mid)
        cr.line_to(1.5, mid)
        cr.move_to(1.5 + head, mid - head)
        cr.line_to(1.5, mid)
        cr.line_to(1.5 + head, mid + head)
    cr.stroke()


def _side(machine: str, server_name: str, side: Side, kind: str, shrink: bool) -> Gtk.Widget:
    """*shrink*: let the labels shrink with the row. Only the right side
    does, so the left side keeps one width and its size group holds on every
    row; the left side's address is capped at ``LEFT_ADDRESS_CHARS``.

    This computer's side is mirrored: text then icon, set flush right, so
    whatever width it has spare goes to its outer edge, not to the gap
    before the arrow."""
    mirror = machine == THIS_COMPUTER
    xalign = 1 if mirror else 0
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
    icon = _image(
        "computer-symbolic" if machine == THIS_COMPUTER else "network-server-symbolic", 16
    )
    icon.add_css_class("dim-label")
    icon.set_valign(Gtk.Align.CENTER)

    text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    text.set_valign(Gtk.Align.CENTER)
    name = Gtk.Label(
        label=_("This computer") if machine == THIS_COMPUTER else server_name, xalign=xalign
    )
    if shrink:
        name.set_ellipsize(Pango.EllipsizeMode.END)
    text.append(name)

    line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    if mirror:
        line.set_halign(Gtk.Align.END)
    marker = _image("ear-symbolic" if side.role == LISTENS else "target-symbolic", 12)
    marker.add_css_class(f"forward-{kind}")
    marker.set_tooltip_text(_("Listens here") if side.role == LISTENS else _("Destination"))
    line.append(marker)
    address = Gtk.Label(label=side.address, xalign=xalign)
    for name_ in ("monospace", "caption", "dim-label"):
        address.add_css_class(name_)
    # In the middle: the port, at the end, is what tells rules apart.
    address.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
    if not shrink:
        # As wide as the address (up to the cap) at the least, not only by
        # nature: a row short of room would otherwise squeeze this side
        # more on some rows than on others, and the arrows would not line up.
        chars = min(len(side.address), LEFT_ADDRESS_CHARS)
        address.set_width_chars(chars)
        address.set_max_width_chars(chars)
    address.set_tooltip_text(side.address)
    line.append(address)
    text.append(line)

    if not mirror:
        box.append(icon)
        box.append(text)
        return box
    box.append(text)
    box.append(icon)
    # A size group sizes the widget it holds, so the flush-right box goes
    # in a holder: the holder takes the group's width, the box its own at
    # the holder's end. The box expands to be given that end; the holder is
    # set not to, or it would pass the expansion on to the strip.
    box.set_halign(Gtk.Align.END)
    box.set_hexpand(True)
    holder = Gtk.Box()
    holder.set_hexpand(False)
    holder.append(box)
    return holder


def build_forward_rule_row(
    view: ForwardView,
    server_name: str,
    on_edit: Callable[[], None],
    on_delete: Callable[[], None],
    side_group: Optional[Gtk.SizeGroup] = None,
) -> Gtk.Widget:
    """One rule's row: the type and its flag over the strip. *side_group*,
    shared by every row of the list, keeps the left side the same width, so
    the arrows line up from row to row."""
    _ensure_css()
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    row.add_css_class("forward-rule")
    body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    body.set_hexpand(True)

    # Type and flag, on a line above the strip rather than in a column
    # beside it: a column made every row, and the dialog, too wide.
    heading = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
    title = Gtk.Label(label=view.title, xalign=0)
    title.add_css_class("forward-title")
    title.add_css_class(f"forward-{view.kind}")
    title.set_valign(Gtk.Align.BASELINE)
    heading.append(title)
    flag = Gtk.Label(label=view.flag, xalign=0, hexpand=True)
    for name in ("monospace", "caption", "dim-label"):
        flag.add_css_class(name)
    flag.set_wrap(True)
    flag.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    flag.set_valign(Gtk.Align.BASELINE)
    heading.append(flag)
    body.append(heading)

    # This computer, arrow, server.
    strip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    strip.add_css_class("forward-strip")
    left = _side(THIS_COMPUTER, server_name, view.this_computer, view.kind, shrink=False)
    if side_group is not None:
        side_group.add_widget(left)
    strip.append(left)

    arrow = Gtk.DrawingArea()
    arrow.set_content_width(ARROW_WIDTH)
    arrow.set_content_height(10)
    arrow.set_valign(Gtk.Align.CENTER)
    arrow.add_css_class(f"forward-{view.kind}")
    arrow.set_draw_func(_draw_arrow, view.outbound)
    arrow.set_tooltip_text(
        _("To {server}").format(server=server_name)
        if view.outbound
        else _("From {server}").format(server=server_name)
    )
    strip.append(arrow)

    right = _side(SERVER, server_name, view.server, view.kind, shrink=True)
    right.set_hexpand(True)
    strip.append(right)
    body.append(strip)
    row.append(body)

    # Actions.
    edit = Gtk.Button(icon_name="document-edit-symbolic", valign=Gtk.Align.CENTER)
    edit.add_css_class("flat")
    edit.set_tooltip_text(_("Edit Rule"))
    edit.connect("clicked", lambda *_a: on_edit())
    delete = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER)
    delete.add_css_class("flat")
    delete.add_css_class("error")
    delete.set_tooltip_text(_("Delete Rule"))
    delete.connect("clicked", lambda *_a: on_delete())
    actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
    actions.set_valign(Gtk.Align.CENTER)
    actions.append(edit)
    actions.append(delete)
    row.append(actions)
    return row
