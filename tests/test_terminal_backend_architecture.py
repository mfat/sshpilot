"""Guard the TerminalWidget/backend dependency boundary."""

from pathlib import Path
import re


def test_terminal_widget_does_not_reach_into_vte_backend():
    source = (Path(__file__).parents[1] / "src/sshpilot/terminal.py").read_text()
    forbidden = {
        "VTE construction": r"Vte\.Terminal\s*\(",
        "VTE PTY ownership": r"Vte\.Pty\.",
        "VTE escape hatch": r"self\.vte\b",
        "backend VTE escape hatch": r"backend\.vte\b",
    }

    leaked = [name for name, pattern in forbidden.items() if re.search(pattern, source)]
    assert not leaked, f"terminal.py bypasses BaseTerminalBackend: {', '.join(leaked)}"

