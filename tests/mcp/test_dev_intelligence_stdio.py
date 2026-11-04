"""V2 dev-intelligence tools over a real MCP stdio session."""

import json
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp.server.mcpserver")
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

REPO_ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.anyio

V2_TOOLS = {"get_context", "inspect_impact", "index_status", "diagnose_bug", "verify_change", "plan_feature"}


def _checkout(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    pkg = root / "src" / "sshpilot" / "demo"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "core.py").write_text(
        "def helper(x):\n    return x + 1\n\n\ndef caller():\n    return helper(1)\n"
    )
    tests = root / "tests" / "demo"
    tests.mkdir(parents=True)
    (tests / "test_core.py").write_text(
        "from sshpilot.demo.core import helper\n\n\ndef test_helper():\n    assert helper(1) == 2\n"
    )
    return root


def _params(root: Path, index_dir: Path, cwd: Path) -> StdioServerParameters:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    env["SSHPILOT_MCP_ROOT"] = str(root)
    env["SSHPILOT_MCP_INDEX_DIR"] = str(index_dir)
    return StdioServerParameters(command=sys.executable, args=["-m", "sshpilot.mcp.dev"], cwd=str(cwd), env=env)


async def _call(session, name, arguments):
    result = await session.call_tool(name, arguments)
    assert not result.is_error, result.content[0].text
    return json.loads(result.content[0].text)


async def test_v2_tools_over_stdio(tmp_path):
    root = _checkout(tmp_path)
    async with stdio_client(_params(root, tmp_path / "index", tmp_path)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            names = {tool.name for tool in (await session.list_tools()).tools}
            assert V2_TOOLS <= names
            assert {"find_symbol", "recommend_tests", "validate_change", "run_tests"} <= names  # legacy kept

            status = await _call(session, "index_status", {})
            assert status["schema"] == "sshpilot-dev-intel/1"
            assert status["refresh"]["reindexed"] == 3

            context = await _call(session, "get_context", {"reference": "helper"})
            assert context["symbol"]["id"] == "src/sshpilot/demo/core.py::helper"
            assert context["callers"][0]["source"] == "src/sshpilot/demo/core.py::caller"
            assert context["related_tests"][0]["test"] == "tests/demo/test_core.py::test_helper"
            assert context["budget"]["used_bytes"] <= context["budget"]["limit_bytes"]

            impact = await _call(session, "inspect_impact", {"paths": ["src/sshpilot/demo/core.py"]})
            assert "tests/demo/test_core.py" in impact["tests"]["focused_files"]

            trace = (
                'Traceback (most recent call last):\n'
                f'  File "{root}/src/sshpilot/demo/core.py", line 2, in helper\n'
                "TypeError: can only concatenate str (not \"int\") to str\n"
            )
            diagnosis = await _call(session, "diagnose_bug", {"symptoms": "caller() fails", "traceback": trace})
            assert diagnosis["candidates"][0]["id"] == "src/sshpilot/demo/core.py::helper"
            assert diagnosis["candidates"][0]["confidence"] == "high"

            plan = await _call(session, "plan_feature", {"request": "make helper() faster", "budget": 20000})
            assert plan["patterns"][0]["id"] == "src/sshpilot/demo/core.py::helper"

            bad_mode = await session.call_tool("verify_change", {"mode": "yolo"})
            assert bad_mode.is_error

            escape = await session.call_tool("get_context", {"reference": "../outside.py:1"})
            assert escape.is_error
            assert "escapes" in escape.content[0].text
