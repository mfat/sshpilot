"""The settings-file version policy (core.settings.versioning + store).

Fixtures cover every version this build has shipped (0-3), a future
version, malformed input, a failing upgrade step, a failing backup, and
repeated execution. The rule under test throughout: the user's file is
either upgraded with its original bytes kept in a new backup, or left
byte-for-byte unchanged.
"""
from __future__ import annotations

import json
import threading

import pytest

from sshpilot.core.settings import (
    CONFIG_VERSION,
    SettingsFileError,
    SettingsMigrationError,
    SettingsVersionError,
    load_settings,
    load_settings_strict,
    migrate_settings_file,
    save_settings,
    upgrade_settings,
)
from sshpilot.core.settings import store, versioning

USER_VALUES = {
    "terminal": {"theme": "solarized", "font": "Iosevka 13"},
    "ui": {"language": "de"},
    "ssh": {"batch_mode": True},
}

# One tree per shipped version. Version 0 is a file from before
# config_version existed; version 2 predates the shortcuts key.
VERSION_FIXTURES = {
    0: dict(USER_VALUES),
    1: {"config_version": 1, **USER_VALUES},
    2: {"config_version": 2, **USER_VALUES},
    3: {"config_version": 3, "shortcuts": {"quit": ["<Ctrl>q"]}, **USER_VALUES},
}


def _write(path, tree):
    path.write_text(json.dumps(tree, indent=2), encoding="utf-8")
    return path.read_bytes()


def _backups(directory):
    return sorted(directory.glob("config.json.v*.bak"))


def _assert_user_values_kept(tree):
    for section, values in USER_VALUES.items():
        for key, value in values.items():
            assert tree[section][key] == value


# -- the registry -------------------------------------------------------------


def test_every_older_version_has_an_upgrade_step():
    """Bumping CONFIG_VERSION without a step for the old version fails here."""
    assert sorted(versioning.MIGRATIONS) == list(range(CONFIG_VERSION))


def test_fixtures_cover_every_shipped_version():
    assert sorted(VERSION_FIXTURES) == list(range(CONFIG_VERSION + 1))


# -- pure upgrade -------------------------------------------------------------


@pytest.mark.parametrize("version", sorted(VERSION_FIXTURES))
def test_upgrade_reaches_current_version_and_keeps_values(version):
    original = json.loads(json.dumps(VERSION_FIXTURES[version]))
    tree, stored = upgrade_settings(original)
    assert stored == version
    assert tree["config_version"] == CONFIG_VERSION
    assert isinstance(tree["shortcuts"], dict)
    _assert_user_values_kept(tree)
    # The input is never modified.
    assert original == VERSION_FIXTURES[version]


def test_upgrade_keeps_existing_shortcuts():
    tree, _ = upgrade_settings(VERSION_FIXTURES[3])
    assert tree["shortcuts"] == {"quit": ["<Ctrl>q"]}


def test_current_tree_is_returned_as_is():
    tree = {"config_version": CONFIG_VERSION}
    assert upgrade_settings(tree)[0] is tree


def test_string_version_is_accepted():
    tree, stored = upgrade_settings({"config_version": "2"})
    assert stored == 2
    assert tree["config_version"] == CONFIG_VERSION


def test_future_version_is_refused():
    with pytest.raises(SettingsVersionError) as excinfo:
        upgrade_settings({"config_version": CONFIG_VERSION + 1})
    assert excinfo.value.newer is True
    assert excinfo.value.stored_version == CONFIG_VERSION + 1


def test_version_without_upgrade_path_is_refused():
    with pytest.raises(SettingsVersionError) as excinfo:
        upgrade_settings({"config_version": -1})
    assert excinfo.value.newer is False


@pytest.mark.parametrize("bad", ["abc", "3.5", True, 2.0, [], {}])
def test_malformed_version_raises(bad):
    with pytest.raises(SettingsFileError):
        upgrade_settings({"config_version": bad})


def test_failing_step_raises_migration_error(monkeypatch):
    def _broken(_tree):
        raise KeyError("boom")

    monkeypatch.setitem(versioning.MIGRATIONS, 2, _broken)
    with pytest.raises(SettingsMigrationError):
        upgrade_settings({"config_version": 2})


# -- migrating the file -------------------------------------------------------


@pytest.mark.parametrize("version", [0, 1, 2])
def test_older_file_is_upgraded_with_exact_backup(tmp_path, version):
    path = tmp_path / "config.json"
    original = _write(path, VERSION_FIXTURES[version])

    assert migrate_settings_file(path) is True

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["config_version"] == CONFIG_VERSION
    _assert_user_values_kept(on_disk)
    backups = _backups(tmp_path)
    assert [b.name.split("-")[0] for b in backups] == [f"config.json.v{version}"]
    assert backups[0].read_bytes() == original


def test_current_file_is_not_touched(tmp_path):
    path = tmp_path / "config.json"
    original = _write(path, VERSION_FIXTURES[3])
    assert migrate_settings_file(path) is False
    assert path.read_bytes() == original
    assert _backups(tmp_path) == []


def test_missing_file_is_not_created(tmp_path):
    path = tmp_path / "config.json"
    assert migrate_settings_file(path) is False
    assert not path.exists()


def test_repeated_migration_is_idempotent(tmp_path):
    path = tmp_path / "config.json"
    _write(path, VERSION_FIXTURES[1])
    assert migrate_settings_file(path) is True
    upgraded = path.read_bytes()
    for _ in range(3):
        assert migrate_settings_file(path) is False
        load_settings_strict(path)
    assert path.read_bytes() == upgraded
    assert len(_backups(tmp_path)) == 1


def test_concurrent_migrations_upgrade_once(tmp_path):
    path = tmp_path / "config.json"
    _write(path, VERSION_FIXTURES[2])
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(migrate_settings_file(path)))
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [False, False, False, True]
    assert len(_backups(tmp_path)) == 1


def test_later_backup_never_overwrites_an_earlier_one(tmp_path, monkeypatch):
    monkeypatch.setattr(store.time, "strftime", lambda _fmt: "20261016T120000")
    path = tmp_path / "config.json"
    first = _write(path, VERSION_FIXTURES[1])
    migrate_settings_file(path)
    second = _write(path, {**VERSION_FIXTURES[1], "ui": {"language": "fr"}})
    migrate_settings_file(path)

    backups = _backups(tmp_path)
    assert [b.name for b in backups] == [
        "config.json.v1-20261016T120000-1.bak",
        "config.json.v1-20261016T120000.bak",
    ]
    assert backups[1].read_bytes() == first
    assert backups[0].read_bytes() == second


def test_backup_failure_writes_nothing(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    original = _write(path, VERSION_FIXTURES[2])

    def _no_backup(*_args, **_kwargs):
        raise PermissionError("read-only directory")

    monkeypatch.setattr(store, "_write_backup", _no_backup)
    with pytest.raises(SettingsMigrationError):
        migrate_settings_file(path)
    with pytest.raises(SettingsMigrationError):
        load_settings_strict(path)
    with pytest.raises(SettingsMigrationError):
        save_settings(path, {"config_version": CONFIG_VERSION})
    assert path.read_bytes() == original
    assert _backups(tmp_path) == []


def test_failing_step_writes_nothing(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    original = _write(path, VERSION_FIXTURES[2])

    def _broken(_tree):
        raise ValueError("bad tree")

    monkeypatch.setitem(versioning.MIGRATIONS, 2, _broken)
    with pytest.raises(SettingsMigrationError):
        migrate_settings_file(path)
    assert path.read_bytes() == original
    assert _backups(tmp_path) == []


def test_failing_write_after_backup_keeps_original(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    original = _write(path, VERSION_FIXTURES[2])

    def _no_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(store, "_atomic_write", _no_write)
    with pytest.raises(OSError):
        migrate_settings_file(path)
    assert path.read_bytes() == original
    # The backup made before the failed write is harmless and kept.
    assert [b.read_bytes() for b in _backups(tmp_path)] == [original]


# -- files this build must not touch ------------------------------------------


UNUSABLE = {
    "future": json.dumps({"config_version": CONFIG_VERSION + 1, **USER_VALUES}),
    "unknown older": json.dumps({"config_version": -1, **USER_VALUES}),
    "invalid json": '{"config_version": 3, "ui": ',
    "blank": "  \n",
    "not an object": "[1, 2, 3]",
    "bad version": json.dumps({"config_version": "three"}),
}


@pytest.fixture(params=sorted(UNUSABLE))
def unusable_file(request, tmp_path):
    path = tmp_path / "config.json"
    path.write_text(UNUSABLE[request.param], encoding="utf-8")
    return path, path.read_bytes()


def test_unusable_file_is_reported_and_left_unchanged(unusable_file):
    path, original = unusable_file
    with pytest.raises(SettingsFileError):
        migrate_settings_file(path)
    with pytest.raises(SettingsFileError):
        load_settings_strict(path)
    with pytest.raises(SettingsFileError):
        save_settings(path, {"config_version": CONFIG_VERSION, "ui": {"language": "en"}})
    assert path.read_bytes() == original
    assert _backups(path.parent) == []


def test_tolerant_loader_uses_defaults_without_touching_unusable_file(unusable_file):
    path, original = unusable_file
    config, migrated = load_settings(path)
    assert migrated is True
    assert config["config_version"] == CONFIG_VERSION
    assert path.read_bytes() == original


# -- writers --------------------------------------------------------------------


def test_save_writes_current_version_even_for_an_older_payload(tmp_path):
    path = tmp_path / "config.json"
    save_settings(path, {"config_version": 2, "ui": {"language": "de"}})
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["config_version"] == CONFIG_VERSION
    assert on_disk["shortcuts"] == {}


def test_save_over_older_file_backs_it_up_first(tmp_path):
    path = tmp_path / "config.json"
    original = _write(path, VERSION_FIXTURES[1])
    config, migrated = load_settings_strict(path)
    assert migrated is True
    config["ui"]["language"] = "fr"
    save_settings(path, config)
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["ui"]["language"] == "fr"
    assert on_disk["terminal"] == USER_VALUES["terminal"]
    assert [b.read_bytes() for b in _backups(tmp_path)] == [original]


def test_plugin_settings_write_refuses_unusable_file(tmp_path):
    from sshpilot.daemon.plugin_settings import PluginSettingsService

    path = tmp_path / "config.json"
    path.write_text(UNUSABLE["future"], encoding="utf-8")
    original = path.read_bytes()
    service = PluginSettingsService(path)
    with pytest.raises(SettingsFileError):
        service.set("demo", "enabled", True)
    assert path.read_bytes() == original


def test_daemon_startup_upgrades_config_file(tmp_path, monkeypatch):
    from sshpilot.daemon import cli

    path = tmp_path / "config.json"
    original = _write(path, VERSION_FIXTURES[2])
    monkeypatch.setattr(cli, "get_config_dir", lambda: tmp_path)
    cli._upgrade_config_file()
    assert json.loads(path.read_text(encoding="utf-8"))["config_version"] == CONFIG_VERSION
    assert [b.read_bytes() for b in _backups(tmp_path)] == [original]


def test_daemon_startup_leaves_unusable_config_and_continues(unusable_file, monkeypatch):
    from sshpilot.daemon import cli

    path, original = unusable_file
    monkeypatch.setattr(cli, "get_config_dir", lambda: path.parent)
    cli._upgrade_config_file()  # must not raise
    assert path.read_bytes() == original
