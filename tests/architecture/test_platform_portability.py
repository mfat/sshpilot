"""Windows rules that have a single correct form, checked on every platform.

Windows CI runs only on pull requests, so these catch the two mistakes that
break Windows outright before anything runs there. See
docs/development/windows-compatibility.md.
"""
from __future__ import annotations

import ast
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[2] / "src" / "sshpilot"

POSIX_ONLY_MODULES = frozenset({"fcntl", "pty", "termios", "tty", "pwd", "grp", "resource"})

# Modules Windows never imports. Anything else that needs a POSIX-only module
# imports it inside a POSIX branch or a ``try``.
POSIX_ONLY_FILES = frozenset({
    "daemon/pty_runner.py",
    "sshpilot_agent.py",
})

# The one place allowed to probe with ``os.kill(pid, 0)``.
PID_PROBE = "platform/process.py"


def _sources():
    for path in sorted(SOURCE.rglob("*.py")):
        rel = path.relative_to(SOURCE).as_posix()
        yield rel, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_posix_only_modules_are_not_imported_at_module_level():
    offenders = []
    for rel, tree in _sources():
        if rel in POSIX_ONLY_FILES:
            continue
        for node in tree.body:
            if isinstance(node, ast.Import):
                names = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = {node.module.split(".")[0]}
            else:
                continue
            for name in sorted(names & POSIX_ONLY_MODULES):
                offenders.append(f"{rel}:{node.lineno} imports {name}")
    assert not offenders, (
        "These modules do not exist on Windows; import them inside a POSIX "
        "branch or a try block:\n" + "\n".join(offenders)
    )


def test_os_kill_zero_is_only_used_by_the_pid_probe():
    offenders = []
    for rel, tree in _sources():
        if rel == PID_PROBE:
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "kill"
                and len(node.args) == 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value == 0
            ):
                offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, (
        "os.kill(pid, 0) terminates the process on Windows; use "
        "sshpilot.platform.process.pid_alive:\n" + "\n".join(offenders)
    )


def test_pid_probe_still_exists():
    # Keep the allowlist honest if the probe moves.
    assert (SOURCE / PID_PROBE).is_file()


def test_platform_helpers_are_gi_free():
    # Core and the daemon import these; they must not pull in GObject.
    for rel in ("platform/detect.py", "platform/process.py", "platform/fs.py", "system_terminal.py"):
        tree = ast.parse((SOURCE / rel).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
        assert "gi" not in imported, rel
