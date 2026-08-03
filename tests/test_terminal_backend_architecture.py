"""Guard the TerminalWidget/backend dependency boundary."""

from pathlib import Path
import re


def test_frontend_does_not_reach_into_vte_backend():
    package = Path(__file__).parents[1] / "src/sshpilot"
    allowed = {"terminal_backends.py", "daemon_terminal_widget.py"}
    forbidden = {
        "VTE escape hatch": r"self\.vte\b",
        "backend VTE escape hatch": r"backend\.vte\b",
    }

    leaked = []
    for path in package.rglob("*.py"):
        if path.name in allowed:
            continue
        source = path.read_text()
        if path.name == "terminal.py":
            for name, pattern in {
                "VTE construction": r"Vte\.Terminal\s*\(",
                "VTE PTY ownership": r"Vte\.Pty\.",
            }.items():
                if re.search(pattern, source):
                    leaked.append(f"{path.relative_to(package)}: {name}")
        for name, pattern in forbidden.items():
            if re.search(pattern, source):
                leaked.append(f"{path.relative_to(package)}: {name}")
        if re.search(r"(?:term|terminal|term_widget|terminal_widget)\.vte\b", source):
            leaked.append(f"{path.relative_to(package)}: external VTE escape hatch")
    assert not leaked, "frontend bypasses BaseTerminalBackend: " + ", ".join(leaked)
