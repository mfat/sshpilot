"""Keep daemon session failure translation in the frontend."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_session_failure_gettext_boundary_and_potfiles():
    runtime = (ROOT / "src/sshpilot/daemon/session_runtime.py").read_text()
    presenter = (ROOT / "src/sshpilot/gtk/session_failure_messages.py").read_text()
    sidebar = (ROOT / "src/sshpilot/gtk/connection_runtime_status.py").read_text()
    controller = (ROOT / "src/sshpilot/terminal_session_controller.py").read_text()
    potfiles = (ROOT / "po/POTFILES").read_text().splitlines()

    assert "gettext" not in runtime
    assert "N_(" not in runtime
    assert "SessionFailureCode" in runtime
    assert "N_(" in presenter
    assert "_(template).format(**failure.parameters)" in presenter
    assert "_(failure.diagnostic)" not in presenter
    assert "format_session_failure(" in sidebar
    assert "format_session_failure(" in controller
    assert "failure.message" not in sidebar
    assert "failure.message" not in controller
    assert "src/sshpilot/gtk/session_failure_messages.py" in potfiles
    assert "src/sshpilot/terminal_session_controller.py" in potfiles
    assert "src/sshpilot/daemon/session_runtime.py" not in potfiles
