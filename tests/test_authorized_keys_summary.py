"""Characterization tests for authorized_keys_window's pure helpers.

_short and _summary are GTK-free; pin their current output before any refactor.
The parser itself (authorized_keys_parser) has its own tests; here we build real
entries through it and assert the summary chips the window derives from them.
"""

import pytest

akw = pytest.importorskip("sshpilot.authorized_keys_window")
parser = pytest.importorskip("sshpilot.authorized_keys_parser")

_KEY = ("ssh-ed25519 "
        "AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl "
        "bob@host")


def _entry(prefix=""):
    line = (prefix + " " + _KEY).strip() + "\n"
    return next(e for e in parser.parse_file(line) if getattr(e, "keytype", None))


def test_short_truncates_with_ellipsis():
    assert akw._short("short", 28) == "short"
    assert akw._short("x" * 30, 10) == "x" * 9 + "…"
    assert len(akw._short("x" * 30, 10)) == 10


def test_summary_no_options():
    assert akw._summary(_entry("")) == "no options"


def test_summary_lists_relevant_chips():
    entry = _entry('command="/bin/true",restrict,cert-authority,'
                   'from="10.0.0.0/8",expiry-time="20301231",permitopen="h:22"')
    s = akw._summary(entry)
    assert "forced cmd" in s
    assert "restrict" in s
    assert "CA" in s
    assert "from=" in s
    assert "expiry" in s
    assert "permitopen" in s
    assert " · " in s  # chips joined with middot


def test_summary_from_value_is_shortened():
    long_from = "10.0.0.0/8,192.168.0.0/16,172.16.0.0/12,!10.0.0.5,!10.0.0.6"
    entry = _entry(f'from="{long_from}"')
    s = akw._summary(entry)
    assert s.startswith("from=")
    assert "…" in s  # long from= value truncated by _short(..., 24)
