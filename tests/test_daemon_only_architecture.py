"""Static dependency checks for the daemon-only production path."""
from pathlib import Path

ROOT = Path(__file__).parents[1]
SRC = ROOT / "src" / "sshpilot"


def test_removed_backend_implementation_is_absent():
    assert not (SRC / "api" / "core_service_client.py").exists()
    runtime = "\n".join(p.read_text(errors="ignore") for p in SRC.rglob("*.py"))
    for obsolete in ("removed frontend backend", "removed manager adapter", "removed connection adapter", "removed backend mode"):
        assert obsolete not in runtime


def test_gtk_factory_is_daemon_only():
    factory = (SRC / "api" / "client_factory.py").read_text()
    assert "connect_or_start" in factory
    assert "except" not in factory
    assert "ConnectionApplicationService" not in factory


def test_daemon_injects_services_directly():
    server = (SRC / "daemon" / "server.py").read_text()
    dispatch = (SRC / "daemon" / "dispatch.py").read_text()
    assert "connections: ConnectionApplicationService" in server
    assert "self._connections = connection_service" in dispatch


def test_core_and_daemon_do_not_import_widget_toolkits():
    import ast
    forbidden = {"gi", "Gtk", "Adw", "Vte", "WebKit", "WebKit2"}
    for directory in (SRC / "core", SRC / "daemon"):
        for path in directory.rglob("*.py"):
            tree = ast.parse(path.read_text())
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])
                    imported.update(alias.name for alias in node.names)
            assert imported.isdisjoint(forbidden), path
