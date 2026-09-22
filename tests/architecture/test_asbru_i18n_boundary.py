"""Ásbrú import messages are structured until GTK presents them."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src" / "sshpilot"


def test_asbru_presenter_is_extracted_into_pot():
    potfiles = (ROOT / "po" / "POTFILES").read_text()
    assert "src/sshpilot/gtk/asbru_import_messages.py" in potfiles


def test_asbru_backend_has_no_gettext_or_finished_summary():
    for relative in (
        "core/import_export/asbru.py",
        "core/connection_application_service.py",
    ):
        text = (SOURCE / relative).read_text()
        asbru = text if relative.endswith("asbru.py") else text.split("def preview_asbru_import", 1)[1].split("def _build_create_data", 1)[0]
        assert "gettext" not in asbru
        assert "connection(s)" not in asbru
        assert "Imported {" not in asbru


def test_window_uses_presenter_for_every_asbru_message_field():
    window = (SOURCE / "window_dialogs.py").read_text()
    asbru = window.split("def _begin_asbru_import", 1)[1].split("def _import_from_file", 1)[0]
    for rendered in (
        "format_asbru_messages(preview.errors)",
        "format_asbru_messages(preview.warnings[:8])",
        "format_asbru_result_message(result)",
        "format_asbru_messages(result.errors[:10])",
        "format_asbru_messages(result.partial_failures[:10])",
        "format_asbru_messages(result.warnings[:6])",
        "format_asbru_rpc_error(p[1])",
    ):
        assert rendered in asbru
    assert '"\\n".join(preview.errors)' not in asbru
    assert 'lines.extend(result.partial_failures' not in asbru
