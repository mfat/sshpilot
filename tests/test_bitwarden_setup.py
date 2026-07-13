"""Tests for sshpilot/bitwarden_setup.py (GTK-free helpers)."""

import io
import subprocess
import zipfile

import pytest

from sshpilot import bitwarden_setup as bs


@pytest.fixture(autouse=True)
def _clear_bitwarden_status_cache():
    bs.invalidate_bitwarden_status_cache()
    yield
    bs.invalidate_bitwarden_status_cache()


def test_resolve_terminal_manager_via_parent_window():
    class Mgr:
        pass

    class Main:
        terminal_manager = Mgr()

    class Prefs:
        parent_window = Main()

    assert bs._resolve_terminal_manager(Prefs()) is Main.terminal_manager


def test_resolve_terminal_manager_missing():
    class Win:
        pass

    assert bs._resolve_terminal_manager(Win()) is None


def test_hide_and_restore_preferences_window():
    class PreferencesWindow:
        def __init__(self):
            self.visible = True
            self.shown = False

        def get_visible(self):
            return self.visible

        def hide(self):
            self.visible = False

        def show(self):
            self.visible = True
            self.shown = True

        def present(self):
            self.shown = True

    prefs = PreferencesWindow()
    hidden = bs._hide_preferences_windows(prefs)
    assert prefs in hidden
    assert not prefs.get_visible()
    bs._restore_preferences_windows(hidden)
    assert prefs.get_visible()
    assert prefs.shown


def test_is_bw_installed_uses_resolve_bw_cli(monkeypatch):
    from sshpilot import platform_utils

    monkeypatch.setattr(platform_utils, "resolve_bw_cli", lambda **kw: None)
    assert bs.is_bw_installed() is False
    monkeypatch.setattr(platform_utils, "resolve_bw_cli", lambda **kw: ["/usr/bin/bw"])
    assert bs.is_bw_installed() is True


def test_path_only_ignores_flatpak_desktop(monkeypatch):
    from sshpilot import platform_utils

    monkeypatch.setattr(platform_utils, "resolve_host_binary", lambda _b: None)
    monkeypatch.setattr(platform_utils, "get_managed_bw_cli_path", lambda: "/tmp/no-bw")
    monkeypatch.setattr(platform_utils, "_managed_bw_cli_argv", lambda _p: None)
    monkeypatch.setattr(platform_utils.shutil, "which", lambda name: "/usr/bin/flatpak" if name == "flatpak" else None)
    monkeypatch.setattr(platform_utils, "_flatpak_bitwarden_cli_prefix", lambda _a: ["/usr/bin/flatpak", "run", "--command=bw", "com.bitwarden.desktop"])
    platform_utils.invalidate_bw_cli_cache()
    assert platform_utils.resolve_bw_cli_binding(force_refresh=True) is None


def test_detect_install_plan_none_when_present(monkeypatch):
    monkeypatch.setattr(bs, "is_bw_installed", lambda: True)
    assert bs.detect_install_plan() is None


def test_detect_install_plan_binary_download(monkeypatch):
    from sshpilot import platform_utils

    monkeypatch.setattr(bs, "is_bw_installed", lambda: False)
    monkeypatch.setattr(platform_utils, "get_managed_bw_cli_path", lambda: "/home/u/.local/share/sshpilot/bin/bw")
    monkeypatch.setattr(platform_utils, "is_flatpak", lambda: False)
    plan = bs.detect_install_plan()
    assert plan is not None
    assert plan.automated is True
    assert plan.argv == ()
    assert "official" in plan.description.lower() or "download" in plan.description.lower()
    assert "/home/u/.local/share/sshpilot/bin/bw" in plan.note


def test_latest_cli_release_picks_linux_asset(monkeypatch):
    releases = [
        {"tag_name": "desktop-v1", "assets": []},
        {
            "tag_name": "cli-v2026.6.0",
            "assets": [
                {"name": "bw-linux-2026.6.0.zip"},
                {"name": "bw-linux-arm64-2026.6.0.zip"},
            ],
        },
    ]
    monkeypatch.setattr(bs, "_fetch_url", lambda url: __import__("json").dumps(releases).encode())
    monkeypatch.setattr(bs.platform, "system", lambda: "Linux")
    monkeypatch.setattr(bs.platform, "machine", lambda: "x86_64")
    version, asset = bs._latest_cli_release()
    assert version == "2026.6.0"
    assert asset == "bw-linux-2026.6.0.zip"


def test_download_and_install_bw_binary(monkeypatch, tmp_path):
    from sshpilot import platform_utils

    dest = tmp_path / "bin" / "bw"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("bw", b"#!/bin/sh\necho bw\n")
    zip_bytes = buf.getvalue()

    monkeypatch.setattr(bs, "_latest_cli_release", lambda: ("2026.6.0", "bw-linux-2026.6.0.zip"))
    monkeypatch.setattr(bs, "_fetch_url", lambda url, **kw: zip_bytes)
    monkeypatch.setattr(platform_utils, "get_managed_bw_cli_path", lambda: str(dest))
    monkeypatch.setattr(platform_utils, "is_flatpak", lambda: False)
    monkeypatch.setattr(bs, "is_bw_installed", lambda force_refresh=False: dest.exists())
    platform_utils.invalidate_bw_cli_cache()

    ok, detail = bs.download_and_install_bw_binary()
    assert ok is True
    assert detail == ""
    assert dest.exists()
    assert dest.stat().st_mode & 0o111


def test_probe_bitwarden_status_ready(monkeypatch):
    class FakeBw:
        def needs_login(self):
            return False

        def is_unlocked(self):
            return True

    monkeypatch.setattr(bs, "is_bw_installed", lambda **kw: True)
    bs.invalidate_bitwarden_status_cache()
    status = bs.probe_bitwarden_status(FakeBw())
    assert status.is_ready is True


def test_probe_bitwarden_status_not_installed(monkeypatch):
    monkeypatch.setattr(bs, "is_bw_installed", lambda **kw: False)
    bs.invalidate_bitwarden_status_cache()
    status = bs.probe_bitwarden_status()
    assert status.cli_installed is False
    assert status.is_ready is False


def test_probe_bitwarden_status_skips_bw_status_when_unlocked(monkeypatch):
    class FakeBw:
        def is_unlocked(self):
            return True

        def needs_login(self):
            raise AssertionError("login check should not run when already unlocked")

    monkeypatch.setattr(bs, "is_bw_installed", lambda **kw: True)
    bs.invalidate_bitwarden_status_cache()
    status = bs.probe_bitwarden_status(FakeBw())
    assert status.is_ready is True


def test_probe_bitwarden_status_cached(monkeypatch):
    calls = {"n": 0}

    class FakeBw:
        def is_unlocked(self):
            return False

        def needs_login(self):
            calls["n"] += 1
            return True

    monkeypatch.setattr(bs, "is_bw_installed", lambda **kw: True)
    bs.invalidate_bitwarden_status_cache()
    assert bs.probe_bitwarden_status(FakeBw()).needs_login is True
    assert bs.probe_bitwarden_status(FakeBw()).needs_login is True
    assert calls["n"] == 1


def test_run_install_binary_plan(monkeypatch):
    plan = bs.InstallPlan(
        argv=(),
        description="binary",
        automated=True,
        terminal_command="download",
    )
    monkeypatch.setattr(bs, "download_and_install_bw_binary", lambda: (True, ""))
    ok, detail = bs.run_install(plan)
    assert ok is True
    assert detail == ""


def test_run_install_fails_when_bw_still_missing(monkeypatch):
    plan = bs.InstallPlan(
        argv=(),
        description="binary",
        automated=True,
        terminal_command="download",
    )
    monkeypatch.setattr(bs, "download_and_install_bw_binary", lambda: (False, "failed"))
    ok, detail = bs.run_install(plan)
    assert ok is False
    assert detail == "failed"


def test_run_install_manual_plan_returns_false():
    plan = bs.InstallPlan(
        argv=(),
        description="manual",
        automated=False,
        terminal_command="manual steps",
    )
    ok, detail = bs.run_install(plan)
    assert ok is False
    assert detail == "manual steps"


def test_format_bitwarden_login_error_trusted_device():
    msg = bs._format_bitwarden_login_error("Trusted device policy active")
    assert "trusted-device" in msg.lower() or "Trusted device" in msg


def test_format_bitwarden_login_error_auth_challenge():
    msg = bs._format_bitwarden_login_error(
        "Your authentication request appears to be coming from a bot.",
    )
    assert "client secret" in msg.lower() or "auth-challenge" in msg.lower()
