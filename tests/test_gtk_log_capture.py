"""Regression test: GTK4 *structured* log warnings reach the Python `gtk` logger.

GTK4 emits its own warnings via structured logging (``g_log_structured`` /
``g_log_structured_array``), which bypasses the legacy ``g_log_set_handler``
path. ``_install_gtk_log_capture`` therefore also installs a
``GLib.log_set_writer_func``; this test guards that the structured path is
captured into the ``gtk`` logger (and thus the log files / Export Diagnostics).

Note: ``GLib.log_structured_array`` cannot be called from Python — its
``LogField.value`` is a gpointer PyGObject won't marshal (gnome bug 683599) — so
we emit via the introspectable ``GLib.log_variant``, which drives the *same*
``g_log_structured_array`` writer path internally.
"""

import logging

import pytest

gi = pytest.importorskip("gi")
gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

from sshpilot import main as sshpilot_main  # noqa: E402

# conftest installs a dummy GLib when real gi isn't already imported (e.g. when
# this file is run in isolation on a slim CI image). Real GLib exposes
# MAJOR_VERSION as an int; the dummy returns a placeholder object.
_REAL_GLIB = (
    isinstance(getattr(GLib, "MAJOR_VERSION", None), int)
    and hasattr(GLib, "log_set_writer_func")
    and hasattr(GLib, "log_variant")
)


@pytest.mark.skipif(not _REAL_GLIB, reason="real GLib structured-logging APIs unavailable")
def test_structured_gtk_warning_reaches_gtk_logger():
    # Installs the legacy handler + the structured writer (idempotent).
    sshpilot_main._install_gtk_log_capture(verbose=False)

    # Capture directly off the 'gtk' logger (independent of propagation config).
    captured = []

    class _Capture(logging.Handler):
        def emit(self, record):
            captured.append(record.getMessage())

    gtk_logger = logging.getLogger("gtk")
    handler = _Capture()
    gtk_logger.addHandler(handler)
    prev_level = gtk_logger.level
    gtk_logger.setLevel(logging.DEBUG)
    try:
        fields = GLib.Variant(
            "a{sv}",
            {
                # MESSAGE is the only mandatory field for log_variant.
                "MESSAGE": GLib.Variant("s", "unit-test-structured-warning"),
                "GLIB_DOMAIN": GLib.Variant("s", "SSHPilotTest"),
            },
        )
        # WARNING (not ERROR) so it is never fatal regardless of the env.
        # log_variant drives the same g_log_structured_array writer path GTK4 uses.
        GLib.log_variant("SSHPilotTest", GLib.LogLevelFlags.LEVEL_WARNING, fields)
    finally:
        gtk_logger.removeHandler(handler)
        gtk_logger.setLevel(prev_level)

    assert any(
        "unit-test-structured-warning" in msg for msg in captured
    ), "structured GTK warning was not forwarded to the 'gtk' logger"
