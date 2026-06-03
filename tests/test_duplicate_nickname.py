"""Regression test: duplicated-connection nicknames must be valid.

The duplicate generator used to produce "Name (Copy)", but the app's own
validator rejects whitespace and parens make an invalid ssh Host alias (see
#953). The generated nickname must pass SSHConnectionValidator and be safe to
use verbatim as a Host alias.
"""

import re
from types import SimpleNamespace

import pytest

# Importing the window module needs GTK typelibs (incl. GdkPixbuf); skip cleanly
# where they aren't available (e.g. headless CI without the full stack).
window = pytest.importorskip("sshpilot.window")
from sshpilot.connection_dialog import SSHConnectionValidator


def _generate(existing_names, base):
    win = SimpleNamespace(
        connection_manager=SimpleNamespace(
            get_connections=lambda: [SimpleNamespace(nickname=n) for n in existing_names]
        )
    )
    return window.MainWindow._generate_duplicate_nickname(win, base)


@pytest.mark.parametrize(
    "existing,base",
    [
        (set(), "myserver"),
        ({"myserver-copy"}, "myserver"),                      # first copy taken -> -2
        ({"myserver-copy", "myserver-copy-2"}, "myserver"),   # -> -3
        ({"myserver"}, "myserver (Copy)"),                    # legacy-format base
        ({"myserver"}, "myserver-Copy"),                      # new-format base
        (set(), ""),                                          # empty base -> "Connection"
    ],
)
def test_duplicate_nickname_is_valid(existing, base):
    validator = SSHConnectionValidator()
    validator.set_existing_names(existing)

    name = _generate(existing, base)

    assert not re.search(r"\s", name), f"nickname has whitespace: {name!r}"
    assert "(" not in name and ")" not in name, f"nickname has parens: {name!r}"
    assert name.lower() not in {n.lower() for n in existing}, f"nickname not unique: {name!r}"
    assert validator.validate_connection_name(name).is_valid, f"validator rejected: {name!r}"


def test_re_duplicating_does_not_stack_suffixes():
    # Duplicating an already-duplicated connection strips the prior suffix.
    name = _generate({"myserver-Copy"}, "myserver-Copy")
    assert name == "myserver-Copy-2"
