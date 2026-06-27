"""Unit tests for crash-log rotation (main._rotate_previous_crash_log).

Scope: this verifies the *pure file-rotation* contract only — that a non-empty
crash.log from a previous run is moved aside to crash.log.previous and its path
returned, and that the no-op / failure cases behave. It deliberately does NOT
exercise faulthandler arming or RLIMIT_CORE tweaks (those live in
_enable_crash_diagnostics and would collide with pytest's own faulthandler).
"""

import os

from sshpilot.main import _rotate_previous_crash_log


def _write(path, content):
    with open(path, "w") as fh:
        fh.write(content)


def test_nonempty_crash_log_is_rotated(tmp_path):
    log = tmp_path / "crash.log"
    _write(log, "boom: previous run crashed\n")

    result = _rotate_previous_crash_log(str(tmp_path))

    preserved = tmp_path / "crash.log.previous"
    assert result == str(preserved)
    assert preserved.exists()
    assert preserved.read_text() == "boom: previous run crashed\n"
    # The live crash.log is moved away so this run starts clean.
    assert not log.exists()


def test_absent_crash_log_is_noop(tmp_path):
    result = _rotate_previous_crash_log(str(tmp_path))

    assert result is None
    assert not (tmp_path / "crash.log.previous").exists()


def test_empty_crash_log_is_noop(tmp_path):
    log = tmp_path / "crash.log"
    _write(log, "")

    result = _rotate_previous_crash_log(str(tmp_path))

    assert result is None
    # A zero-byte log is not a real crash report, so it is left untouched.
    assert log.exists()
    assert not (tmp_path / "crash.log.previous").exists()


def test_previous_report_is_overwritten(tmp_path):
    # A stale crash.log.previous must be replaced by the most recent crash.
    _write(tmp_path / "crash.log.previous", "older crash\n")
    _write(tmp_path / "crash.log", "newer crash\n")

    result = _rotate_previous_crash_log(str(tmp_path))

    preserved = tmp_path / "crash.log.previous"
    assert result == str(preserved)
    assert preserved.read_text() == "newer crash\n"


def test_replace_failure_is_swallowed(tmp_path, monkeypatch):
    _write(tmp_path / "crash.log", "boom\n")

    def _boom(*_args, **_kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(os, "replace", _boom)

    # Best-effort contract: a rotation failure returns None instead of raising,
    # so startup is never blocked by an un-rotatable log.
    assert _rotate_previous_crash_log(str(tmp_path)) is None
