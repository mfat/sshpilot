"""Permanent regression fixtures replayed against the fuzzing harness.

Each fixture under ``tests/fixtures/sidecar_regressions/*.json`` names a
sequence of operations drawn from the same vocabulary as
``SidecarIdentityMachine``'s rules (``tests/sidecar/state_machine.py``) and is
replayed by calling those rule methods directly -- so a regression exercises
exactly the same code path as the property-based fuzzer, and a minimized
Hypothesis failure can be promoted here by transcribing its operation
sequence into a new fixture file.

Supported ``op`` values (each ``ref`` is a fixture-local name the fixture
invents for a connection; it never has to match sshPilot's internal UUID or
alias):

* ``managed_rename`` -- ``{"ref", "suffix", "use_raw_editor"?}``
* ``managed_change_destination`` -- ``{"ref", "hostname", "username"?}``
* ``managed_rename_and_change_destination`` -- ``{"ref", "suffix", "hostname", "username"?}``
* ``managed_delete`` -- ``{"ref"}``
* ``managed_duplicate`` -- ``{"ref", "as"}`` (registers the duplicate under ``as``)
* ``external_rename_single`` -- ``{"ref", "suffix", "use_raw_editor"?}``
* ``external_change_port`` -- ``{"ref", "port", "use_raw_editor"?}``
* ``external_change_identity_files`` -- ``{"ref", "identity_files", "use_raw_editor"?}``
* ``external_collide_and_rename`` -- ``{"a", "b", "use_raw_editor"?}``
* ``add_connection_reusing_deleted_alias`` -- ``{"as", "hostname", "username"?}``
* ``toggle_mode`` -- ``{}``
* ``restart`` -- ``{}``
* ``explicit_reload_idempotent`` -- ``{}``
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Any, Dict

import pytest

from .state_machine import SidecarIdentityMachine

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "sidecar_regressions"


def _replay(machine: SidecarIdentityMachine, fixture: Dict[str, Any]) -> None:
    # Rule bodies call hypothesis.assume() to skip preconditions that don't
    # apply under fuzzing; a curated fixture never actually trips one, but
    # calling a rule outside Hypothesis's own execution machinery makes
    # assume() emit a deprecation notice we don't need repeated per call.
    warnings.filterwarnings(
        "ignore",
        category=Warning,
        module="hypothesis.control",
    )
    refs: Dict[str, str] = {}

    def ref(name: str) -> str:
        """Resolve a fixture-local name to the ACTIVE root's connection.

        The two SSH configuration roots hold independent connections, so a
        fixture that toggles modes and then edits "prod" means the current
        root's own "prod" -- not the one it left behind.
        """
        return machine.resolve_ref(refs[name])

    for seed in fixture.get("initial_connections", []):
        logical_id = machine.add_connection_external(
            hostname=seed["hostname"],
            port=seed.get("port", 22),
            username=seed.get("username", ""),
            identity_files=tuple(seed.get("identity_files", ())),
            use_raw_editor=False,
        )
        refs[seed["ref"]] = logical_id
        machine.sidecar_stays_healthy()

    for op in fixture.get("operations", []):
        kind = op["op"]
        if kind == "managed_rename":
            machine.managed_rename(ref(op["ref"]), op["suffix"], op.get("use_raw_editor", False))
        elif kind == "managed_change_destination":
            machine.managed_change_destination(
                ref(op["ref"]), op["hostname"], op.get("username", "")
            )
        elif kind == "managed_rename_and_change_destination":
            machine.managed_rename_and_change_destination(
                ref(op["ref"]), op["suffix"], op["hostname"], op.get("username", "")
            )
        elif kind == "managed_delete":
            machine.managed_delete(ref(op["ref"]))
        elif kind == "managed_duplicate":
            refs[op["as"]] = machine.managed_duplicate(ref(op["ref"]))
        elif kind == "external_rename_single":
            machine.external_rename_single(
                ref(op["ref"]), op["suffix"], op.get("use_raw_editor", False)
            )
        elif kind == "external_change_port":
            machine.external_change_port(ref(op["ref"]), op["port"], op.get("use_raw_editor", False))
        elif kind == "external_change_identity_files":
            machine.external_change_identity_files(
                ref(op["ref"]), tuple(op["identity_files"]), op.get("use_raw_editor", False)
            )
        elif kind == "external_collide_and_rename":
            machine.external_collide_and_rename(
                ref(op["a"]), ref(op["b"]), op.get("use_raw_editor", False)
            )
        elif kind == "add_connection_reusing_deleted_alias":
            refs[op["as"]] = machine.add_connection_reusing_deleted_alias(
                op["hostname"], op.get("username", "")
            )
        elif kind == "toggle_mode":
            machine.toggle_mode()
        elif kind == "restart":
            machine.restart()
        elif kind == "explicit_reload_idempotent":
            machine.explicit_reload_idempotent()
        else:
            raise ValueError(f"unsupported regression op: {kind!r}")
        machine.sidecar_stays_healthy()


def _fixture_files():
    if not FIXTURES_DIR.exists():
        return []
    return sorted(FIXTURES_DIR.glob("*.json"))


@pytest.mark.parametrize("fixture_path", _fixture_files(), ids=lambda p: p.stem)
def test_regression_fixture(fixture_path: Path) -> None:
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    machine = SidecarIdentityMachine()
    try:
        _replay(machine, fixture)
    finally:
        machine.teardown()


def test_fixtures_directory_is_not_empty() -> None:
    """Guard against the parametrized test silently collecting zero cases."""

    assert _fixture_files(), f"no regression fixtures found under {FIXTURES_DIR}"
