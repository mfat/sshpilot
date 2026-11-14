"""A read-only SSH config is explained by the frontend, never by the daemon."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src" / "sshpilot"


def test_presenter_is_in_potfiles():
    potfiles = set((ROOT / "po" / "POTFILES").read_text(encoding="utf-8").splitlines())
    assert "src/sshpilot/gtk/ssh_config_access_messages.py" in potfiles


def test_backend_sends_a_code_and_no_explanation():
    for relative in (
        "core/connections/ssh_config_store.py",
        "core/connections/repository.py",
        "core/connection_application_service.py",
    ):
        source = (SOURCE / relative).read_text(encoding="utf-8")
        assert "gettext" not in source
        assert "Home Manager, so make the change" not in source
    store = (SOURCE / "core" / "connections" / "ssh_config_store.py").read_text(encoding="utf-8")
    assert "ErrorCode.SSH_CONFIG_READ_ONLY" in store


def test_every_gui_surface_uses_the_presenter():
    for relative in (
        "window.py",
        "connection_dialog.py",
        "ssh_config_editor_service.py",
    ):
        view = (SOURCE / relative).read_text(encoding="utf-8")
        assert "gtk.ssh_config_access_messages import" in view, relative
