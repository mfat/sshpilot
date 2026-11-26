"""A refused connection save is explained by the frontend, never by the daemon."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src" / "sshpilot"
PRESENTER = "src/sshpilot/gtk/connection_save_messages.py"


def test_presenter_is_in_potfiles():
    potfiles = set((ROOT / "po" / "POTFILES").read_text(encoding="utf-8").splitlines())
    assert PRESENTER in potfiles


def test_backend_sends_a_refusal_code_and_no_explanation():
    for relative in (
        "core/connections/ssh_config_store.py",
        "core/connections/repository.py",
        "core/connection_application_service.py",
        "api/errors.py",
    ):
        source = (SOURCE / relative).read_text(encoding="utf-8")
        assert "gettext" not in source, relative
        assert "can't tell which" not in source, relative
    service = (SOURCE / "core" / "connection_application_service.py").read_text(encoding="utf-8")
    assert 'details={"refusal": refusal}' in service


def test_every_refusal_the_api_names_has_wording():
    from sshpilot.api.errors import MutationRefusal
    from sshpilot.gtk import connection_save_messages as messages

    assert set(messages._REFUSALS) == set(MutationRefusal)


def test_the_window_asks_the_presenter():
    window = (SOURCE / "window.py").read_text(encoding="utf-8")
    assert "gtk.connection_save_messages import save_failure_text" in window
