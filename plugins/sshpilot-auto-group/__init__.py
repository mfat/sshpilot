"""Smart Auto-Grouping — sort new connections into sidebar groups by rule.

A non-protocol sshPilot plugin. You define rules like ``*.prod.*`` → *Production*;
whenever a connection is created whose nickname or host matches, it's dropped
into the matching group automatically. An "Apply to existing connections" button
backfills hosts you added before installing the plugin.

Capabilities exercised (all from ``sshpilot.plugins.api``):
* reacting to ``CONNECTION_CREATED`` (``ctx.events``)
* sidebar groups (``ctx.create_group`` / ``ctx.add_connection_to_group``)
* enumerating saved hosts (``ctx.list_connections`` — needs app API >= 1.4)
* per-plugin persisted settings (``ctx.settings``)
* a UI page (``ctx.ui.register_page``) and toasts (``ctx.ui.notify``)

Pure logic (``match_group``) lives at module top with no GTK import, so it's
unit-testable without a display; ``gi`` is imported lazily inside the page
factory, which only runs inside the running app.
"""

from __future__ import annotations

import fnmatch
import logging
from typing import Any, Dict, List, Optional

from sshpilot.plugins.api import Events, PluginContext, SshPilotPlugin

logger = logging.getLogger(__name__)


# --- pure logic (no GTK) ----------------------------------------------------

def match_group(nickname: str, host: str,
                rules: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Return the first rule whose glob pattern matches the connection's
    nickname or host (case-insensitive), or None. A rule is
    ``{"pattern": str, "group": str, "color": Optional[str]}``; rules without a
    pattern or group are skipped."""
    haystack = [(nickname or "").lower(), (host or "").lower()]
    for rule in rules or []:
        pattern = (rule.get("pattern") or "").strip().lower()
        if not pattern or not (rule.get("group") or "").strip():
            continue
        if any(fnmatch.fnmatch(value, pattern) for value in haystack):
            return rule
    return None


def normalize_rules(raw: Any) -> List[Dict[str, Any]]:
    """Coerce whatever is stored in settings into a clean rules list, dropping
    malformed entries (settings round-trips through JSON, so be defensive)."""
    out: List[Dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        pattern = str(item.get("pattern", "")).strip()
        group = str(item.get("group", "")).strip()
        if not pattern or not group:
            continue
        rule = {"pattern": pattern, "group": group}
        color = item.get("color")
        if color:
            rule["color"] = str(color)
        out.append(rule)
    return out


# --- plugin -----------------------------------------------------------------

class Plugin(SshPilotPlugin):
    def activate(self, ctx: PluginContext) -> None:
        # Registration only — no live UI/connection work here.
        self.ctx = ctx
        self._rules: List[Dict[str, Any]] = normalize_rules(
            ctx.settings.get("rules", []))
        self._rows_box = None
        self._status_label = None

        ctx.ui.register_page(
            "rules", "Auto-Grouping", "view-list-bullet-symbolic",
            self._build_page)
        ctx.events.subscribe(Events.CONNECTION_CREATED, self._on_connection_created)

    def deactivate(self) -> None:
        logger.info("auto-group: deactivate")

    # --- persistence ------------------------------------------------------
    def _save_rules(self) -> None:
        self._rules = normalize_rules(self._rules)
        self.ctx.settings.set("rules", self._rules)

    # --- event handler (main thread) -------------------------------------
    def _on_connection_created(self, info) -> None:
        rule = match_group(info.nickname, info.host, self._rules)
        if rule is None:
            return
        self._assign(info.nickname, rule)
        self.ctx.ui.notify(f"Added {info.nickname} to {rule['group']}")

    def _assign(self, nickname: str, rule: Dict[str, Any]) -> bool:
        group_id = self.ctx.create_group(rule["group"], rule.get("color"))
        if not group_id:
            return False
        return bool(self.ctx.add_connection_to_group(nickname, group_id))

    # --- UI (gi imported lazily; only runs inside the app) ----------------
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

        title = Gtk.Label(label="Auto-Grouping Rules")
        title.add_css_class("title-2")
        title.set_halign(Gtk.Align.START)
        box.append(title)

        subtitle = Gtk.Label(
            label="New connections whose nickname or host matches a pattern "
                  "are added to the matching group automatically.")
        subtitle.add_css_class("dim-label")
        subtitle.set_halign(Gtk.Align.START)
        subtitle.set_wrap(True)
        subtitle.set_xalign(0)
        box.append(subtitle)

        # Existing rules.
        self._rules_group = Adw.PreferencesGroup(title="Rules")
        box.append(self._rules_group)
        self._rebuild_rule_rows()

        # Add-a-rule form.
        add_group = Adw.PreferencesGroup(title="Add a rule")
        self._pattern_entry = Adw.EntryRow(title="Pattern (e.g. *.prod.*)")
        self._group_entry = Adw.EntryRow(title="Group name")
        self._color_entry = Adw.EntryRow(title="Color (optional, #rrggbb)")
        add_group.add(self._pattern_entry)
        add_group.add(self._group_entry)
        add_group.add(self._color_entry)
        add_btn = Gtk.Button(label="Add rule")
        add_btn.add_css_class("suggested-action")
        add_btn.set_halign(Gtk.Align.START)
        add_btn.connect("clicked", self._on_add_clicked)
        add_group.add(add_btn)
        box.append(add_group)

        # Backfill action.
        apply_btn = Gtk.Button(label="Apply to existing connections")
        apply_btn.set_halign(Gtk.Align.START)
        apply_btn.connect("clicked", self._on_apply_existing_clicked)
        box.append(apply_btn)

        self._status_label = Gtk.Label(label="")
        self._status_label.add_css_class("dim-label")
        self._status_label.set_halign(Gtk.Align.START)
        box.append(self._status_label)

        return outer

    def _rebuild_rule_rows(self) -> None:
        Adw, Gtk = self._Adw, self._Gtk
        group = self._rules_group
        # Drop previously added rows.
        for row in getattr(self, "_rule_rows", []):
            group.remove(row)
        self._rule_rows = []
        if not self._rules:
            empty = Adw.ActionRow(title="No rules yet")
            empty.set_subtitle("Add one below.")
            group.add(empty)
            self._rule_rows.append(empty)
            return
        for index, rule in enumerate(self._rules):
            row = Adw.ActionRow(title=rule["pattern"])
            row.set_subtitle(f"→ {rule['group']}")
            delete = Gtk.Button(icon_name="user-trash-symbolic")
            delete.add_css_class("flat")
            delete.set_valign(Gtk.Align.CENTER)
            delete.connect("clicked", self._on_delete_clicked, index)
            row.add_suffix(delete)
            group.add(row)
            self._rule_rows.append(row)

    def _on_add_clicked(self, _btn) -> None:
        pattern = self._pattern_entry.get_text().strip()
        group = self._group_entry.get_text().strip()
        color = self._color_entry.get_text().strip()
        if not pattern or not group:
            self._set_status("A pattern and a group name are required.")
            return
        rule = {"pattern": pattern, "group": group}
        if color:
            rule["color"] = color
        self._rules.append(rule)
        self._save_rules()
        self._pattern_entry.set_text("")
        self._group_entry.set_text("")
        self._color_entry.set_text("")
        self._rebuild_rule_rows()
        self._set_status(f"Added rule {pattern} → {group}")

    def _on_delete_clicked(self, _btn, index: int) -> None:
        if 0 <= index < len(self._rules):
            removed = self._rules.pop(index)
            self._save_rules()
            self._rebuild_rule_rows()
            self._set_status(f"Removed rule {removed['pattern']}")

    def _on_apply_existing_clicked(self, _btn) -> None:
        applied = 0
        for info in self.ctx.list_connections():
            rule = match_group(info.nickname, info.host, self._rules)
            if rule is not None and self._assign(info.nickname, rule):
                applied += 1
        self._set_status(f"Applied rules to {applied} connection(s).")
        self.ctx.ui.notify(f"Auto-grouped {applied} connection(s)")

    def _set_status(self, text: str) -> None:
        if self._status_label is not None:
            self._status_label.set_text(text)
