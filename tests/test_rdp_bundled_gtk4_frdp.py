"""The deb/rpm/Arch packages bundle gtk4-frdp privately (meson option rdp_tab).

They only recommend FreeRDP, so the bundled library may be present but unable
to load. That must count as "no tab" -- RDP then opens in FreeRDP's window --
rather than a tab that fails when it opens.
"""

import sys
import types

import pytest

rdp_tab = pytest.importorskip("sshpilot.rdp_tab")


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    monkeypatch.setattr(rdp_tab, "_available", None)


def _build_config(monkeypatch, libdir):
    module = types.ModuleType("sshpilot.build_config")
    module.RDP_TAB_LIBDIR = libdir
    monkeypatch.setitem(sys.modules, "sshpilot.build_config", module)


def test_no_meson_install_means_no_bundled_copy(monkeypatch):
    monkeypatch.setitem(sys.modules, "sshpilot.build_config", None)
    assert rdp_tab._bundled_libdir() == ""


def test_a_build_without_the_rdp_tab_has_no_bundled_copy(monkeypatch):
    _build_config(monkeypatch, "")
    assert rdp_tab._bundled_libdir() == ""


def test_a_missing_bundled_directory_is_ignored(monkeypatch, tmp_path):
    _build_config(monkeypatch, str(tmp_path / "absent"))
    assert rdp_tab._bundled_libdir() == ""


def test_a_bundled_copy_that_cannot_load_means_no_tab(monkeypatch, tmp_path):
    """FreeRDP not installed: the library fails to load, GI is never asked."""
    _build_config(monkeypatch, str(tmp_path))
    required = []
    monkeypatch.setattr(rdp_tab.gi, "require_version",
                        lambda *args: required.append(args))
    # tmp_path holds no libgtk4-frdp-4.0.so.0: ctypes raises OSError, exactly
    # as it does for a present library whose FreeRDP is missing.
    assert rdp_tab.embedded_rdp_available() is False
    assert required == []
