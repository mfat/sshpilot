"""A configuration OpenSSH rejects is explained by the frontend, never by the daemon."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src" / "sshpilot"


def test_presenter_is_in_potfiles():
    potfiles = set((ROOT / "po" / "POTFILES").read_text().splitlines())
    assert "src/sshpilot/gtk/config_syntax_notice.py" in potfiles


def test_backend_sends_locations_and_no_explanation():
    for relative in (
        "core/ssh_config_effective.py",
        "core/connection_application_service.py",
        "api/models/connections.py",
        "daemon/dispatch.py",
    ):
        source = (SOURCE / relative).read_text()
        assert "gettext" not in source, relative
        assert "SSH connections cannot start" not in source, relative


def test_main_window_uses_the_presenter():
    window = (SOURCE / "window.py").read_text()
    assert "gtk.config_syntax_notice import" in window
    notice = (SOURCE / "gtk" / "config_syntax_notice.py").read_text()
    assert "list_config_syntax_errors" in notice
