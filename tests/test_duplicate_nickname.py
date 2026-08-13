"""Regression test: duplicated-connection nicknames must be valid.

The duplicate generator used to produce "Name (Copy)", but the app's own
validator rejects whitespace and parens make an invalid ssh Host alias (see
#953). The generated nickname must be safe to use verbatim as a Host alias:
whitespace-free, parenthesis-free, and composed of literal ssh Host-alias
characters. The canonical implementation is the pure, GTK-free
``generate_duplicate_nickname`` helper.
"""

import re

import pytest

from sshpilot.core.connections.models import generate_duplicate_nickname


def _generate(existing_names, base):
    return generate_duplicate_nickname(base, set(existing_names))


@pytest.mark.parametrize(
    "existing,base,expected",
    [
        (set(), "myserver", "myserver-Copy"),
        ({"myserver-copy"}, "myserver", "myserver-Copy-2"),          # first copy taken -> -2
        ({"myserver-copy", "myserver-copy-2"}, "myserver", "myserver-Copy-3"),  # -> -3
        ({"myserver"}, "myserver (Copy)", "myserver-Copy"),          # legacy-format base
        ({"myserver"}, "myserver-Copy", "myserver-Copy"),            # new-format base
        (set(), "", "Connection-Copy"),                              # empty base -> "Connection"
        ({"myserver-Copy"}, "myserver-Copy", "myserver-Copy-2"),     # no stacked suffixes
    ],
)
def test_duplicate_nickname_is_valid(existing, base, expected):
    name = _generate(existing, base)

    assert name == expected
    assert not re.search(r"\s", name), f"nickname has whitespace: {name!r}"
    assert "(" not in name and ")" not in name, f"nickname has parens: {name!r}"
    assert name.lower() not in {n.lower() for n in existing}, f"nickname not unique: {name!r}"
    # Safe literal ssh Host-alias characters.
    assert re.fullmatch(r"[A-Za-z0-9_@.-]+", name), f"unsafe alias: {name!r}"


def test_re_duplicating_does_not_stack_suffixes():
    # Duplicating an already-duplicated connection strips the prior suffix.
    name = _generate({"myserver-Copy"}, "myserver-Copy")
    assert name == "myserver-Copy-2"
