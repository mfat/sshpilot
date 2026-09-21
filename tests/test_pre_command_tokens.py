"""``%h``/``%p``/``%u`` expansion in a pre-connection command.

The same three tokens OpenSSH uses, so the vocabulary is one a user of this
app already knows. Without them a port knock has to hardcode the host it is
knocking for, and rots silently the first time the connection is re-pointed
or renamed — which is the failure this exists to prevent.

Expansion happens where the connection's values are (the application service,
and the Test RPC from the editor's fields), never in the runner, which knows
nothing about connections.
"""

from __future__ import annotations

import shlex

import pytest

from sshpilot.api.models.pre_command import expand_pre_command_tokens as expand


def test_the_three_tokens_expand():
    result = expand(
        "knock %h -p %p -u %u", hostname="example.com", port=2222, username="alice"
    )

    assert result == "knock example.com -p 2222 -u alice"


def test_a_value_with_a_space_stays_one_argument():
    """The command is handed to a shell; a hostname must not become argv."""

    result = expand("knock %h", hostname="two words")

    assert result == "knock 'two words'"


@pytest.mark.parametrize(
    "value", ["a'b", 'a"b', "a;rm -rf /", "a$(id)", "a`id`", "a b|c", "a\\b"]
)
def test_shell_metacharacters_in_a_value_stay_one_argument(value):
    """Parsed back by the same rules a shell uses, the value is intact.

    Checking for the raw value in the output would be wrong: correctly
    quoting ``a'b`` produces ``'a'"'"'b'``, which does not contain ``a'b``.
    What matters is that a shell reading it gets one argument, unchanged.
    """

    result = expand("knock %h", hostname=value)

    assert shlex.split(result) == ["knock", value]


def test_a_doubled_percent_is_a_literal_percent():
    assert expand("echo 100%% done", hostname="h") == "echo 100% done"


def test_an_unknown_token_is_left_alone():
    """``date +%H`` and ``printf %s`` are ordinary things to write here."""

    command = "date +%H:%M and printf %s"

    assert expand(command, hostname="h") == command


def test_a_command_with_no_percent_is_returned_unchanged():
    command = "knock example.com 7000 8000"

    assert expand(command, hostname="other.example") is command


def test_a_trailing_percent_is_left_alone():
    assert expand("echo 50%", hostname="h") == "echo 50%"


def test_missing_values_expand_to_nothing_rather_than_the_token():
    """A half-configured connection should not knock a host called "%h"."""

    assert expand("knock %h", hostname="") == "knock ''"


def test_a_non_string_command_is_not_expanded():
    assert expand(None, hostname="h") == ""
