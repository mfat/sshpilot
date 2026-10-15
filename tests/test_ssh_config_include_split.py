"""Include arguments are split the way OpenSSH splits them (Windows paths)."""

from __future__ import annotations

import pytest

from sshpilot import ssh_config_document as doc

BS = "\\"


@pytest.mark.parametrize(
    "text, expected",
    [
        # Single backslashes are kept: a Windows path survives.
        (f"C:{BS}Users{BS}me{BS}.ssh{BS}conf.d{BS}*", [f"C:{BS}Users{BS}me{BS}.ssh{BS}conf.d{BS}*"]),
        (f'"C:{BS}Program Files{BS}x{BS}cfg" other', [f"C:{BS}Program Files{BS}x{BS}cfg", "other"]),
        # A backslash escapes a space, a quote or another backslash.
        (f"a{BS} b c", ["a b", "c"]),
        (f"C:{BS}{BS}double", [f"C:{BS}double"]),
        (f"'single quoted' \"x{BS}\"y\"", ["single quoted", 'x"y']),
        ("  spaced\targs  ", ["spaced", "args"]),
    ],
)
def test_openssh_argv_split(text, expected):
    assert doc._openssh_argv_split(text) == expected


def test_unterminated_quote_is_an_error():
    with pytest.raises(ValueError):
        doc._openssh_argv_split('"open')


def test_windows_uses_openssh_rules_and_posix_keeps_shlex(monkeypatch):
    path = f"C:{BS}a{BS}b"
    monkeypatch.setattr(doc.sys, "platform", "win32")
    assert doc.split_include_args(path) == [path]
    monkeypatch.setattr(doc.sys, "platform", "linux")
    assert doc.split_include_args(path) == ["C:ab"]  # shlex, unchanged on POSIX
