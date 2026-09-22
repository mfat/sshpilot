"""The Host Info display contract belongs to the frontend catalogue."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src" / "sshpilot"


def test_host_info_gettext_sources_are_in_potfiles():
    potfiles = set((ROOT / "po" / "POTFILES").read_text().splitlines())
    for relative in (
        "gtk/host_info_failure_messages.py",
        "machine_info_dialog.py",
        "host_info_tab.py",
        "host_info_payload.py",
        "host_info_shell.py",
    ):
        assert f"src/sshpilot/{relative}" in potfiles


def test_host_info_backend_has_no_gettext_or_finished_failure_message():
    service = (SOURCE / "daemon" / "host_info_service.py").read_text()
    assert "gettext" not in service
    assert "The host information probe failed" not in service
    assert "The remote host returned unreadable system information" not in service


def test_both_host_info_views_use_the_same_presenter():
    for relative in ("machine_info_dialog.py", "host_info_tab.py"):
        view = (SOURCE / relative).read_text()
        assert "from .gtk.host_info_failure_messages import" in view
        assert "format_host_info_failure(summary.failure)" in view
        assert "format_host_info_error(error)" in view
        assert "summary.failure.message" not in view
