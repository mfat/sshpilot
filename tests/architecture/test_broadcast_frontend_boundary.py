"""Phase 3 guards: frontend connection-targeted execution cannot fan out locally."""

import ast
from pathlib import Path


FRONTEND_FILES = (
    "src/sshpilot/window_broadcast.py",
    "src/sshpilot/terminal_manager.py",
    "src/sshpilot/command_blocks.py",
)


def test_broadcast_frontends_do_not_spawn_processes_or_construct_ssh():
    forbidden_imports = {"subprocess"}
    forbidden_calls = {"Popen", "system"}
    for filename in FRONTEND_FILES:
        tree = ast.parse(Path(filename).read_text(encoding="utf-8"), filename)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert not ({alias.name for alias in node.names} & forbidden_imports), filename
            if isinstance(node, ast.ImportFrom):
                assert node.module not in forbidden_imports, filename
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", getattr(node.func, "id", ""))
                assert name not in forbidden_calls, filename
                assert not any(
                    keyword.arg == "shell"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                    for keyword in node.keywords
                ), filename


def test_connection_targeted_command_blocks_have_no_terminal_fanout_helpers():
    source = Path("src/sshpilot/command_blocks.py").read_text(encoding="utf-8")
    assert "_feed_connections_in_split_view" not in source
    assert "_feed_specific_terminal" not in source
    assert "connection-established" not in source
    # feed_child_data remains valid only for the active-terminal interactive
    # insertion path; it must occur exactly once in this module.
    assert source.count("feed_child_data") == 1


def test_daemon_broadcast_service_is_gtk_free():
    source = Path("src/sshpilot/daemon/broadcast_service.py").read_text(encoding="utf-8")
    assert "gi.repository" not in source
    assert "Gtk" not in source
