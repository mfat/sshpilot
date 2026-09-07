"""Host Info dialog — renders daemon-owned remote host information.

This module is presentation only.  It holds no probe text and no parsing: the
daemon runs the probe and returns typed DTOs
(:mod:`sshpilot.api.models.host_info`), and
:class:`~sshpilot.gtk.host_info_controller.HostInfoController` starts probes
and delivers their results without polling.  Everything here turns values into
pixels and localized text.

Two presentation rules keep the tabs consistent with each other:

* severity is computed once, in :func:`_usage_severity`, and drives both the
  donut ring and the usage bars -- green while there is headroom, amber as it
  runs out, red when it is nearly gone -- so the same number can never read
  "critical" on one tab and "healthy" on another;
* a value the host did not report is rendered as "N/A" rather than as zero.
"""

from __future__ import annotations

import logging
import math
from gettext import gettext as _, ngettext
from typing import Callable, List, Optional, Sequence, Tuple

from gi.repository import Adw, GLib, Gdk, Gtk, Pango

from .api.connection_identity import connection_id_for
from .api.models.common import SessionId
from .api.models.host_info import (
    HostInfoProbe,
    HostInfoSnapshot,
    InterfaceCounters,
    NetworkInterfaceKind,
    NetworkInterfaceState,
    SocketDirection,
)
from .gtk.host_info_controller import HostInfoController, HostInfoProbeBusy
from .gtk.host_info_failure_messages import (
    format_host_info_error,
    format_host_info_failure,
)

logger = logging.getLogger(__name__)

#: How often the Traffic tab samples byte counters while it is visible.
_TRAFFIC_INTERVAL_MS = 2000

#: Usage fractions at or above these read as elevated and critical.
_WARN_FRACTION = 0.75
_CRITICAL_FRACTION = 0.90

#: Temperatures in °C at or above these read as elevated and critical.
_WARN_CELSIUS = 60.0
_CRITICAL_CELSIUS = 80.0

_SEVERITY_OK = "usage-ok"
_SEVERITY_WARN = "usage-warn"
_SEVERITY_CRITICAL = "usage-critical"
#: A reading the host did not publish is neither healthy nor alarming. It gets
#: its own neutral colour so an unknown value never reads as "fine".
_SEVERITY_UNKNOWN = "usage-unknown"
_SEVERITY_CLASSES = (
    _SEVERITY_OK,
    _SEVERITY_WARN,
    _SEVERITY_CRITICAL,
    _SEVERITY_UNKNOWN,
)

_CSS = b"""
/* Usage bars mean "how full", so the fill must darken as the value rises:
   green while there is headroom, amber as it runs out, red when it is nearly
   gone. GTK's own levelbar offsets mean the opposite (they describe a level,
   where full is good) and painted a nearly-full disk green, so every filled
   block is painted from the severity class instead. */
levelbar.usage-bar block.filled {
    background-color: @success_bg_color;
    border-radius: 3px;
}
levelbar.usage-bar.usage-warn block.filled {
    background-color: @warning_bg_color;
}
levelbar.usage-bar.usage-critical block.filled {
    background-color: @error_bg_color;
}
levelbar.usage-bar.usage-unknown block.filled {
    background-color: @insensitive_fg_color;
}
levelbar.usage-bar trough {
    border-radius: 3px;
}

/* The donut ring reads its colour from the CSS foreground so it follows the
   light/dark theme and the user's palette instead of a baked-in RGB value. */
.host-info-gauge { color: @success_bg_color; }
.host-info-gauge.usage-warn { color: @warning_bg_color; }
.host-info-gauge.usage-critical { color: @error_bg_color; }
.host-info-gauge.usage-unknown { color: @insensitive_fg_color; }
"""

_css_installed = False


def _ensure_css() -> None:
    """Install the dialog's styling once per display."""

    global _css_installed
    if _css_installed:
        return
    display = Gdk.Display.get_default()
    if display is None:
        return
    provider = Gtk.CssProvider()
    provider.load_from_data(_CSS)
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_USER
    )
    _css_installed = True


# ---------------------------------------------------------------------------
# Value formatting (the only place raw numbers become text)
# ---------------------------------------------------------------------------

def _format_bytes(value: Optional[float]) -> str:
    """Binary units, for memory as the kernel reports it."""

    if value is None:
        return _("N/A")
    if value < 1024:
        return _("%d B") % int(value)
    scaled = float(value)
    for unit in ("KiB", "MiB", "GiB", "TiB", "PiB"):
        scaled /= 1024
        if scaled < 1024 or unit == "PiB":
            precision = 1 if scaled < 100 else 0
            return f"{scaled:.{precision}f} {unit}"
    return ""


def _format_bytes_si(value: Optional[float]) -> str:
    """Decimal units, for storage as drive vendors label it."""

    if value is None:
        return _("N/A")
    if value < 1000:
        return _("%d B") % int(value)
    scaled = float(value)
    for unit in ("KB", "MB", "GB", "TB", "PB"):
        scaled /= 1000
        if scaled < 1000 or unit == "PB":
            precision = 1 if scaled < 100 else 0
            return f"{scaled:.{precision}f} {unit}"
    return ""


def _format_rate(value: Optional[float]) -> str:
    if value is None:
        return "—"
    if value < 1024:
        return _("%d B/s") % int(value)
    scaled = float(value)
    for unit in ("KiB/s", "MiB/s", "GiB/s", "TiB/s"):
        scaled /= 1024
        if scaled < 1024 or unit == "TiB/s":
            precision = 1 if scaled < 100 else 0
            return f"{scaled:.{precision}f} {unit}"
    return ""


def _format_uptime(seconds: Optional[float]) -> str:
    if seconds is None:
        return _("N/A")
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    minutes = int((seconds % 3600) // 60)
    parts: List[str] = []
    if days:
        parts.append(ngettext("%d day", "%d days", days) % days)
    if hours:
        parts.append(ngettext("%d hour", "%d hours", hours) % hours)
    if minutes:
        parts.append(ngettext("%d minute", "%d minutes", minutes) % minutes)
    if not parts:
        return _("Less than a minute")
    return _(", ").join(parts)


def _format_frequency(megahertz: Optional[float]) -> str:
    if megahertz is None:
        return _("N/A")
    if megahertz >= 1000:
        return _("%.2f GHz") % (megahertz / 1000)
    return _("%.0f MHz") % megahertz


def _format_percent(fraction: Optional[float]) -> str:
    return "—" if fraction is None else _("%d%%") % int(round(fraction * 100))


def _format_reported_percent(value: Optional[float]) -> str:
    """Format a percentage the host itself reported.

    Unlike :func:`_format_percent` this takes no fraction: a CPU share above
    100 is a real reading on a multi-core host, not an error to clamp.
    """

    return _("N/A") if value is None else _("%.1f%%") % value


def _or_na(text: str) -> str:
    return text.strip() or _("N/A")


def _usage_severity(fraction: Optional[float]) -> str:
    if fraction is None:
        return _SEVERITY_UNKNOWN
    if fraction < _WARN_FRACTION:
        return _SEVERITY_OK
    return _SEVERITY_CRITICAL if fraction >= _CRITICAL_FRACTION else _SEVERITY_WARN


def _temperature_severity(celsius: float) -> str:
    if celsius >= _CRITICAL_CELSIUS:
        return _SEVERITY_CRITICAL
    return _SEVERITY_WARN if celsius >= _WARN_CELSIUS else _SEVERITY_OK


def _interface_icon(kind: NetworkInterfaceKind) -> str:
    if kind is NetworkInterfaceKind.LOOPBACK:
        return "network-transmit-receive-symbolic"
    if kind is NetworkInterfaceKind.WIRELESS:
        return "network-wireless-symbolic"
    return "network-idle-symbolic"


def _interface_kind_label(kind: NetworkInterfaceKind) -> str:
    return {
        NetworkInterfaceKind.LOOPBACK: _("Loopback"),
        NetworkInterfaceKind.WIRELESS: _("Wi-Fi"),
        NetworkInterfaceKind.ETHERNET: _("Ethernet"),
    }.get(kind, _("Unknown"))


def _interface_state_label(state: NetworkInterfaceState) -> str:
    return {
        NetworkInterfaceState.UP: _("Up"),
        NetworkInterfaceState.DOWN: _("Down"),
        NetworkInterfaceState.NO_CARRIER: _("No carrier"),
    }.get(state, _("Unknown"))


# ---------------------------------------------------------------------------
# Small widget builders
# ---------------------------------------------------------------------------

def _card() -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    box.add_css_class("card")
    return box


def _section_label(text: str) -> Gtk.Label:
    label = Gtk.Label(label=text)
    label.set_xalign(0)
    label.add_css_class("heading")
    label.add_css_class("caption")
    label.set_opacity(0.65)
    return label


def _caption(text: str, *, dim: float = 0.55) -> Gtk.Label:
    label = Gtk.Label(label=text)
    label.set_xalign(0)
    label.add_css_class("caption")
    label.set_opacity(dim)
    label.set_wrap(True)
    return label


def _value_label(text: str, *, mono: bool = False, selectable: bool = True) -> Gtk.Label:
    label = Gtk.Label(label=text)
    label.set_xalign(0)
    label.set_selectable(selectable)
    label.set_ellipsize(Pango.EllipsizeMode.END)
    if mono:
        label.add_css_class("monospace")
    return label


def _usage_bar(fraction: Optional[float], *, height: int = 8,
               severity: Optional[str] = None) -> Gtk.LevelBar:
    """A bar whose fill colour states severity, not GTK's inverted level."""

    bar = Gtk.LevelBar()
    bar.set_min_value(0)
    bar.set_max_value(1.0)
    bar.set_value(min(max(fraction or 0.0, 0.0), 1.0))
    bar.set_hexpand(True)
    bar.set_valign(Gtk.Align.CENTER)
    bar.set_size_request(-1, height)
    bar.add_css_class("usage-bar")
    bar.add_css_class(severity or _usage_severity(fraction))
    bar.set_sensitive(fraction is not None)
    return bar


def _key_value_rows(card: Gtk.Box, rows: Sequence[Tuple[str, str, bool]]) -> None:
    """Fill a card with ``(key, value, monospace)`` rows separated by rules."""

    for index, (key, value, mono) in enumerate(rows):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        row.set_margin_start(16)
        row.set_margin_end(16)
        row.set_margin_top(12)
        row.set_margin_bottom(12)

        key_label = Gtk.Label(label=key)
        key_label.set_xalign(0)
        key_label.set_opacity(0.6)
        key_label.set_size_request(200, -1)
        row.append(key_label)

        value_label = _value_label(value, mono=mono)
        value_label.set_hexpand(True)
        row.append(value_label)

        card.append(row)
        if index < len(rows) - 1:
            card.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))


class _Table:
    """A card-hosted grid whose header and rows share real column widths.

    Columns are grid columns rather than labels padded with a minimum size
    request, so a long mount point or device name widens its column instead of
    silently overflowing into the next one.
    """

    def __init__(self, columns: Sequence[Tuple[str, float, bool]]) -> None:
        self.widget = _card()
        self._columns = columns
        self._grid = Gtk.Grid()
        self._grid.set_column_spacing(16)
        self._grid.set_margin_start(16)
        self._grid.set_margin_end(16)
        self._grid.set_margin_top(10)
        self._grid.set_margin_bottom(10)
        self._row = 0
        self.widget.append(self._grid)

        for column, (title, xalign, expand) in enumerate(columns):
            label = Gtk.Label(label=title)
            label.set_xalign(xalign)
            label.add_css_class("caption")
            label.add_css_class("heading")
            label.set_opacity(0.6)
            label.set_hexpand(expand)
            self._grid.attach(label, column, 0, 1, 1)
        self._row = 1

    def add_row(self, cells: Sequence[Gtk.Widget]) -> None:
        separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        separator.set_margin_top(6)
        separator.set_margin_bottom(6)
        self._grid.attach(separator, 0, self._row, len(self._columns), 1)
        self._row += 1
        for column, cell in enumerate(cells):
            _, xalign, expand = self._columns[column]
            if isinstance(cell, Gtk.Label):
                cell.set_xalign(xalign)
            cell.set_hexpand(expand)
            self._grid.attach(cell, column, self._row, 1, 1)
        self._row += 1

    def add_empty(self, text: str) -> None:
        label = Gtk.Label(label=text)
        label.add_css_class("dim-label")
        label.set_margin_top(14)
        label.set_margin_bottom(14)
        self.widget.append(label)


# ---------------------------------------------------------------------------
# Donut gauge
# ---------------------------------------------------------------------------

def _draw_ring(area: Gtk.DrawingArea, cr, width: int, height: int,
               fraction: Optional[float]) -> None:
    """Draw the gauge ring in the widget's themed foreground colour."""

    colour = area.get_color()
    centre_x, centre_y = width / 2, height / 2
    radius = min(centre_x, centre_y) - 6
    if radius <= 0:
        return
    cr.set_line_width(max(8, radius * 0.22))
    cr.set_line_cap(1)  # round

    cr.set_source_rgba(colour.red, colour.green, colour.blue, colour.alpha * 0.18)
    cr.arc(centre_x, centre_y, radius, 0, 2 * math.pi)
    cr.stroke()

    if fraction:
        cr.set_source_rgba(colour.red, colour.green, colour.blue, colour.alpha)
        start = -math.pi / 2
        cr.arc(centre_x, centre_y, radius, start, start + 2 * math.pi * min(fraction, 1.0))
        cr.stroke()


def _gauge_card(fraction: Optional[float], title: str, detail: str,
                sub_detail: str) -> Gtk.Box:
    """A donut gauge whose percentage is a real label, not painted text."""

    card = _card()
    inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    inner.set_margin_top(16)
    inner.set_margin_bottom(12)
    inner.set_margin_start(12)
    inner.set_margin_end(12)

    area = Gtk.DrawingArea()
    area.set_content_width(96)
    area.set_content_height(96)
    area.add_css_class("host-info-gauge")
    area.add_css_class(_usage_severity(fraction))
    area.set_draw_func(lambda widget, cr, w, h: _draw_ring(widget, cr, w, h, fraction))

    percent = Gtk.Label(label=_format_percent(fraction))
    percent.add_css_class("title-1")
    percent.set_halign(Gtk.Align.CENTER)
    percent.set_valign(Gtk.Align.CENTER)

    overlay = Gtk.Overlay()
    overlay.set_halign(Gtk.Align.CENTER)
    overlay.set_child(area)
    overlay.add_overlay(percent)
    inner.append(overlay)

    heading = Gtk.Label(label=title)
    heading.add_css_class("heading")
    inner.append(heading)

    if detail:
        detail_label = Gtk.Label(label=detail)
        detail_label.add_css_class("caption")
        detail_label.add_css_class("monospace")
        inner.append(detail_label)
    if sub_detail:
        sub_label = Gtk.Label(label=sub_detail)
        sub_label.add_css_class("caption")
        sub_label.set_opacity(0.6)
        inner.append(sub_label)

    card.append(inner)
    return card


def _page() -> Gtk.Box:
    page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
    page.set_margin_top(18)
    page.set_margin_bottom(24)
    page.set_margin_start(24)
    page.set_margin_end(24)
    return page


# ---------------------------------------------------------------------------
# Dialog
# ---------------------------------------------------------------------------

class MachineInfoDialog:
    """Presents one remote host's daemon-reported system information."""

    def __init__(self, window, connection) -> None:
        _ensure_css()
        self._window = window
        self._connection = connection
        self._snapshot: Optional[HostInfoSnapshot] = None
        self._closed = False
        self._controller: Optional[HostInfoController] = None
        self._interaction_dialogs = None

        self._age_timer_id = 0
        self._traffic_timer_id = 0
        self._last_refresh: Optional[float] = None
        self._previous_counters: Optional[Sequence[InterfaceCounters]] = None
        self._previous_counter_time = 0.0
        self._rate_labels: dict = {}

        self._dialog = Adw.Dialog()
        self._dialog.set_content_width(900)
        self._dialog.set_content_height(716)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(self._build_header())
        self._content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._content.set_vexpand(True)
        toolbar.set_content(self._content)
        self._dialog.set_child(toolbar)
        self._dialog.connect("closed", self._on_closed)

        self._show_status(_("Gathering host information…"), spinner=True)
        self._dialog.present(window)
        self._start_probe()

    # -- header ---------------------------------------------------------

    def _build_header(self) -> Adw.HeaderBar:
        header = Adw.HeaderBar()
        header.set_show_start_title_buttons(False)
        header.set_show_end_title_buttons(False)

        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title_row.set_halign(Gtk.Align.CENTER)
        icon = Gtk.Image.new_from_icon_name("info-outline-symbolic")
        icon.set_opacity(0.65)
        title_row.append(icon)

        title_column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        title = Gtk.Label(label=_("Host Info"))
        title.add_css_class("title")
        title_column.append(title)

        subtitle = Gtk.Label(label=self._subtitle())
        subtitle.add_css_class("subtitle")
        subtitle.set_opacity(0.55)
        subtitle.set_ellipsize(Pango.EllipsizeMode.END)
        title_column.append(subtitle)
        title_row.append(title_column)
        header.set_title_widget(title_row)

        close_button = Gtk.Button(icon_name="window-close-symbolic")
        close_button.add_css_class("circular")
        close_button.set_tooltip_text(_("Close"))
        close_button.connect("clicked", lambda _button: self._dialog.close())
        header.pack_end(close_button)

        self._refresh_button = Gtk.Button(label=_("Refresh"))
        self._refresh_button.set_icon_name("view-refresh-symbolic")
        self._refresh_button.set_tooltip_text(_("Refresh host information"))
        self._refresh_button.connect("clicked", lambda _button: self._on_refresh())
        header.pack_end(self._refresh_button)

        self._age_label = Gtk.Label(label="")
        self._age_label.add_css_class("dim-label")
        self._age_label.add_css_class("caption")
        header.pack_end(self._age_label)
        return header

    def _subtitle(self) -> str:
        nickname = getattr(self._connection, "nickname", "") or ""
        username = getattr(self._connection, "username", "") or ""
        host = getattr(self._connection, "host", "") or ""
        if username and host:
            return _("%(nickname)s — %(user)s@%(host)s") % {
                "nickname": nickname, "user": username, "host": host
            }
        if host:
            return _("%(nickname)s — %(host)s") % {"nickname": nickname, "host": host}
        return nickname

    # -- daemon probes --------------------------------------------------

    def _ensure_controller(self) -> Optional[HostInfoController]:
        if self._controller is not None:
            return self._controller
        client = getattr(self._window, "client", None)
        if client is None:
            return None
        self._controller = HostInfoController(client)
        if self._interaction_dialogs is None:
            self._attach_interaction_presenter(client)
        return self._controller

    def _attach_interaction_presenter(self, client) -> None:
        """Let a gather ask for a passphrase, password, host key or MFA answer.

        The presenter is built before the probe starts so it exists by the time
        a prompt can appear, and starts unbound -- ignoring every interaction
        -- until :meth:`_bind_interactions` binds it to the operation scope the
        daemon raises the probe's prompts under.
        """

        bridge = getattr(self._window, "client_bridge", None)
        if bridge is None:
            return
        try:
            from .daemon_interaction_dialogs import DaemonInteractionDialogs

            self._interaction_dialogs = DaemonInteractionDialogs(
                client, bridge, self._window
            )
        except Exception:
            logger.debug("Host info interaction presenter unavailable", exc_info=True)

    def _bind_interactions(self, operation_id) -> bool:
        """Bind the presenter to the probe's operation scope.

        ``set_session`` reconciles prompts the daemon already created, so a
        password asked for before this ran is still presented.
        """

        if self._closed or self._interaction_dialogs is None:
            return False
        try:
            self._interaction_dialogs.set_session(SessionId(str(operation_id)))
        except Exception:
            logger.debug(
                "Host info interaction presenter bind failed", exc_info=True
            )
        return False

    def _submit(self, probe: HostInfoProbe, on_result: Callable, on_error: Callable) -> bool:
        """Ask the daemon for one probe; returns False when it cannot be asked.

        The controller owns its own worker, so this returns immediately and the
        callbacks are marshalled back onto the GTK main loop.  A probe of this
        kind that is already outstanding raises :class:`HostInfoProbeBusy`.
        """

        controller = self._ensure_controller()
        if controller is None:
            return False
        try:
            connection_id = connection_id_for(self._connection)
        except Exception:
            logger.debug("Host info connection identity unavailable", exc_info=True)
            return False
        # Only the full gather is interactive; bandwidth sampling is
        # autofill-only and must never rebind the presenter away from it.
        on_started = None
        if probe is HostInfoProbe.FULL:
            on_started = lambda operation_id: GLib.idle_add(  # noqa: E731
                self._bind_interactions, operation_id
            )
        controller.start(
            connection_id,
            probe,
            lambda summary: GLib.idle_add(on_result, summary),
            lambda error: GLib.idle_add(on_error, error),
            on_started=on_started,
        )
        return True

    def _start_probe(self) -> None:
        try:
            started = self._submit(HostInfoProbe.FULL, self._on_snapshot, self._on_error)
        except HostInfoProbeBusy:
            return
        if not started:
            self._show_status(_("Daemon connection unavailable."))
            self._refresh_button.set_sensitive(True)

    def _on_refresh(self) -> None:
        self._refresh_button.set_sensitive(False)
        self._stop_traffic_timer()
        self._show_status(_("Gathering host information…"), spinner=True)
        self._start_probe()

    def _on_snapshot(self, summary) -> bool:
        if self._closed:
            return False
        self._refresh_button.set_sensitive(True)
        if summary.failure is not None:
            self._show_status(
                _("Could not gather host information.\n\n%s")
                % format_host_info_failure(summary.failure)
            )
            return False
        if summary.snapshot is None:
            self._show_status(_("The host returned no system information."))
            return False
        self._snapshot = summary.snapshot
        self._previous_counters = summary.counters
        self._previous_counter_time = GLib.get_monotonic_time() / 1_000_000
        self._last_refresh = self._previous_counter_time
        self._build_tabs()
        self._start_age_timer()
        return False

    def _on_error(self, error: BaseException) -> bool:
        if self._closed:
            return False
        # A failed refresh must not leave the button dead; the user needs to be
        # able to try again.
        self._refresh_button.set_sensitive(True)
        logger.warning("Host info gather failed: %s", error)
        self._show_status(
            _("Could not gather host information.\n\n%s")
            % format_host_info_error(error)
        )
        return False

    # -- status / age ---------------------------------------------------

    def _clear_content(self) -> None:
        child = self._content.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self._content.remove(child)
            child = following

    def _show_status(self, message: str, *, spinner: bool = False) -> None:
        self._clear_content()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_valign(Gtk.Align.CENTER)
        box.set_vexpand(True)
        box.set_margin_start(24)
        box.set_margin_end(24)
        if spinner:
            busy = Gtk.Spinner()
            busy.set_size_request(32, 32)
            busy.start()
            box.append(busy)
        label = Gtk.Label(label=message)
        label.set_wrap(True)
        label.set_justify(Gtk.Justification.CENTER)
        if not spinner:
            label.add_css_class("dim-label")
        box.append(label)
        self._content.append(box)

    def _start_age_timer(self) -> None:
        if self._age_timer_id:
            GLib.source_remove(self._age_timer_id)
        self._update_age_label()
        self._age_timer_id = GLib.timeout_add_seconds(1, self._tick_age)

    def _tick_age(self) -> bool:
        if self._closed:
            self._age_timer_id = 0
            return False
        self._update_age_label()
        return True

    def _update_age_label(self) -> None:
        if self._last_refresh is None:
            self._age_label.set_text("")
            return
        elapsed = int(GLib.get_monotonic_time() / 1_000_000 - self._last_refresh)
        if elapsed < 60:
            self._age_label.set_text(
                ngettext("Updated %d second ago", "Updated %d seconds ago", elapsed)
                % elapsed
            )
        else:
            minutes = elapsed // 60
            self._age_label.set_text(
                ngettext("Updated %d minute ago", "Updated %d minutes ago", minutes)
                % minutes
            )

    # -- traffic sampling ----------------------------------------------

    def _start_traffic_timer(self) -> None:
        if self._traffic_timer_id:
            return
        self._traffic_timer_id = GLib.timeout_add(
            _TRAFFIC_INTERVAL_MS, self._tick_traffic
        )

    def _stop_traffic_timer(self) -> None:
        if self._traffic_timer_id:
            GLib.source_remove(self._traffic_timer_id)
            self._traffic_timer_id = 0

    def _tick_traffic(self) -> bool:
        if self._closed:
            self._traffic_timer_id = 0
            return False
        # Skip this tick when the previous sample has not come back yet, so a
        # slow link cannot build a queue of outstanding probes.
        try:
            started = self._submit(
                HostInfoProbe.NETWORK_COUNTERS,
                self._on_counters,
                self._on_counter_error,
            )
        except HostInfoProbeBusy:
            return True
        if not started:
            self._traffic_timer_id = 0
            return False
        return True

    def _on_counters(self, summary) -> bool:
        if self._closed or summary.failure is not None:
            return False
        now = GLib.get_monotonic_time() / 1_000_000
        elapsed = now - self._previous_counter_time
        if self._previous_counters and elapsed > 0:
            previous = {item.name: item for item in self._previous_counters}
            for counters in summary.counters:
                labels = self._rate_labels.get(counters.name)
                earlier = previous.get(counters.name)
                if labels is None or earlier is None:
                    continue
                receive, transmit = labels
                receive.set_text(
                    _format_rate(max(0, counters.rx_bytes - earlier.rx_bytes) / elapsed)
                )
                transmit.set_text(
                    _format_rate(max(0, counters.tx_bytes - earlier.tx_bytes) / elapsed)
                )
        self._previous_counters = summary.counters
        self._previous_counter_time = now
        return False

    def _on_counter_error(self, error: BaseException) -> bool:
        logger.debug("Host info bandwidth sample failed: %s", error)
        return False

    # -- teardown -------------------------------------------------------

    def _on_closed(self, *_args) -> None:
        self._closed = True
        if self._age_timer_id:
            GLib.source_remove(self._age_timer_id)
            self._age_timer_id = 0
        self._stop_traffic_timer()
        if self._controller is not None:
            self._controller.close()
            self._controller = None
        if self._interaction_dialogs is not None:
            self._interaction_dialogs.close()
            self._interaction_dialogs = None

    # -- tabs -----------------------------------------------------------

    def _build_tabs(self) -> None:
        self._clear_content()
        self._rate_labels = {}

        pages = (
            ("overview", _("Overview"), self._build_overview()),
            ("resources", _("Resources"), self._build_resources()),
            ("storage", _("Storage"), self._build_storage()),
            ("network", _("Network"), self._build_network()),
            ("traffic", _("Traffic"), self._build_traffic()),
            ("system", _("System"), self._build_system()),
        )

        stack = Adw.ViewStack()
        stack.set_vexpand(True)
        for name, title, widget in pages:
            scrolled = Gtk.ScrolledWindow()
            scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            scrolled.set_vexpand(True)
            scrolled.set_child(widget)
            stack.add_titled(scrolled, name, title)

        if hasattr(Adw, "InlineViewSwitcher"):
            switcher = Adw.InlineViewSwitcher()
            switcher.set_stack(stack)
            switcher.set_hexpand(True)
            switcher.set_halign(Gtk.Align.FILL)
            try:
                switcher.set_display_mode(Adw.InlineViewSwitcherDisplayMode.LABELS)
            except Exception:
                logger.debug("Inline switcher label mode unavailable", exc_info=True)
        else:
            switcher = Gtk.StackSwitcher(stack=stack)
            switcher.set_halign(Gtk.Align.CENTER)

        switcher_card = _card()
        switcher_card.set_hexpand(True)
        switcher_card.set_margin_start(24)
        switcher_card.set_margin_end(24)
        switcher_card.set_margin_top(12)
        switcher_card.append(switcher)

        container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        container.set_vexpand(True)
        container.append(switcher_card)
        container.append(stack)
        self._content.append(container)

        stack.connect("notify::visible-child-name", self._on_tab_changed)

    def _on_tab_changed(self, stack, _pspec) -> None:
        if stack.get_visible_child_name() == "traffic":
            self._start_traffic_timer()
        else:
            self._stop_traffic_timer()

    # -- Overview -------------------------------------------------------

    def _build_overview(self) -> Gtk.Box:
        page = _page()
        snapshot = self._snapshot
        memory = snapshot.memory
        processors = snapshot.cpu.logical_processors

        load_fraction = None
        load_detail = ""
        if snapshot.load_average is not None:
            load_detail = _("load %(one).2f · %(five).2f · %(fifteen).2f") % {
                "one": snapshot.load_average.one,
                "five": snapshot.load_average.five,
                "fifteen": snapshot.load_average.fifteen,
            }
            if processors:
                load_fraction = snapshot.load_average.one / processors

        gauges = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        gauges.set_homogeneous(True)
        gauges.append(
            _gauge_card(
                load_fraction,
                _("CPU"),
                _format_frequency(snapshot.cpu.frequency_mhz),
                load_detail,
            )
        )

        used = memory.used_bytes
        memory_fraction = used / memory.total_bytes if used is not None and memory.total_bytes else None
        swap_detail = ""
        if memory.swap_total_bytes:
            swap_detail = _("swap %(used)s / %(total)s") % {
                "used": _format_bytes(memory.swap_used_bytes),
                "total": _format_bytes(memory.swap_total_bytes),
            }
        gauges.append(
            _gauge_card(
                memory_fraction,
                _("Memory"),
                _("%(used)s / %(total)s") % {
                    "used": _format_bytes(used),
                    "total": _format_bytes(memory.total_bytes),
                },
                swap_detail,
            )
        )

        root = snapshot.root_filesystem
        root_detail = root_device = ""
        if root is not None:
            root_detail = _("%(used)s / %(total)s") % {
                "used": _format_bytes_si(root.used_bytes),
                "total": _format_bytes_si(root.size_bytes),
            }
            root_device = (
                _("%(fstype)s on %(device)s")
                % {"fstype": root.fstype, "device": root.device}
                if root.fstype
                else root.device
            )
        gauges.append(
            _gauge_card(
                root.used_fraction if root is not None else None,
                _("Root filesystem"),
                root_detail,
                root_device,
            )
        )
        page.append(gauges)

        card = _card()
        rows = [
            (_("Hostname"), _or_na(snapshot.hostname), True),
        ]
        if snapshot.device_model:
            rows.append((_("Device"), snapshot.device_model, False))
        rows.extend(
            [
                (_("Operating system"), _or_na(snapshot.os_pretty_name), False),
                (_("Kernel"), _or_na(snapshot.kernel), True),
                (_("Processor"), self._processor_text(), False),
                (_("CPU frequency"), _format_frequency(snapshot.cpu.frequency_mhz), True),
                (_("Memory"), _format_bytes(memory.total_bytes or None), True),
                (_("Uptime"), _format_uptime(snapshot.uptime_seconds), False),
                (_("Booted"), _or_na(snapshot.boot_time), False),
            ]
        )
        _key_value_rows(card, rows)
        page.append(card)
        return page

    def _processor_text(self) -> str:
        cpu = self._snapshot.cpu
        threads = cpu.total_threads
        if cpu.cores_per_socket and cpu.sockets:
            cores = cpu.cores_per_socket * cpu.sockets
            topology = _("%(cores)d cores · %(threads)d threads · %(sockets)d sockets") % {
                "cores": cores,
                "threads": threads or cores,
                "sockets": cpu.sockets,
            }
        elif threads:
            topology = ngettext("%d core", "%d cores", threads) % threads
        else:
            topology = ""
        if cpu.model and topology:
            return _("%(model)s (%(topology)s)") % {
                "model": cpu.model, "topology": topology
            }
        return _or_na(cpu.model or topology)

    # -- Resources ------------------------------------------------------

    def _build_resources(self) -> Gtk.Box:
        page = _page()
        snapshot = self._snapshot
        processors = snapshot.cpu.logical_processors

        page.append(_section_label(_("Load average")))
        load_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        load_row.set_homogeneous(True)
        averages = (
            (_("1 min"), snapshot.load_average.one if snapshot.load_average else None),
            (_("5 min"), snapshot.load_average.five if snapshot.load_average else None),
            (_("15 min"), snapshot.load_average.fifteen if snapshot.load_average else None),
        )
        for title, value in averages:
            fraction = value / processors if value is not None and processors else None
            card = _card()
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            inner.set_margin_top(14)
            inner.set_margin_bottom(14)
            inner.set_margin_start(16)
            inner.set_margin_end(16)

            header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            caption = Gtk.Label(label=title)
            caption.add_css_class("caption")
            caption.set_opacity(0.6)
            caption.set_hexpand(True)
            caption.set_xalign(0)
            header.append(caption)
            reading = Gtk.Label(label="—" if value is None else f"{value:.2f}")
            reading.add_css_class("monospace")
            reading.add_css_class("heading")
            header.append(reading)
            inner.append(header)
            inner.append(_usage_bar(fraction, height=6))
            card.append(inner)
            load_row.append(card)
        page.append(load_row)
        page.append(
            _caption(
                ngettext("Based on %d CPU", "Based on %d CPUs", processors)
                % processors
                if processors
                else _("CPU count unavailable.")
            )
        )

        page.append(self._processes_section())

        # Full width and stacked: the memory readings and the sensor labels
        # both wrap badly in a half-width column.
        page.append(self._memory_section())
        page.append(self._temperature_section())
        return page

    def _processes_section(self) -> Gtk.Box:
        section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        section.append(_section_label(_("Top processes")))
        table = _Table(
            (
                (_("Process"), 0.0, True),
                (_("CPU"), 1.0, False),
                (_("Memory"), 1.0, False),
            )
        )
        processes = self._snapshot.processes[:5]
        for process in processes:
            command = _value_label(process.command, mono=True)
            command.add_css_class("caption")
            cpu = _value_label(_format_reported_percent(process.cpu_percent), mono=True)
            cpu.add_css_class("caption")
            memory = _value_label(
                _format_reported_percent(process.memory_percent), mono=True
            )
            memory.add_css_class("caption")
            memory.set_opacity(0.75)
            table.add_row([command, cpu, memory])
        if not processes:
            table.add_empty(_("No process list reported"))
        section.append(table.widget)
        # Above 100% is a real reading, not an overflow: the host measures a
        # share of one CPU, and a threaded process uses several.
        section.append(_caption(_("A share of one CPU, ranked by the host.")))
        return section

    def _memory_section(self) -> Gtk.Box:
        memory = self._snapshot.memory
        section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        section.append(_section_label(_("Memory")))

        card = _card()
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        inner.set_margin_top(16)
        inner.set_margin_bottom(16)
        inner.set_margin_start(16)
        inner.set_margin_end(16)

        used = memory.used_bytes
        fraction = used / memory.total_bytes if used is not None and memory.total_bytes else None
        inner.append(_usage_bar(fraction, height=10))

        readings = Gtk.FlowBox()
        readings.set_selection_mode(Gtk.SelectionMode.NONE)
        readings.set_max_children_per_line(5)
        readings.set_min_children_per_line(2)
        for key, value in (
            (_("MemTotal"), _format_bytes(memory.total_bytes or None)),
            (_("MemFree"), _format_bytes(memory.free_bytes)),
            (_("Buffers"), _format_bytes(memory.buffers_bytes)),
            (_("Cached"), _format_bytes(memory.cached_bytes)),
            (_("MemAvailable"), _format_bytes(memory.available_bytes)),
        ):
            item = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            name = Gtk.Label(label=key)
            name.set_opacity(0.6)
            item.append(name)
            reading = Gtk.Label(label=value)
            reading.add_css_class("monospace")
            item.append(reading)
            readings.insert(item, -1)
        inner.append(readings)
        card.append(inner)

        if memory.swap_total_bytes:
            card.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
            swap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            swap.set_margin_top(12)
            swap.set_margin_bottom(16)
            swap.set_margin_start(16)
            swap.set_margin_end(16)
            header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            title = Gtk.Label(label=_("Swap"))
            title.set_opacity(0.6)
            title.set_hexpand(True)
            title.set_xalign(0)
            header.append(title)
            swap_fraction = memory.swap_used_bytes / memory.swap_total_bytes
            reading = Gtk.Label(
                label=_("%(used)s / %(total)s · %(percent)s") % {
                    "used": _format_bytes(memory.swap_used_bytes),
                    "total": _format_bytes(memory.swap_total_bytes),
                    "percent": _format_percent(swap_fraction),
                }
            )
            reading.add_css_class("monospace")
            header.append(reading)
            swap.append(header)
            swap.append(_usage_bar(swap_fraction, height=6))
            card.append(swap)

        section.append(card)
        return section

    def _temperature_section(self) -> Gtk.Box:
        section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        section.append(_section_label(_("Temperatures")))
        readings = self._snapshot.temperatures
        card = _card()
        if not readings:
            label = Gtk.Label(label=_("N/A"))
            label.add_css_class("dim-label")
            label.set_margin_top(16)
            label.set_margin_bottom(16)
            card.append(label)
            section.append(card)
            return section

        for index, reading in enumerate(readings):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            row.set_margin_start(16)
            row.set_margin_end(16)
            row.set_margin_top(11)
            row.set_margin_bottom(11)

            name = Gtk.Label(label=reading.label)
            name.set_opacity(0.6)
            name.set_xalign(0)
            name.set_size_request(120, -1)
            name.set_ellipsize(Pango.EllipsizeMode.END)
            row.append(name)

            # The reading drives the bar's colour, so a hot sensor is
            # visibly hot rather than merely long.
            row.append(
                _usage_bar(
                    min(reading.celsius / 100.0, 1.0),
                    height=6,
                    severity=_temperature_severity(reading.celsius),
                )
            )

            value = Gtk.Label(label=_("%d °C") % int(reading.celsius))
            value.add_css_class("monospace")
            value.set_xalign(1)
            value.set_size_request(56, -1)
            row.append(value)

            card.append(row)
            if index < len(readings) - 1:
                card.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        section.append(card)
        return section

    # -- Storage --------------------------------------------------------

    def _build_storage(self) -> Gtk.Box:
        page = _page()
        page.append(_section_label(_("Filesystems")))
        table = _Table(
            (
                (_("Mount point"), 0.0, False),
                (_("Device"), 0.0, False),
                (_("Usage"), 0.0, True),
                (_("Used / Size"), 1.0, False),
                (_("Available"), 1.0, False),
            )
        )
        filesystems = self._snapshot.filesystems
        for filesystem in filesystems:
            usage = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            usage.set_hexpand(True)
            usage.append(_usage_bar(filesystem.used_fraction))
            percent = Gtk.Label(label=_format_percent(filesystem.used_fraction))
            percent.add_css_class("monospace")
            percent.add_css_class("caption")
            usage.append(percent)

            device = _value_label(
                _("%(device)s · %(fstype)s") % {
                    "device": filesystem.device, "fstype": filesystem.fstype
                }
                if filesystem.fstype
                else filesystem.device,
                mono=True,
            )
            device.set_opacity(0.7)
            device.add_css_class("caption")

            size = _value_label(
                _("%(used)s / %(total)s") % {
                    "used": _format_bytes_si(filesystem.used_bytes),
                    "total": _format_bytes_si(filesystem.size_bytes),
                },
                mono=True,
            )
            size.add_css_class("caption")

            # Reserved blocks mean size - used is not what is left, so the
            # host's own available figure is reported rather than derived.
            available = _value_label(
                _format_bytes_si(filesystem.available_bytes), mono=True
            )
            available.add_css_class("caption")

            table.add_row(
                [
                    _value_label(filesystem.mount_point, mono=True),
                    device,
                    usage,
                    size,
                    available,
                ]
            )
        if not filesystems:
            table.add_empty(_("No filesystems reported"))
        page.append(table.widget)
        page.append(_caption(_("Only physical disks are shown.")))
        page.append(self._io_pressure_section())
        return page

    def _io_pressure_section(self) -> Gtk.Box:
        section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        section.append(_section_label(_("I/O pressure")))
        snapshot = self._snapshot
        if snapshot.io_pressure_some is None and snapshot.io_pressure_full is None:
            card = _card()
            empty = Gtk.Label(label=_("This host does not report I/O pressure"))
            empty.add_css_class("dim-label")
            empty.set_margin_top(16)
            empty.set_margin_bottom(16)
            card.append(empty)
            section.append(card)
            return section

        table = _Table(
            (
                ("", 0.0, True),
                (_("10 s"), 1.0, False),
                (_("60 s"), 1.0, False),
                (_("300 s"), 1.0, False),
            )
        )
        for title, stall in (
            (_("Some tasks stalled"), snapshot.io_pressure_some),
            (_("All tasks stalled"), snapshot.io_pressure_full),
        ):
            name = _value_label(title)
            name.add_css_class("caption")
            cells = [name]
            for value in (
                (stall.avg10, stall.avg60, stall.avg300) if stall is not None
                else (None, None, None)
            ):
                reading = _value_label(_format_reported_percent(value), mono=True)
                reading.add_css_class("caption")
                cells.append(reading)
            table.add_row(cells)
        section.append(table.widget)
        section.append(
            _caption(_("Share of each window spent waiting on storage."))
        )
        return section

    # -- Network --------------------------------------------------------

    def _build_network(self) -> Gtk.Box:
        page = _page()
        snapshot = self._snapshot

        page.append(_section_label(_("Interfaces")))
        card = _card()
        interfaces = snapshot.interfaces
        for index, interface in enumerate(interfaces):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
            row.set_margin_start(16)
            row.set_margin_end(16)
            row.set_margin_top(14)
            row.set_margin_bottom(14)

            icon = Gtk.Image.new_from_icon_name(_interface_icon(interface.kind))
            icon.set_pixel_size(16)
            if interface.state is not NetworkInterfaceState.UP:
                icon.set_opacity(0.5)
            row.append(icon)

            name = _value_label(interface.name, mono=True)
            name.set_size_request(160, -1)
            name.add_css_class("caption")
            row.append(name)

            details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            details.set_hexpand(True)
            # Every address the host reported, v4 first: an interface that
            # answers only over IPv6 is invisible when just ipv4[0] is shown.
            for value in (*interface.ipv4_addresses, *interface.ipv6_addresses):
                address = _value_label(value, mono=True)
                address.add_css_class("caption")
                details.append(address)

            # State reads as text. It used to live in the icon's tooltip,
            # which nobody hovers and a touch user cannot reach at all.
            descriptors = []
            if interface.mac_address:
                descriptors.append(interface.mac_address)
            descriptors.append(_interface_kind_label(interface.kind))
            descriptors.append(_interface_state_label(interface.state))
            if interface.mtu:
                descriptors.append(_("MTU %d") % interface.mtu)
            summary = _value_label(_(" · ").join(descriptors), mono=True)
            summary.add_css_class("caption")
            summary.set_opacity(0.55)
            details.append(summary)
            row.append(details)

            card.append(row)
            if index < len(interfaces) - 1:
                card.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        if not interfaces:
            empty = Gtk.Label(label=_("No interfaces reported"))
            empty.add_css_class("dim-label")
            empty.set_margin_top(16)
            empty.set_margin_bottom(16)
            card.append(empty)
        page.append(card)

        page.append(_section_label(_("Routing & resolution")))
        gateway = snapshot.default_gateway
        if gateway and snapshot.default_gateway_interface:
            gateway = _("%(gateway)s via %(interface)s") % {
                "gateway": gateway, "interface": snapshot.default_gateway_interface
            }
        ssh_endpoint = _("N/A")
        if snapshot.ssh_port is not None:
            ssh_endpoint = (
                _("%(port)d/tcp (%(process)s)") % {
                    "port": snapshot.ssh_port, "process": snapshot.ssh_process
                }
                if snapshot.ssh_process
                else _("%d/tcp") % snapshot.ssh_port
            )
        routing = _card()
        _key_value_rows(
            routing,
            [
                (_("Default gateway"), _or_na(gateway), True),
                (_("DNS servers"), _or_na(_(", ").join(snapshot.dns_servers)), True),
                (_("Listening SSH port"), ssh_endpoint, True),
            ],
        )
        page.append(routing)
        return page

    # -- Traffic --------------------------------------------------------

    def _build_traffic(self) -> Gtk.Box:
        page = _page()
        page.append(
            _caption(
                _("Live — sampled every 2 s."),
                dim=0.75,
            )
        )

        page.append(_section_label(_("Bandwidth")))
        table = _Table(
            (
                (_("Network interface"), 0.0, False),
                (_("Received"), 1.0, False),
                (_("Transmitted"), 1.0, False),
                (_("Rate ↓"), 1.0, True),
                (_("Rate ↑"), 1.0, False),
            )
        )
        # Hide loopback by what the host said it is, not by the name "lo".
        loopback = {
            item.name for item in self._snapshot.interfaces
            if item.kind is NetworkInterfaceKind.LOOPBACK
        }
        counters = [
            item for item in (self._previous_counters or ())
            if item.name not in loopback
        ]
        if not counters:
            counters = list(self._previous_counters or ())
        for item in counters:
            receive_rate = _value_label("—", mono=True, selectable=False)
            receive_rate.add_css_class("caption")
            transmit_rate = _value_label("—", mono=True, selectable=False)
            transmit_rate.add_css_class("caption")
            self._rate_labels[item.name] = (receive_rate, transmit_rate)

            received = _value_label(_format_bytes(item.rx_bytes), mono=True)
            received.add_css_class("caption")
            transmitted = _value_label(_format_bytes(item.tx_bytes), mono=True)
            transmitted.add_css_class("caption")
            name = _value_label(item.name, mono=True)
            name.add_css_class("caption")
            table.add_row([name, received, transmitted, receive_rate, transmit_rate])
        if not counters:
            table.add_empty(_("No interface counters reported"))
        page.append(table.widget)
        page.append(_caption(_("Totals since boot.")))

        page.append(_section_label(_("Remote sessions")))
        page.append(self._sessions_card(remote_only=True))

        incoming = [
            item for item in self._snapshot.sockets
            if item.direction is SocketDirection.INCOMING
        ]
        outgoing = [
            item for item in self._snapshot.sockets
            if item.direction is SocketDirection.OUTGOING
        ]
        columns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        columns.set_homogeneous(True)
        for title, sockets in (
            (ngettext("Incoming · %d established", "Incoming · %d established",
                      len(incoming)) % len(incoming), incoming),
            (ngettext("Outgoing · %d established", "Outgoing · %d established",
                      len(outgoing)) % len(outgoing), outgoing),
        ):
            column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            column.append(_section_label(title))
            card = _card()
            shown = sockets[:8]
            for index, socket in enumerate(shown):
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
                row.set_margin_start(16)
                row.set_margin_end(16)
                row.set_margin_top(11)
                row.set_margin_bottom(11)

                protocol = _value_label(socket.protocol, mono=True)
                protocol.set_size_request(44, -1)
                protocol.add_css_class("caption")
                protocol.set_opacity(0.55)
                row.append(protocol)

                port = _value_label(
                    "—" if socket.local_port is None else f":{socket.local_port}",
                    mono=True,
                )
                port.set_size_request(60, -1)
                port.add_css_class("caption")
                row.append(port)

                peer = _value_label(
                    socket.peer_address
                    if socket.peer_port is None
                    else f"{socket.peer_address}:{socket.peer_port}",
                    mono=True,
                )
                peer.set_hexpand(True)
                peer.set_opacity(0.75)
                peer.add_css_class("caption")
                row.append(peer)

                process = Gtk.Label(label=socket.process)
                process.add_css_class("caption")
                process.set_opacity(0.55)
                row.append(process)

                card.append(row)
                if index < len(shown) - 1:
                    card.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
            hidden = len(sockets) - len(shown)
            if hidden:
                card.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
                more = Gtk.Label(label=ngettext("%d more", "%d more", hidden) % hidden)
                more.add_css_class("caption")
                more.set_opacity(0.55)
                more.set_margin_top(11)
                more.set_margin_bottom(11)
                card.append(more)
            if not shown:
                empty = Gtk.Label(label=_("None"))
                empty.add_css_class("dim-label")
                empty.set_margin_top(12)
                empty.set_margin_bottom(12)
                card.append(empty)
            column.append(card)
            columns.append(column)
        page.append(columns)
        page.append(_caption(_("Some process names need root.")))
        return page

    # -- System ---------------------------------------------------------

    def _build_system(self) -> Gtk.Box:
        snapshot = self._snapshot
        page = _page()

        identity = _card()
        distribution = " ".join(
            part for part in (snapshot.os_id, snapshot.os_version_id) if part
        )
        _key_value_rows(
            identity,
            [
                (_("Distribution"), _or_na(distribution), True),
                (_("Architecture"), _or_na(snapshot.architecture), True),
            ],
        )
        page.append(identity)

        page.append(_section_label(_("Listening services")))
        listening = _Table(
            (
                (_("Port"), 0.0, False),
                (_("Service"), 0.0, True),
            )
        )
        for entry in snapshot.listening_ports:
            port = _value_label(_("%d/tcp") % entry.port, mono=True)
            port.add_css_class("caption")
            service = _value_label(_or_na(entry.process), mono=True)
            service.add_css_class("caption")
            service.set_opacity(0.75)
            listening.add_row([port, service])
        if not snapshot.listening_ports:
            listening.add_empty(_("No listening services reported"))
        page.append(listening.widget)
        page.append(_caption(_("Some process names need root.")))

        page.append(_section_label(_("Failed units")))
        units = _card()
        if snapshot.failed_units:
            _key_value_rows(
                units,
                [
                    (unit.name, unit.description, False)
                    for unit in snapshot.failed_units
                ],
            )
        else:
            # Also what a host without systemd looks like: neither has a
            # failed unit to report.
            empty = Gtk.Label(label=_("No failed units"))
            empty.add_css_class("dim-label")
            empty.set_margin_top(16)
            empty.set_margin_bottom(16)
            units.append(empty)
        page.append(units)

        page.append(_section_label(_("SSH host keys")))
        keys = _Table(
            (
                (_("Algorithm"), 0.0, False),
                (_("Fingerprint"), 0.0, True),
                (_("Bits"), 1.0, False),
            )
        )
        for host_key in snapshot.host_keys:
            algorithm = _value_label(host_key.algorithm, mono=True)
            algorithm.add_css_class("caption")
            fingerprint = _value_label(host_key.fingerprint, mono=True)
            fingerprint.add_css_class("caption")
            fingerprint.set_opacity(0.75)
            bits = _value_label(
                _("N/A") if host_key.bits is None else str(host_key.bits), mono=True
            )
            bits.add_css_class("caption")
            keys.add_row([algorithm, fingerprint, bits])
        if not snapshot.host_keys:
            keys.add_empty(_("No host keys reported"))
        page.append(keys.widget)

        page.append(_section_label(_("Logged-in users")))
        page.append(self._sessions_card(remote_only=False))
        return page

    def _sessions_card(self, *, remote_only: bool) -> Gtk.Box:
        sessions = [
            session for session in self._snapshot.sessions
            if session.remote or not remote_only
        ]
        card = _card()
        if not sessions:
            empty = Gtk.Label(
                label=_("No remote sessions") if remote_only else _("No logged-in users")
            )
            empty.add_css_class("dim-label")
            empty.set_margin_top(16)
            empty.set_margin_bottom(16)
            card.append(empty)
            return card

        for index, session in enumerate(sessions):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            row.set_margin_start(16)
            row.set_margin_end(16)
            row.set_margin_top(12)
            row.set_margin_bottom(12)

            user = _value_label(session.user or _("Unknown"))
            user.add_css_class("heading")
            user.set_size_request(110, -1)
            row.append(user)

            descriptors = [session.tty] if session.tty else []
            descriptors.append(session.origin or _("local console"))
            detail = _value_label(_(" · ").join(descriptors), mono=True)
            detail.set_hexpand(True)
            detail.set_opacity(0.75)
            detail.add_css_class("caption")
            row.append(detail)

            since = Gtk.Label(label=session.since)
            since.add_css_class("caption")
            since.set_opacity(0.55)
            row.append(since)

            card.append(row)
            if index < len(sessions) - 1:
                card.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        return card
