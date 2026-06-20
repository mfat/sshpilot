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
import math
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


def rgba_to_hex(red: float, green: float, blue: float) -> str:
    """Convert 0–1 RGBA components to a ``#rrggbb`` string. Alpha is dropped — the
    sidebar parses the value with ``Gdk.RGBA.parse``, and hex keeps the stored
    settings readable."""
    def channel(value: float) -> int:
        return max(0, min(255, round(value * 255)))
    return "#{:02x}{:02x}{:02x}".format(channel(red), channel(green), channel(blue))


def move_rule(rules: List[Dict[str, Any]], index: int, delta: int) -> int:
    """Move the rule at ``index`` by ``delta`` (e.g. -1 up, +1 down), clamped to
    the list bounds. Order matters because the first matching rule wins. Returns
    the new index (unchanged if the move is a no-op)."""
    if not (0 <= index < len(rules)):
        return index
    target = max(0, min(len(rules) - 1, index + delta))
    if target != index:
        rules.insert(target, rules.pop(index))
    return target


# --- plugin -----------------------------------------------------------------

class Plugin(SshPilotPlugin):
    def activate(self, ctx: PluginContext) -> None:
        # Registration only — no live UI/connection work here.
        self.ctx = ctx
        self._rules: List[Dict[str, Any]] = normalize_rules(
            ctx.settings.get("rules", []))
        self._rules_group = None
        self._rule_rows: List[Any] = []

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
        # Only announce success — _assign returns False if the group couldn't be
        # created/assigned (e.g. UI not ready), and a false toast is misleading.
        if self._assign(info.nickname, rule):
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

        page = Adw.PreferencesPage()
        page.set_title("Auto-Grouping")

        # How it works.
        help_group = Adw.PreferencesGroup(
            title="Automatic grouping",
            description=(
                "When a connection is created, its nickname and host are matched "
                "against the patterns below. The first matching rule wins, and the "
                "connection is moved into that group (created if needed) with the "
                "chosen colour."))
        examples = Adw.ExpanderRow(
            title="Pattern help & examples",
            subtitle="Shell-style globs — * matches anything, ? one character")
        for pat, desc in (
            ("*.prod.*", "hosts on a prod subdomain (web.prod.example.com)"),
            ("prod-*", "nicknames starting with prod-"),
            ("10.0.*", "hosts in the 10.0.x.x range"),
            ("*", "catch-all — keep this rule last"),
        ):
            examples.add_row(Adw.ActionRow(title=pat, subtitle=desc))
        help_group.add(examples)
        page.add(help_group)

        # Rules, with an Add button in the group header.
        self._rules_group = Adw.PreferencesGroup(title="Rules")
        add_btn = Gtk.Button(icon_name="list-add-symbolic")
        add_btn.add_css_class("flat")
        add_btn.set_tooltip_text("Add rule")
        add_btn.connect("clicked", lambda _b: self._open_rule_dialog())
        self._rules_group.set_header_suffix(add_btn)
        page.add(self._rules_group)
        self._rebuild_rule_rows()

        # Backfill.
        actions_group = Adw.PreferencesGroup()
        apply_row = Adw.ActionRow(
            title="Apply rules to existing connections",
            subtitle="Sort already-saved connections using the rules above")
        apply_row.set_activatable(True)
        apply_row.add_prefix(Gtk.Image.new_from_icon_name("view-refresh-symbolic"))
        apply_row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        apply_row.connect("activated", lambda _r: self._on_apply_existing())
        actions_group.add(apply_row)
        page.add(actions_group)

        return page

    def _rebuild_rule_rows(self) -> None:
        Adw, Gtk = self._Adw, self._Gtk
        group = self._rules_group
        if group is None:
            return
        for row in self._rule_rows:
            group.remove(row)
        self._rule_rows = []

        if not self._rules:
            empty = Adw.ActionRow(
                title="No rules yet",
                subtitle="Click the + button above to add your first rule.")
            group.add(empty)
            self._rule_rows.append(empty)
            return

        last = len(self._rules) - 1
        for index, rule in enumerate(self._rules):
            row = Adw.ActionRow(title=rule["pattern"], subtitle=f"→ {rule['group']}")
            row.set_activatable(True)
            row.connect("activated", lambda _r, i=index: self._open_rule_dialog(i))
            row.add_prefix(self._color_swatch(rule.get("color")))

            up = self._flat_icon_button("go-up-symbolic", "Move up",
                                        self._on_move_clicked, index, -1)
            up.set_sensitive(index > 0)
            down = self._flat_icon_button("go-down-symbolic", "Move down",
                                          self._on_move_clicked, index, 1)
            down.set_sensitive(index < last)
            delete = self._flat_icon_button("user-trash-symbolic", "Delete rule",
                                            self._on_delete_clicked, index)
            delete.add_css_class("error")
            for button in (up, down, delete):
                row.add_suffix(button)
            group.add(row)
            self._rule_rows.append(row)

    def _flat_icon_button(self, icon_name, tooltip, callback, *args):
        Gtk = self._Gtk
        button = Gtk.Button(icon_name=icon_name)
        button.add_css_class("flat")
        button.set_valign(Gtk.Align.CENTER)
        button.set_tooltip_text(tooltip)
        button.connect("clicked", lambda _b: callback(*args))
        return button

    def _color_swatch(self, color):
        from gi.repository import Gdk
        Gtk = self._Gtk
        area = Gtk.DrawingArea()
        area.set_content_width(18)
        area.set_content_height(18)
        area.set_valign(Gtk.Align.CENTER)
        rgba = Gdk.RGBA()
        has_color = bool(color) and rgba.parse(color)

        def draw(_area, cr, width, height):
            radius = min(width, height) / 2 - 1
            cr.arc(width / 2, height / 2, radius, 0, 2 * math.pi)
            if has_color:
                cr.set_source_rgba(rgba.red, rgba.green, rgba.blue, 1.0)
                cr.fill()
            else:
                cr.set_source_rgba(0.6, 0.6, 0.6, 0.5)
                cr.set_line_width(1.0)
                cr.stroke()

        area.set_draw_func(draw)
        return area

    # --- add / edit dialog ------------------------------------------------
    def _open_rule_dialog(self, index=None):
        Adw, Gtk = self._Adw, self._Gtk
        from gi.repository import Gdk

        editing = index is not None
        existing = self._rules[index] if editing else {}

        pattern_row = Adw.EntryRow(title="Pattern")
        pattern_row.set_text(existing.get("pattern", ""))
        group_row = Adw.EntryRow(title="Group name")
        group_row.set_text(existing.get("group", ""))

        color_switch = Adw.SwitchRow(title="Assign a colour")
        color_switch.set_active(bool(existing.get("color")))

        rgba = Gdk.RGBA()
        rgba.parse(existing.get("color") or "#3584e4")
        if hasattr(Gtk, "ColorDialogButton"):
            color_button = Gtk.ColorDialogButton.new(Gtk.ColorDialog.new())
        else:  # GTK < 4.10
            color_button = Gtk.ColorButton()
        color_button.set_rgba(rgba)
        color_button.set_valign(Gtk.Align.CENTER)
        color_row = Adw.ActionRow(title="Colour")
        color_row.add_suffix(color_button)
        color_row.set_sensitive(color_switch.get_active())
        color_switch.connect(
            "notify::active",
            lambda s, _p: color_row.set_sensitive(s.get_active()))

        form = Adw.PreferencesGroup()
        for r in (pattern_row, group_row, color_switch, color_row):
            form.add(r)

        def read_values():
            color = None
            if color_switch.get_active():
                c = color_button.get_rgba()
                color = rgba_to_hex(c.red, c.green, c.blue)
            return (pattern_row.get_text().strip(),
                    group_row.get_text().strip(), color)

        def commit():
            pattern, grp, color = read_values()
            if not pattern or not grp:
                return False
            rule = {"pattern": pattern, "group": grp}
            if color:
                rule["color"] = color
            if editing:
                self._rules[index] = rule
            else:
                self._rules.append(rule)
            self._save_rules()
            self._rebuild_rule_rows()
            self.ctx.ui.notify(
                f"Rule {'updated' if editing else 'added'}: {pattern} → {grp}")
            return True

        title = "Edit Rule" if editing else "Add Rule"
        save_label = "Save" if editing else "Add"
        parent = self._rules_group.get_root()

        if hasattr(Adw, "Dialog"):
            dialog = Adw.Dialog()
            dialog.set_title(title)
            dialog.set_content_width(420)
            toolbar = Adw.ToolbarView()
            header = Adw.HeaderBar()
            header.set_show_end_title_buttons(False)
            cancel = Gtk.Button(label="Cancel")
            cancel.connect("clicked", lambda _b: dialog.close())
            save = Gtk.Button(label=save_label)
            save.add_css_class("suggested-action")
            header.pack_start(cancel)
            header.pack_end(save)
            toolbar.add_top_bar(header)
            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            for fn in (content.set_margin_top, content.set_margin_bottom,
                       content.set_margin_start, content.set_margin_end):
                fn(12)
            content.append(form)
            toolbar.set_content(content)
            dialog.set_child(toolbar)

            def validate(*_a):
                pattern, grp, _c = read_values()
                save.set_sensitive(bool(pattern and grp))
            pattern_row.connect("changed", validate)
            group_row.connect("changed", validate)
            validate()

            save.connect("clicked", lambda _b: dialog.close() if commit() else None)
            dialog.present(parent)
        else:  # libadwaita < 1.5
            dialog = Adw.MessageDialog.new(parent, title, None)
            dialog.set_extra_child(form)
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("save", save_label)
            dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
            dialog.set_default_response("save")
            dialog.connect("response",
                           lambda _d, resp: commit() if resp == "save" else None)
            dialog.present()

    # --- row actions ------------------------------------------------------
    def _on_move_clicked(self, index, delta):
        if move_rule(self._rules, index, delta) != index:
            self._save_rules()
            self._rebuild_rule_rows()

    def _on_delete_clicked(self, index):
        if 0 <= index < len(self._rules):
            removed = self._rules.pop(index)
            self._save_rules()
            self._rebuild_rule_rows()
            self.ctx.ui.notify(f"Removed rule {removed['pattern']}")

    def _on_apply_existing(self):
        applied = 0
        for info in self.ctx.list_connections():
            rule = match_group(info.nickname, info.host, self._rules)
            if rule is not None and self._assign(info.nickname, rule):
                applied += 1
        self.ctx.ui.notify(f"Auto-grouped {applied} connection(s)")
