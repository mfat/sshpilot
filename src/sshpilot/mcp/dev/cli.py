"""Command-line adapter for the development-intelligence engine.

Same operations and JSON results as the MCP tools, without the optional MCP
SDK. Examples::

    python -m sshpilot.mcp.dev.cli index-status
    python -m sshpilot.mcp.dev.cli get-context sshpilot.daemon.dispatch.RequestDispatcher._handle_open_session
    python -m sshpilot.mcp.dev.cli inspect-impact --base HEAD
    python -m sshpilot.mcp.dev.cli diagnose-bug --symptoms "..." --traceback-file trace.txt
    python -m sshpilot.mcp.dev.cli verify-change --base HEAD --mode focused

Exit status: 0 on success, 2 for invalid input, 1 when ``verify-change``
reports anything other than ``passed``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from .._scope import RepoScope, ScopeError
from .engine import DevIntelligence, EngineError

MAX_TEXT_INPUT = 64 * 1024


def _read_text_arg(value: Optional[str], file_value: Optional[str]) -> Optional[str]:
    if file_value:
        if file_value == "-":
            return sys.stdin.read(MAX_TEXT_INPUT)
        return Path(file_value).read_text(encoding="utf-8", errors="replace")[:MAX_TEXT_INPUT]
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sshpilot-dev-intel", description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", help="checkout root (default: SSHPILOT_MCP_ROOT or discovery from CWD)")
    parser.add_argument("--index-dir", help="index directory (default: SSHPILOT_MCP_INDEX_DIR or the XDG cache)")
    parser.add_argument("--budget", type=int, default=None, help="reply budget in bytes")
    parser.add_argument("--compact", action="store_true", help="print JSON on one line")
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("index-status", help="refresh the index and report freshness")
    status.add_argument("--rebuild", action="store_true", help="discard and rebuild the index")

    context = sub.add_parser("get-context", help="bounded context for a symbol reference")
    context.add_argument("reference")
    context.add_argument("--no-source", action="store_true")
    context.add_argument("--depth", type=int, default=1)

    impact = sub.add_parser("inspect-impact", help="impact of changes since a base or of given paths")
    impact.add_argument("--base")
    impact.add_argument("--path", action="append", dest="paths")
    impact.add_argument("--depth", type=int, default=2)

    diagnose = sub.add_parser("diagnose-bug", help="rank candidate locations for a failure")
    diagnose.add_argument("--symptoms", default="")
    diagnose.add_argument("--symptoms-file")
    diagnose.add_argument("--traceback")
    diagnose.add_argument("--traceback-file")
    diagnose.add_argument("--test-name")
    diagnose.add_argument("--changed-path", action="append", dest="changed_paths")
    diagnose.add_argument("--base", help="use paths changed since this revision as evidence")
    diagnose.add_argument("--max-items", type=int, default=8)

    verify = sub.add_parser("verify-change", help="static checks plus optional focused tests")
    verify.add_argument("--base", default="HEAD")
    verify.add_argument("--mode", choices=("static", "focused"), default="static")
    verify.add_argument("--suite", action="append", dest="suites")
    verify.add_argument("--max-test-files", type=int, default=8)
    return parser


def run(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        scope = RepoScope(Path(args.root)) if args.root else RepoScope.discover()
    except ScopeError as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 2
    service = DevIntelligence(scope, index_dir=Path(args.index_dir) if args.index_dir else None)
    exit_code = 0
    try:
        if args.command == "index-status":
            result = service.index_status(rebuild=args.rebuild)
        elif args.command == "get-context":
            result = service.get_context(args.reference, include_source=not args.no_source, depth=args.depth, budget=args.budget)
        elif args.command == "inspect-impact":
            result = service.inspect_impact(base=args.base, paths=args.paths, depth=args.depth, budget=args.budget)
        elif args.command == "diagnose-bug":
            result = service.diagnose_bug(
                symptoms=_read_text_arg(args.symptoms, args.symptoms_file) or "",
                traceback=_read_text_arg(args.traceback, args.traceback_file),
                test_name=args.test_name,
                changed_paths=args.changed_paths,
                base=args.base,
                max_items=args.max_items,
                budget=args.budget,
            )
        elif args.command == "verify-change":
            result = service.verify_change(
                base=args.base,
                mode=args.mode,
                suites=args.suites,
                max_test_files=args.max_test_files,
                budget=args.budget,
            )
            exit_code = 0 if result.get("overall") == "passed" else 1
        else:  # pragma: no cover - argparse enforces the choices
            raise EngineError(f"unknown command {args.command}")
    except (EngineError, ScopeError) as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 2
    finally:
        service.close()
    print(json.dumps(result, indent=None if args.compact else 2, sort_keys=False, default=str))
    return exit_code


def main() -> int:
    return run()


if __name__ == "__main__":
    sys.exit(main())
