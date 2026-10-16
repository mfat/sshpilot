"""The settings-file version policy, in one place (GTK-free).

Every reader and writer of ``config.json`` applies the same rules:

* current version -> used as is;
* a known older version -> upgraded by chaining the steps in
  :data:`MIGRATIONS`, one version at a time;
* an older version with no step (negative, or a gap) -> left untouched and
  reported as :class:`SettingsVersionError`;
* a newer version -> left untouched and reported as
  :class:`SettingsVersionError`: an older build must not backfill or rewrite
  a tree it does not understand;
* a step that fails -> :class:`SettingsMigrationError`, file untouched.

These functions are pure: they never touch the disk. Persisting an upgrade
(with a backup of the original bytes) is ``store.migrate_settings_file``.

Bumping ``CONFIG_VERSION`` therefore needs a step here for the old version;
``tests/core/test_settings_versioning.py`` fails until there is one.
"""
from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Callable, Dict, Tuple

from .defaults import CONFIG_VERSION


class SettingsFileError(RuntimeError):
    """Raised when a settings file cannot be read as a valid current-version tree.

    This is the core-level signal for authoritative readers; the daemon maps it
    to a stable sanitized API error (no paths, no raw JSON, no exception text).
    """


class SettingsVersionError(SettingsFileError):
    """The stored version is newer than this build, or has no upgrade path."""

    def __init__(self, message: str, *, stored_version: int, newer: bool) -> None:
        super().__init__(message)
        self.stored_version = stored_version
        self.newer = newer


class SettingsMigrationError(SettingsFileError):
    """An upgrade step failed, or its result could not be persisted safely."""


def _v0_to_v1(tree: Dict[str, Any]) -> None:
    # Version 0 is a file written before config_version existed. Nothing in
    # the tree changed when the field was introduced.
    del tree


def _v1_to_v2(tree: Dict[str, Any]) -> None:
    # Version 2 introduced version handling itself; the keys were unchanged.
    del tree


def _v2_to_v3(tree: Dict[str, Any]) -> None:
    # Version 3 added user shortcut overrides.
    if not isinstance(tree.get("shortcuts"), dict):
        tree["shortcuts"] = {}


#: ``MIGRATIONS[n]`` upgrades a version-``n`` tree in place to version ``n + 1``.
MIGRATIONS: Dict[int, Callable[[Dict[str, Any]], None]] = {
    0: _v0_to_v1,
    1: _v1_to_v2,
    2: _v2_to_v3,
}


def parse_stored_version(data: Dict[str, Any]) -> int:
    """Return the stored ``config_version`` as an integer.

    A missing ``config_version`` is version 0, a file from before versioning.
    Any other non-integer type or value — bools, floats, non-numeric strings,
    lists, dicts — is malformed and raises.
    """
    value = data.get("config_version")
    if value is None:
        return 0
    if isinstance(value, bool):
        raise SettingsFileError("config_version must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value):
        return int(value)
    raise SettingsFileError("config_version must be an integer")


def upgrade_settings(data: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
    """Return ``(tree, stored_version)`` with *tree* at ``CONFIG_VERSION``.

    A current tree is returned as is (same object). An older one is upgraded
    on a deep copy, so *data* is never modified. Raises
    :class:`SettingsVersionError` or :class:`SettingsMigrationError` when the
    tree cannot be used, and :class:`SettingsFileError` for a malformed
    version field.
    """
    stored = parse_stored_version(data)
    if stored > CONFIG_VERSION:
        raise SettingsVersionError(
            "The settings file was written by a newer version of SSH Pilot",
            stored_version=stored,
            newer=True,
        )
    if stored == CONFIG_VERSION:
        return data, stored
    tree = deepcopy(data)
    version = stored
    while version < CONFIG_VERSION:
        step = MIGRATIONS.get(version)
        if step is None:
            raise SettingsVersionError(
                "The settings file version has no upgrade path",
                stored_version=stored,
                newer=False,
            )
        try:
            step(tree)
        except Exception as exc:
            raise SettingsMigrationError(
                "The settings file could not be upgraded"
            ) from exc
        version += 1
        tree["config_version"] = version
    return tree, stored
