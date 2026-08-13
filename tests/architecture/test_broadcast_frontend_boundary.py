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
    tree = ast.parse(source)
    methods = [
        node
        for cls in tree.body
        if isinstance(cls, ast.ClassDef)
        for node in cls.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    feed_owners = {
        node.name
        for node in methods
        if any(
            isinstance(child, ast.Call)
            and getattr(child.func, "attr", "") == "feed_child_data"
            for child in ast.walk(node)
        )
    }
    # Terminal input exists only for ordinary active-terminal insertion and
    # commands explicitly marked interactive_terminal.
    assert feed_owners == {"_feed_terminal", "_feed_interactive_when_connected"}


def test_daemon_broadcast_service_is_gtk_free():
    source = Path("src/sshpilot/daemon/broadcast_service.py").read_text(encoding="utf-8")
    assert "gi.repository" not in source
    assert "Gtk" not in source
