"""Closing a transient window hands activation to its parent (GH #1339)."""

import subprocess
import sys

import pytest

from sshpilot.platform.windows import transient_close
from sshpilot.platform.windows.transient_close import (
    _on_close_request,
    hand_activation_to_parent,
    install_transient_close_handoff,
)


class FakeWindow:
    def __init__(self, parent=None, active=True, mapped=True):
        self.parent = parent
        self.active = active
        self.mapped = mapped
        self.presented = 0

    def get_transient_for(self):
        return self.parent

    def is_active(self):
        return self.active

    def get_mapped(self):
        return self.mapped

    def present(self):
        self.presented += 1


def test_active_dialog_presents_its_parent():
    parent = FakeWindow()
    hand_activation_to_parent(FakeWindow(parent=parent))
    assert parent.presented == 1


def test_inactive_dialog_leaves_activation_alone():
    parent = FakeWindow()
    hand_activation_to_parent(FakeWindow(parent=parent, active=False))
    assert parent.presented == 0


def test_hidden_parent_is_not_shown():
    parent = FakeWindow(mapped=False)
    hand_activation_to_parent(FakeWindow(parent=parent))
    assert parent.presented == 0


def test_window_without_parent_is_a_noop():
    hand_activation_to_parent(FakeWindow())


def test_hook_stays_installed_when_the_parent_fails():
    class Broken(FakeWindow):
        def present(self):
            raise RuntimeError("no surface")

    assert _on_close_request(FakeWindow(parent=Broken())) is True


def test_hook_does_not_veto_or_uninstall_on_a_normal_close():
    parent = FakeWindow()
    assert _on_close_request(FakeWindow(parent=parent)) is True
    assert parent.presented == 1


def test_install_is_windows_only(monkeypatch):
    monkeypatch.setattr(transient_close.sys, "platform", "linux")
    assert install_transient_close_handoff() is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
def test_install_attaches_in_a_fresh_process_once_a_window_exists():
    """Mirrors startup: nothing has initialised Gtk.Window's class beforehand."""
    pytest.importorskip("gi")
    code = (
        "import gi\n"
        "gi.require_version('Gtk', '4.0')\n"
        "from gi.repository import Gtk\n"
        "from sshpilot.platform.windows.transient_close import (\n"
        "    install_transient_close_handoff)\n"
        "assert Gtk.init_check()\n"
        "Gtk.Window().destroy()\n"
        "assert install_transient_close_handoff() is True\n"
        "assert install_transient_close_handoff() is True\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 0, done.stderr
