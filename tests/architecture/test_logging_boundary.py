from __future__ import annotations

import ast
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[2] / "src" / "sshpilot"


def _python_files(root: Path):
    return tuple(root.rglob("*.py"))


def test_shared_logging_policy_is_gi_free():
    text = (SOURCE / "logging_support.py").read_text(encoding="utf-8")
    tree = ast.parse(text)
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    )
    assert not imported.intersection({"gi", "Gio", "GLib", "Gtk", "Adw", "Vte"})


def test_rotating_file_configuration_is_registered_once():
    occurrences = []
    for path in _python_files(SOURCE):
        text = path.read_text(encoding="utf-8")
        if "RotatingFileHandler" in text:
            occurrences.append(path.relative_to(SOURCE).as_posix())
    assert occurrences == ["logging_support.py"]


def test_daemon_and_frontend_files_do_not_cross_write():
    daemon_text = chr(10).join(
        path.read_text(encoding="utf-8")
        for path in (SOURCE / "daemon").rglob("*.py")
    )
    assert "'sshpilot.log'" not in daemon_text
    assert '"sshpilot.log"' not in daemon_text
    assert "'app.log'" not in daemon_text
    assert '"app.log"' not in daemon_text
    assert "'ssh.log'" not in daemon_text
    assert '"ssh.log"' not in daemon_text

    frontend_writers = (SOURCE / "main.py").read_text(encoding="utf-8")
    assert "'daemon.log'" not in frontend_writers
    assert '"daemon.log"' not in frontend_writers


def test_sensitive_transport_payloads_are_not_logged_directly():
    sensitive_attrs = {
        "command",
        "data",
        "params",
        "frame",
        "environment",
        "payload",
        "content",
    }
    for path in (SOURCE / "api").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "logger.debug(request.params)" not in text
        assert "logger.info(request.params)" not in text
        assert "logger.debug(frame)" not in text
        assert "logger.info(frame)" not in text
        tree = ast.parse(text, filename=str(path))
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
                continue
            if call.func.attr not in {"debug", "info", "warning", "error", "exception", "critical", "log"}:
                continue
            for argument in call.args[1:]:
                for node in ast.walk(argument):
                    if isinstance(node, ast.Attribute) and node.attr in sensitive_attrs:
                        raise AssertionError(
                            f"sensitive logging field {node.attr!r} in {path}"
                        )
                    if isinstance(node, ast.Name) and node.id in {"environment"}:
                        raise AssertionError(
                            f"sensitive logging value {node.id!r} in {path}"
                        )


def test_frontend_terminal_and_broadcast_boundaries_do_not_log_payload_fields():
    """Keep the known GTK operational entry points free of raw input logs."""

    sensitive_attrs = {"command", "data", "frame", "payload", "content"}
    for relative in ("terminal_manager.py", "window_broadcast.py"):
        path = SOURCE / relative
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
                continue
            if call.func.attr not in {
                "debug", "info", "warning", "error", "exception", "critical", "log"
            }:
                continue
            for argument in call.args[1:]:
                for node in ast.walk(argument):
                    if isinstance(node, ast.Attribute) and node.attr in sensitive_attrs:
                        raise AssertionError(
                            f"sensitive logging field {node.attr!r} in {path}"
                        )
    for path in (SOURCE / "daemon").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "logger.debug(request.params)" not in text
        assert "logger.info(request.params)" not in text
        assert "logger.debug(frame)" not in text
        assert "logger.info(frame)" not in text
        tree = ast.parse(text, filename=str(path))
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
                continue
            if call.func.attr not in {"debug", "info", "warning", "error", "exception", "critical", "log"}:
                continue
            for argument in call.args[1:]:
                for node in ast.walk(argument):
                    if isinstance(node, ast.Attribute) and node.attr in sensitive_attrs:
                        raise AssertionError(
                            f"sensitive logging field {node.attr!r} in {path}"
                        )
                    if isinstance(node, ast.Name) and node.id in {"environment"}:
                        raise AssertionError(
                            f"sensitive logging value {node.id!r} in {path}"
                        )
