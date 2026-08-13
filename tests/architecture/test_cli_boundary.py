from __future__ import annotations

import ast
from pathlib import Path


CLI_ROOT = Path("src/sshpilot/cli")
FORBIDDEN_MODULES = {
    "gi",
    "paramiko",
    "subprocess",
    "sshpilot.core",
    "sshpilot.daemon",
    "sshpilot.gtk",
}


def test_reference_cli_is_frontend_neutral_and_has_no_process_fallback():
    for path in CLI_ROOT.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                continue
            for module in modules:
                assert module not in FORBIDDEN_MODULES
                assert not module.startswith("gi.")
                assert not module.startswith("sshpilot.core.")
                assert not module.startswith("sshpilot.daemon.")
                assert not module.startswith("sshpilot.gtk.")

        source = path.read_text(encoding="utf-8")
        assert "subprocess" not in source
        assert "paramiko" not in source
        assert "shell=True" not in source
        assert "os.system" not in source

