"""Wire round-trip and strictness for ``connections.list_config_conflicts``."""

from __future__ import annotations

import pytest

from sshpilot.api.models.connections import ConfigConflict, EarlierDirective
from sshpilot.api.transport.codec import (
    config_conflicts_from_wire,
    config_conflicts_to_wire,
)


def _conflict(**overrides):
    fields = dict(
        connection_id="web",
        host="web",
        key="user",
        own_value="deploy",
        earlier=EarlierDirective(
            key="user", value="admin", block="Host *",
            file="/home/me/.ssh/config", line=3,
        ),
    )
    fields.update(overrides)
    return ConfigConflict(**fields)


def test_config_conflicts_round_trip():
    conflicts = (_conflict(), _conflict(connection_id="db", host="db"))
    assert config_conflicts_from_wire(config_conflicts_to_wire(conflicts)) == conflicts
    assert config_conflicts_from_wire(config_conflicts_to_wire(())) == ()


def test_config_conflicts_reject_unknown_and_missing_fields():
    wire = config_conflicts_to_wire((_conflict(),))
    wire["conflicts"][0]["extra"] = 1
    with pytest.raises(ValueError):
        config_conflicts_from_wire(wire)
    wire = config_conflicts_to_wire((_conflict(),))
    del wire["conflicts"][0]["earlier"]["line"]
    with pytest.raises(ValueError):
        config_conflicts_from_wire(wire)


def test_config_conflict_key_must_match_its_earlier_directive():
    with pytest.raises(ValueError):
        _conflict(key="port")
    with pytest.raises(ValueError):
        _conflict(own_value="")
