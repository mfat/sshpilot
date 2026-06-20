"""Session Log — track when you connect to each host.

A non-protocol sshPilot plugin. Records session open/close times for every
terminal tab, shows a history on a Tools page, and exports CSV for billing or
incident timelines.

Capabilities exercised (all from ``sshpilot.plugins.api``):
* reacting to ``SESSION_OPENED`` / ``SESSION_CLOSED`` (``ctx.events``)
* per-plugin persisted settings (``ctx.settings``)
* a UI page (``ctx.ui.register_page``) and toasts (``ctx.ui.notify``)

Uses only the API-1 event/settings/UI surface, so it works on any sshPilot
build (no ``list_connections``/1.4 dependency).

Pure logic (``SessionLogStore``) has no GTK import and is unit-tested without
a display; ``gi`` is imported lazily inside the page factory.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sshpilot.plugins.api import Events, PluginContext, SshPilotPlugin

logger = logging.getLogger(__name__)

MAX_ENTRIES = 500


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_iso(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _duration_seconds(opened: str, closed: str) -> Optional[int]:
    start = _parse_iso(opened)
    end = _parse_iso(closed)
    if start is None or end is None:
        return None
    return max(0, int((end - start).total_seconds()))


def _format_duration(seconds: Optional[int]) -> str:
    if seconds is None:
        return "—"
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


# --- pure logic (no GTK) ----------------------------------------------------

class SessionLogStore:
    """Append-only session log with a fixed-size ring buffer."""

    def __init__(self, data: Any = None, *, max_entries: int = MAX_ENTRIES):
        self._max = max(1, int(max_entries))
        self._open: Dict[str, Dict[str, Any]] = {}
        self._entries: List[Dict[str, Any]] = []
        if isinstance(data, dict):
            raw = data.get("entries")
            if isinstance(raw, list):
                self._entries = [
                    dict(item) for item in raw[-self._max:]
                    if isinstance(item, dict)
                ]

    def as_dict(self) -> Dict[str, Any]:
        return {"entries": list(self._entries)}

    def record_open(self, *, session_id: str, nickname: str, host: str,
                    opened_at: Optional[str] = None) -> None:
        opened_at = opened_at or _utc_now_iso()
        self._open[session_id] = {
            "session_id": session_id,
            "nickname": nickname,
            "host": host,
            "opened_at": opened_at,
        }

    def record_close(self, *, session_id: str, closed_at: Optional[str] = None,
                     nickname: str = "", host: str = "") -> bool:
        closed_at = closed_at or _utc_now_iso()
        pending = self._open.pop(session_id, None)
        if pending is None:
            if not nickname and not host:
                return False
            pending = {
                "session_id": session_id,
                "nickname": nickname,
                "host": host,
                "opened_at": "",
            }
        entry = {
            "session_id": pending["session_id"],
            "nickname": pending.get("nickname") or nickname,
            "host": pending.get("host") or host,
            "opened_at": pending.get("opened_at") or "",
            "closed_at": closed_at,
        }
        entry["duration_seconds"] = _duration_seconds(
            entry["opened_at"], entry["closed_at"])
        self._entries.append(entry)
        if len(self._entries) > self._max:
            self._entries = self._entries[-self._max:]
        return True

    def entries(self) -> List[Dict[str, Any]]:
        return list(reversed(self._entries))

    def filter_entries(
        self,
        *,
        nickname: str = "",
        host: str = "",
        since: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        nick_q = (nickname or "").lower()
        host_q = (host or "").lower()
        out: List[Dict[str, Any]] = []
        for entry in self.entries():
            enick = (entry.get("nickname") or "").lower()
            ehost = (entry.get("host") or "").lower()
            if nick_q or host_q:
                if nick_q and nick_q == host_q:
                    if nick_q not in enick and nick_q not in ehost:
                        continue
                else:
                    if nick_q and nick_q not in enick:
                        continue
                    if host_q and host_q not in ehost:
                        continue
            if since is not None:
                opened = _parse_iso(entry.get("opened_at") or "")
                if opened is None or opened < since:
                    continue
            out.append(entry)
        return out

    def totals_by_connection(
        self, entries: Optional[Iterable[Dict[str, Any]]] = None,
    ) -> List[Tuple[str, int, int]]:
        """Return [(nickname, session_count, total_seconds), …] sorted by time."""
        source = list(entries) if entries is not None else self.entries()
        totals: Dict[str, Tuple[int, int]] = {}
        for entry in source:
            nick = entry.get("nickname") or entry.get("host") or "?"
            count, seconds = totals.get(nick, (0, 0))
            dur = entry.get("duration_seconds")
            if dur is None:
                dur = _duration_seconds(
                    entry.get("opened_at") or "", entry.get("closed_at") or "")
            totals[nick] = (count + 1, seconds + int(dur or 0))
        return sorted(
            [(nick, c, s) for nick, (c, s) in totals.items()],
            key=lambda item: item[2],
            reverse=True,
        )

    def export_csv(self, entries: Optional[Iterable[Dict[str, Any]]] = None) -> str:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "nickname", "host", "opened_at", "closed_at", "duration_seconds",
        ])
        for entry in (entries if entries is not None else self.entries()):
            writer.writerow([
                entry.get("nickname", ""),
                entry.get("host", ""),
                entry.get("opened_at", ""),
                entry.get("closed_at", ""),
                entry.get("duration_seconds", ""),
            ])
        return buf.getvalue()


# --- plugin -----------------------------------------------------------------

class Plugin(SshPilotPlugin):
    def activate(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self._store = SessionLogStore(ctx.settings.get("log", {}))
        self._list_box = None
        self._filter_entry = None
        self._status_label = None

        ctx.ui.register_page(
            "log", "Session Log", "view-list-symbolic", self._build_page)
        ctx.events.subscribe(Events.SESSION_OPENED, self._on_session_opened)
        ctx.events.subscribe(Events.SESSION_CLOSED, self._on_session_closed)

    def deactivate(self) -> None:
        logger.info("session-log: deactivate")

    def _persist(self) -> None:
        self.ctx.settings.set("log", self._store.as_dict())

    # --- event handlers (main thread) -------------------------------------
    def _on_session_opened(self, info) -> None:
        conn = info.connection
        self._store.record_open(
            session_id=info.session_id,
            nickname=conn.nickname,
            host=conn.host,
        )

    def _on_session_closed(self, info) -> None:
        conn = info.connection
        if self._store.record_close(
                session_id=info.session_id,
                nickname=conn.nickname,
                host=conn.host):
            self._persist()
            self._refresh_list()

    # --- UI (gi imported lazily) ------------------------------------------
    def _build_page(self):
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gtk

        self._Gtk = Gtk
        self._Adw = Adw

        outer = Gtk.ScrolledWindow()
        outer.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        for fn in (box.set_margin_top, box.set_margin_bottom,
                   box.set_margin_start, box.set_margin_end):
            fn(18)
        outer.set_child(box)

        title = Gtk.Label(label="Session Log")
        title.add_css_class("title-2")
        title.set_halign(Gtk.Align.START)
        box.append(title)

        subtitle = Gtk.Label(
            label="A lightweight history of terminal sessions — when each "
                  "connection opened and closed. Export CSV for billing or "
                  "incident review.")
        subtitle.add_css_class("dim-label")
        subtitle.set_halign(Gtk.Align.START)
        subtitle.set_wrap(True)
        subtitle.set_xalign(0)
        box.append(subtitle)

        filter_group = Adw.PreferencesGroup(title="Filter")
        self._filter_entry = Adw.EntryRow(title="Nickname or host contains…")
        self._filter_entry.connect("changed", self._on_filter_changed)
        filter_group.add(self._filter_entry)
        box.append(filter_group)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        export_btn = Gtk.Button(label="Export CSV")
        export_btn.connect("clicked", self._on_export_clicked)
        actions.append(export_btn)
        clear_btn = Gtk.Button(label="Clear log")
        clear_btn.connect("clicked", self._on_clear_clicked)
        actions.append(clear_btn)
        box.append(actions)

        totals_label = Gtk.Label(label="Totals (filtered)")
        totals_label.add_css_class("heading")
        totals_label.set_halign(Gtk.Align.START)
        box.append(totals_label)

        self._totals_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.append(self._totals_box)

        history_label = Gtk.Label(label="Recent sessions")
        history_label.add_css_class("heading")
        history_label.set_halign(Gtk.Align.START)
        box.append(history_label)

        self._list_box = Gtk.ListBox()
        self._list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self._list_box.add_css_class("boxed-list")
        box.append(self._list_box)

        self._status_label = Gtk.Label(label="")
        self._status_label.add_css_class("dim-label")
        self._status_label.set_halign(Gtk.Align.START)
        box.append(self._status_label)

        self._refresh_list()
        return outer

    def _filter_text(self) -> str:
        if self._filter_entry is None:
            return ""
        return self._filter_entry.get_text().strip().lower()

    def _filtered_entries(self) -> List[Dict[str, Any]]:
        query = self._filter_text()
        if not query:
            return self._store.entries()
        return self._store.filter_entries(nickname=query, host=query)

    def _on_filter_changed(self, *_args) -> None:
        self._refresh_list()

    def _refresh_list(self) -> None:
        if self._list_box is None:
            return
        Gtk = self._Gtk
        while child := self._list_box.get_first_child():
            self._list_box.remove(child)
        while child := self._totals_box.get_first_child():
            self._totals_box.remove(child)

        entries = self._filtered_entries()
        totals = self._store.totals_by_connection(entries)
        if not totals:
            self._totals_box.append(Gtk.Label(
                label="No sessions recorded yet.",
                xalign=0))
        else:
            for nick, count, seconds in totals[:10]:
                self._totals_box.append(Gtk.Label(
                    label=f"{nick}: {count} session(s), "
                          f"{_format_duration(seconds)} total",
                    xalign=0))

        if not entries:
            self._list_box.append(Gtk.Label(
                label="No matching sessions.",
                xalign=0))
            return

        for entry in entries[:100]:
            row = Gtk.ListBoxRow()
            opened = entry.get("opened_at") or "?"
            closed = entry.get("closed_at") or "?"
            dur = _format_duration(entry.get("duration_seconds"))
            label = Gtk.Label(
                label=(
                    f"{entry.get('nickname') or '?'} "
                    f"({entry.get('host') or '?'}) — "
                    f"{opened} → {closed} ({dur})"
                ),
                xalign=0, wrap=True)
            label.set_margin_top(6)
            label.set_margin_bottom(6)
            row.set_child(label)
            self._list_box.append(row)

    def _on_export_clicked(self, _btn) -> None:
        csv_text = self._store.export_csv(self._filtered_entries())
        self._export_csv(csv_text)

    def _export_csv(self, csv_text: str) -> None:
        """Save the log to a CSV file via a save dialog. Falls back to the
        clipboard only when there's no display (headless)."""
        import gi
        gi.require_version("Gdk", "4.0")
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gdk, Gtk

        if Gdk.Display.get_default() is None:
            return self._copy_to_clipboard(csv_text)

        dialog = Gtk.FileDialog(title="Save session log")
        dialog.set_initial_name("sshpilot-session-log.csv")

        def on_save(dlg, result):
            import gi
            gi.require_version("GLib", "2.0")
            from gi.repository import GLib
            try:
                file = dlg.save_finish(result)
            except GLib.Error:
                return  # user cancelled
            path = file.get_path()
            if not path:
                return
            try:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(csv_text)
            except OSError as exc:
                self._set_status(f"Could not save: {exc}")
                self.ctx.ui.notify("Session log export failed")
                return
            self._set_status(f"Saved to {path}")
            self.ctx.ui.notify("Session log saved")

        dialog.save(None, None, on_save)

    def _copy_to_clipboard(self, csv_text: str) -> None:
        import gi
        gi.require_version("Gdk", "4.0")
        from gi.repository import Gdk

        display = Gdk.Display.get_default()
        if display is None:
            return
        display.get_clipboard().set(csv_text)
        self._set_status("CSV copied to clipboard.")
        self.ctx.ui.notify("Session log copied to clipboard")

    def _on_clear_clicked(self, _btn) -> None:
        self._store = SessionLogStore()
        self._persist()
        self._refresh_list()
        self._set_status("Log cleared.")
        self.ctx.ui.notify("Session log cleared")

    def _set_status(self, text: str) -> None:
        if self._status_label is not None:
            self._status_label.set_text(text)
