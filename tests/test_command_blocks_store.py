"""Characterization tests for command_blocks.CommandBlockStore.

These pin the *current* behavior of the JSON-backed command/folder store (which
is explicitly GTK-free) before any refactor. They are not aspirational — each
assertion mirrors what the code does today, including quirks.
"""

import pytest

command_blocks = pytest.importorskip("sshpilot.command_blocks")
CommandBlockStore = command_blocks.CommandBlockStore
PlaceholderDialog = command_blocks.PlaceholderDialog


class FakeConfig:
    """Minimal stand-in for Config: a dict backing store + a save counter."""

    def __init__(self, initial=None):
        self.config_data = {} if initial is None else dict(initial)
        self.saves = 0

    def save_json_config(self):
        self.saves += 1


def _seeded_store():
    """Store with defaults already marked loaded, so CRUD tests start empty."""
    cfg = FakeConfig({
        "command_blocks": {"folders": [], "commands": [], "defaults_loaded": True}
    })
    return CommandBlockStore(cfg), cfg


# --- defaults / initialization -------------------------------------------


def test_fresh_config_loads_defaults_and_persists():
    cfg = FakeConfig()
    store = CommandBlockStore(cfg)
    data = cfg.config_data["command_blocks"]
    assert data["defaults_loaded"] is True
    # Default folders/commands were seeded from the module constants.
    assert len(data["folders"]) == len(command_blocks.DEFAULT_FOLDERS)
    assert len(data["commands"]) == len(command_blocks.DEFAULT_COMMANDS)
    assert cfg.saves >= 1


def test_existing_commands_skip_default_seeding():
    cfg = FakeConfig({
        "command_blocks": {
            "folders": [],
            "commands": [{"id": "x", "name": "keep", "command": "echo"}],
            "defaults_loaded": False,
        }
    })
    CommandBlockStore(cfg)
    data = cfg.config_data["command_blocks"]
    # Pre-existing commands short-circuit seeding: no defaults added.
    assert [c["id"] for c in data["commands"]] == ["x"]
    assert data["defaults_loaded"] is True


def test_data_repairs_missing_block():
    cfg = FakeConfig({"command_blocks": "not-a-dict"})
    store = CommandBlockStore(cfg)
    assert isinstance(cfg.config_data["command_blocks"], dict)
    assert "folders" in cfg.config_data["command_blocks"]


# --- command CRUD ---------------------------------------------------------


def test_add_command_shape_and_defaults():
    store, cfg = _seeded_store()
    saves_before = cfg.saves
    entry = store.add_command("List", "ls -la", description="d", tags=["a", "b"])
    assert entry["name"] == "List"
    assert entry["command"] == "ls -la"
    assert entry["description"] == "d"
    assert entry["tags"] == ["a", "b"]
    assert entry["use_count"] == 0
    assert entry["last_used"] is None
    assert entry["is_favorite"] is False
    assert entry["id"] and entry["created_at"]
    assert store.get_commands()[-1]["id"] == entry["id"]
    assert cfg.saves == saves_before + 1


def test_update_command_only_allowed_keys():
    store, _ = _seeded_store()
    e = store.add_command("n", "c")
    store.update_command(e["id"], name="n2", command="c2", use_count=999, bogus="x")
    updated = store.get_commands()[0]
    assert updated["name"] == "n2"
    assert updated["command"] == "c2"
    # use_count and unknown keys are NOT writable via update_command.
    assert updated["use_count"] == 0
    assert "bogus" not in updated


def test_update_command_unknown_id_is_noop():
    store, _ = _seeded_store()
    store.add_command("n", "c")
    store.update_command("nope", name="x")  # must not raise
    assert store.get_commands()[0]["name"] == "n"


def test_delete_command():
    store, _ = _seeded_store()
    a = store.add_command("a", "ca")
    b = store.add_command("b", "cb")
    store.delete_command(a["id"])
    assert [c["id"] for c in store.get_commands()] == [b["id"]]


def test_duplicate_command_names_and_resets_usage():
    store, _ = _seeded_store()
    e = store.add_command("Orig", "cmd")
    store.record_use(e["id"])
    dup = store.duplicate_command(e["id"])
    assert dup is not None
    assert dup["name"] == "Orig (copy)"
    assert dup["id"] != e["id"]
    assert dup["use_count"] == 0
    assert dup["last_used"] is None
    assert len(store.get_commands()) == 2


def test_duplicate_missing_returns_none():
    store, _ = _seeded_store()
    assert store.duplicate_command("nope") is None


def test_record_use_increments_and_stamps():
    store, _ = _seeded_store()
    e = store.add_command("n", "c")
    store.record_use(e["id"])
    store.record_use(e["id"])
    got = store.get_commands()[0]
    assert got["use_count"] == 2
    assert got["last_used"] is not None


# --- queries --------------------------------------------------------------


def test_search_empty_returns_all_and_substring_matches_fields():
    store, _ = _seeded_store()
    store.add_command("Deploy", "kubectl apply", description="ship it", tags=["prod"])
    store.add_command("Logs", "journalctl -f", description="tail", tags=["debug"])
    assert len(store.search("")) == 2
    assert len(store.search("   ")) == 2  # whitespace-only == empty
    assert [c["name"] for c in store.search("kubectl")] == ["Deploy"]     # command field
    assert [c["name"] for c in store.search("PROD")] == ["Deploy"]        # tag, case-insensitive
    assert [c["name"] for c in store.search("tail")] == ["Logs"]          # description
    assert store.search("nomatch") == []


def test_get_favorites():
    store, _ = _seeded_store()
    store.add_command("plain", "c")
    fav = store.add_command("fav", "c", is_favorite=True)
    assert [c["id"] for c in store.get_favorites()] == [fav["id"]]


# --- folders --------------------------------------------------------------


def test_add_folder_shape_and_order():
    store, _ = _seeded_store()
    f1 = store.add_folder("Prod")
    f2 = store.add_folder("Dev", parent_id=f1["id"])
    assert f1["name"] == "Prod" and f1["order"] == 0
    assert f2["parent_id"] == f1["id"] and f2["order"] == 1
    assert [f["id"] for f in store.get_folders()] == [f1["id"], f2["id"]]


def test_delete_folder():
    store, _ = _seeded_store()
    f = store.add_folder("X")
    store.delete_folder(f["id"])
    assert store.get_folders() == []


# --- placeholder parsing (pure helper on PlaceholderDialog) ---------------


def test_parse_placeholders_extracts_unique_in_order():
    parse = PlaceholderDialog._parse_placeholders
    assert parse(None, "ssh ${HOST} -p ${PORT}") == ["HOST", "PORT"]
    # duplicates collapse, first-seen order preserved
    assert parse(None, "${A} ${B} ${A}") == ["A", "B"]
    assert parse(None, "no placeholders here") == []
