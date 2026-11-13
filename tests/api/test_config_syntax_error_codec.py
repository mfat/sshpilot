"""Wire round-trip and strictness for ``connections.list_config_syntax_errors``."""

from __future__ import annotations

import pytest

from sshpilot.api.models.connections import ConfigSyntaxError
from sshpilot.api.transport.codec import (
    config_syntax_errors_from_wire,
    config_syntax_errors_to_wire,
)


def _error(**overrides):
    fields = dict(
        file="/home/me/.ssh/config",
        line=5,
        diagnostic="Bad configuration option: *",
    )
    fields.update(overrides)
    return ConfigSyntaxError(**fields)


def test_config_syntax_errors_round_trip():
    errors = (_error(), _error(line=9, diagnostic=""))
    assert config_syntax_errors_from_wire(config_syntax_errors_to_wire(errors)) == errors
    assert config_syntax_errors_from_wire(config_syntax_errors_to_wire(())) == ()


def test_config_syntax_errors_reject_unknown_and_missing_fields():
    wire = config_syntax_errors_to_wire((_error(),))
    wire["errors"][0]["extra"] = 1
    with pytest.raises(ValueError):
        config_syntax_errors_from_wire(wire)
    wire = config_syntax_errors_to_wire((_error(),))
    del wire["errors"][0]["line"]
    with pytest.raises(ValueError):
        config_syntax_errors_from_wire(wire)


def test_config_syntax_error_needs_a_file_and_a_real_line():
    with pytest.raises(ValueError):
        _error(file="")
    with pytest.raises(ValueError):
        _error(line=0)
    with pytest.raises(ValueError):
        _error(line=True)
