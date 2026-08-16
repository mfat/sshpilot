"""Phase 14 GUI package fixtures (real GTK only)."""

from __future__ import annotations

import os
import time

import pytest

from tests._gui_harness import requires_gui


@pytest.fixture
def phase14_harness(tmp_path, monkeypatch):
    """Boot isolated Phase 14 production GTK harness; tear down after test."""
    requires_gui()
    # Prefer software rendering to reduce VTE/GTK abort flakiness on CI VMs.
    monkeypatch.setenv("GSK_RENDERER", os.environ.get("GSK_RENDERER", "cairo"))
    monkeypatch.setenv("GDK_BACKEND", os.environ.get("GDK_BACKEND", "x11"))
    monkeypatch.setenv("LIBGL_ALWAYS_SOFTWARE", "1")
    monkeypatch.delenv("G_DEBUG", raising=False)
    # Force serial-friendly isolation (one HOME / daemon / OpenSSH per test).
    monkeypatch.setenv("SSHPILOT_GUI_TESTS", "1")

    from tests.gui._phase14_harness import Phase14Harness, make_isolated_home

    home = make_isolated_home(tmp_path)
    harness = Phase14Harness(home=home)
    try:
        harness.boot()
        yield harness
    finally:
        leftover_meta = None
        try:
            if harness.openssh is not None:
                leftover_meta = {
                    "runtime": harness.openssh.runtime,
                    "container_id": harness.openssh.container_id,
                    "container_name": harness.openssh.container_name,
                }
        except Exception:
            leftover_meta = None
        try:
            harness.close()
        except Exception:
            pass
        if leftover_meta:
            try:
                from tests.fixtures.temporary_openssh import destroy_temporary_openssh_meta

                destroy_temporary_openssh_meta(leftover_meta)
            except Exception:
                pass
        # Brief settle so the next test does not inherit lingering PTYs/X11 grabs.
        time.sleep(0.15)
