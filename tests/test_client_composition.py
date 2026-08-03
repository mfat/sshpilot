from pathlib import Path


def test_gtk_composition_only_constructs_daemon_client():
    root = Path(__file__).parents[1] / "src" / "sshpilot"
    factory = (root / "api" / "client_factory.py").read_text()
    window = (root / "window.py").read_text()
    assert "connect_or_start" in factory
    assert "ConnectionApplicationService" not in window
    assert "retry_daemon_connection" in window
