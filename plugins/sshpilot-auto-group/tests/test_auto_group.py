"""Tests for Smart Auto-Grouping. Pure logic is tested directly; the plugin's
event handling is tested against a fake PluginContext. No GTK required (gi is
imported lazily, only inside the page factory)."""

import importlib.util
import os

HERE = os.path.dirname(__file__)


def _load():
    spec = importlib.util.spec_from_file_location(
        "auto_group_plugin", os.path.join(HERE, "..", "__init__.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Info:
    def __init__(self, nickname, host):
        self.nickname = nickname
        self.host = host


class _Settings:
    def __init__(self, data=None):
        self._data = dict(data or {})

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value


class _Ctx:
    """Minimal fake of the bits the plugin uses."""

    def __init__(self, settings=None, connections=()):
        self.settings = _Settings(settings)
        self._connections = list(connections)
        self.created_groups = []
        self.assignments = []
        self.notes = []
        self.subscribed = {}
        self.pages = []
        self.ui = self
        self.events = self

    # ui facade
    def register_page(self, page_id, title, icon, factory):
        self.pages.append(page_id)

    def notify(self, message, timeout=3):
        self.notes.append(message)

    # events facade
    def subscribe(self, event, callback):
        self.subscribed[event] = callback

    # connection/group facade
    def create_group(self, name, color=None):
        self.created_groups.append((name, color))
        return f"gid:{name}"

    def add_connection_to_group(self, nickname, group_id):
        self.assignments.append((nickname, group_id))
        return True

    def list_connections(self):
        return list(self._connections)


def test_match_group_first_match_wins():
    mod = _load()
    rules = [
        {"pattern": "*.dev.*", "group": "Dev"},
        {"pattern": "*.prod.*", "group": "Production"},
        {"pattern": "*", "group": "Catch-all"},
    ]
    assert mod.match_group("web", "web.prod.example.com", rules)["group"] == "Production"
    assert mod.match_group("db", "db.dev.example.com", rules)["group"] == "Dev"
    assert mod.match_group("misc", "example.org", rules)["group"] == "Catch-all"


def test_match_group_matches_nickname_too():
    mod = _load()
    rules = [{"pattern": "prod-*", "group": "Production"}]
    assert mod.match_group("prod-web", "10.0.0.1", rules)["group"] == "Production"
    assert mod.match_group("staging", "10.0.0.2", rules) is None


def test_match_group_is_case_insensitive():
    mod = _load()
    rules = [{"pattern": "*.PROD.*", "group": "Production"}]
    assert mod.match_group("X", "web.prod.example.com", rules) is not None


def test_normalize_rules_drops_malformed():
    mod = _load()
    raw = [
        {"pattern": "a*", "group": "A", "color": "#fff"},
        {"pattern": "", "group": "B"},     # no pattern
        {"pattern": "c*", "group": ""},    # no group
        "not-a-dict",
        {"pattern": "d*", "group": "D"},
    ]
    out = mod.normalize_rules(raw)
    assert [r["pattern"] for r in out] == ["a*", "d*"]
    assert out[0]["color"] == "#fff"


def test_activate_registers_page_and_subscribes():
    mod = _load()
    ctx = _Ctx(settings={"rules": [{"pattern": "*.prod.*", "group": "Production"}]})
    mod.Plugin().activate(ctx)
    assert "rules" in ctx.pages
    assert mod.Events.CONNECTION_CREATED in ctx.subscribed


def test_connection_created_assigns_to_group():
    mod = _load()
    ctx = _Ctx(settings={"rules": [{"pattern": "*.prod.*", "group": "Production",
                                    "color": "#e01b24"}]})
    plugin = mod.Plugin()
    plugin.activate(ctx)
    handler = ctx.subscribed[mod.Events.CONNECTION_CREATED]

    handler(_Info("web", "web.prod.example.com"))
    assert ctx.created_groups == [("Production", "#e01b24")]
    assert ctx.assignments == [("web", "gid:Production")]

    # A non-matching connection is left alone.
    handler(_Info("laptop", "192.168.1.5"))
    assert len(ctx.assignments) == 1


def test_apply_to_existing_backfills(monkeypatch):
    mod = _load()
    conns = [_Info("web", "web.prod.example.com"),
             _Info("db", "db.prod.example.com"),
             _Info("home", "10.0.0.9")]
    ctx = _Ctx(settings={"rules": [{"pattern": "*.prod.*", "group": "Production"}]},
               connections=conns)
    plugin = mod.Plugin()
    plugin.activate(ctx)
    plugin._on_apply_existing()
    assert [a[0] for a in ctx.assignments] == ["web", "db"]


def test_rgba_to_hex():
    mod = _load()
    assert mod.rgba_to_hex(0.0, 0.0, 0.0) == "#000000"
    assert mod.rgba_to_hex(1.0, 1.0, 1.0) == "#ffffff"
    # Adwaita blue 0x3584e4 → components 53/132/228 over 255.
    assert mod.rgba_to_hex(53 / 255, 132 / 255, 228 / 255) == "#3584e4"
    # Out-of-range values are clamped, not wrapped.
    assert mod.rgba_to_hex(-0.5, 2.0, 0.5) == "#00ff80"


def test_move_rule_reorders_and_clamps():
    mod = _load()
    rules = [{"pattern": "a", "group": "A"},
             {"pattern": "b", "group": "B"},
             {"pattern": "c", "group": "C"}]
    assert mod.move_rule(rules, 2, -1) == 1          # c moves up
    assert [r["pattern"] for r in rules] == ["a", "c", "b"]
    assert mod.move_rule(rules, 0, -1) == 0          # already at top — no-op
    assert [r["pattern"] for r in rules] == ["a", "c", "b"]
    assert mod.move_rule(rules, 1, 99) == 2          # clamp to last
    assert [r["pattern"] for r in rules] == ["a", "b", "c"]
    assert mod.move_rule(rules, 5, -1) == 5          # out-of-range — no-op
