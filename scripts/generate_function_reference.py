#!/usr/bin/env python3
"""Generate a Markdown reference of all functions and methods in sshpilot package."""
from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path
from textwrap import shorten
from typing import Dict, List, Optional, Tuple

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "sshpilot"
OUTPUT_PATH = Path(__file__).resolve().parents[1] / "documentation" / "function-reference.md"


class FunctionRecord:
    __slots__ = ("module", "qualname", "name", "args", "doc", "is_method", "class_path")

    def __init__(
        self,
        module: str,
        qualname: str,
        name: str,
        args: str,
        doc: Optional[str],
        is_method: bool,
        class_path: Tuple[str, ...],
    ) -> None:
        self.module = module
        self.qualname = qualname
        self.name = name
        self.args = args
        self.doc = doc
        self.is_method = is_method
        self.class_path = class_path


def format_args(node: ast.FunctionDef) -> str:
    params = []
    args = node.args
    defaults = [None] * (len(args.args) - len(args.defaults)) + list(args.defaults)
    for arg, default in zip(args.args, defaults):
        if arg.arg == "self":
            continue
        if arg.arg == "cls":
            continue
        if default is None:
            params.append(arg.arg)
        else:
            params.append(f"{arg.arg}={ast.unparse(default) if hasattr(ast, 'unparse') else '…'}")
    if args.vararg:
        params.append(f"*{args.vararg.arg}")
    if args.kwonlyargs:
        for kwarg, default in zip(args.kwonlyargs, args.kw_defaults):
            if default is None:
                params.append(f"{kwarg.arg}")
            else:
                params.append(f"{kwarg.arg}={ast.unparse(default) if hasattr(ast, 'unparse') else '…'}")
    if args.kwarg:
        params.append(f"**{args.kwarg.arg}")
    return ", ".join(params)


def simplify_doc(doc: Optional[str], name: str) -> str:
    if doc:
        first_line = doc.strip().splitlines()[0].strip()
        return shorten(first_line, width=160, placeholder="…")
    # Auto-generate brief explanation from name
    clean = name.strip("_")
    if clean == "__init__":
        return "Initializes the instance."  # Standard init summary
    words = clean.replace("_", " ")
    lower = words.lower()
    prefixes = {
        "get ": "Returns ",
        "set ": "Sets ",
        "load ": "Loads ",
        "save ": "Saves ",
        "create ": "Creates ",
        "build ": "Builds ",
        "show ": "Shows ",
        "open ": "Opens ",
        "update ": "Updates ",
        "refresh ": "Refreshes ",
        "toggle ": "Toggles ",
        "handle ": "Handles ",
        "remove ": "Removes ",
        "add ": "Adds ",
        "delete ": "Deletes ",
        "connect ": "Connects ",
        "on ": "Handles ",
        "is ": "Checks whether ",
        "has ": "Determines whether ",
    }
    explanation = None
    for prefix, phrase in prefixes.items():
        if lower.startswith(prefix):
            remainder = words[len(prefix):].strip()
            if remainder:
                explanation = phrase + remainder
            break
    if not explanation:
        explanation = f"Handles {words}"
    if not explanation.endswith('.'):
        explanation += '.'
    explanation = explanation[0].upper() + explanation[1:]
    return explanation


class FunctionCollector(ast.NodeVisitor):
    def __init__(self, module: str) -> None:
        self.module = module
        self.class_stack: List[str] = []
        self.records: List[FunctionRecord] = []
        self.function_depth = 0

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.class_stack.append(node.name)
        self.generic_visit(node)
        self.class_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # type: ignore[override]
        if self.function_depth == 0:
            self._add_record(node)
        self.function_depth += 1
        self.generic_visit(node)
        self.function_depth -= 1

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # type: ignore[override]
        if self.function_depth == 0:
            self._add_record(node)
        self.function_depth += 1
        self.generic_visit(node)
        self.function_depth -= 1

    def _add_record(self, node: ast.AST) -> None:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return
        qual_parts = list(self.class_stack) + [node.name]
        qualname = ".".join(qual_parts)
        is_method = bool(self.class_stack)
        doc = ast.get_docstring(node)
        args = format_args(node)
        record = FunctionRecord(
            module=self.module,
            qualname=qualname,
            name=node.name,
            args=args,
            doc=doc,
            is_method=is_method,
            class_path=tuple(self.class_stack),
        )
        self.records.append(record)


def iter_python_modules(root: Path):
    for path in sorted(root.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        yield path


def collect_functions() -> List[FunctionRecord]:
    records: List[FunctionRecord] = []
    for module_path in iter_python_modules(PACKAGE_ROOT):
        module_rel = module_path.relative_to(PACKAGE_ROOT).with_suffix("")
        module_name = "sshpilot." + ".".join(module_rel.parts)
        source = module_path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            raise RuntimeError(f"Failed to parse {module_path}: {exc}") from exc
        collector = FunctionCollector(module_name)
        collector.visit(tree)
        records.extend(collector.records)
    return records


def generate_markdown(records: List[FunctionRecord]) -> str:
    lines: List[str] = []
    lines.append("# Function and Method Reference\n")
    lines.append("This document enumerates the functions and methods available in the `sshpilot` package."\
                 " Each entry includes its signature and a brief description."\
                 "\n")
    modules: Dict[str, List[FunctionRecord]] = defaultdict(list)
    for record in records:
        modules[record.module].append(record)

    for module_name in sorted(modules):
        lines.append(f"## Module: `{module_name}`\n")
        module_records = modules[module_name]
        # Group by class path
        by_class: Dict[Tuple[str, ...], List[FunctionRecord]] = defaultdict(list)
        for record in sorted(module_records, key=lambda r: (r.class_path, r.qualname)):
            by_class[record.class_path].append(record)

        # Top-level functions first (empty class path)
        if () in by_class:
            lines.append("### Functions\n")
            for record in by_class[()]:
                signature = f"{record.name}({record.args})" if record.args else f"{record.name}()"
                description = simplify_doc(record.doc, record.name)
                lines.append(f"- **`{signature}`** — {description}\n")
            del by_class[()]

        for class_path in sorted(by_class):
            class_title = ".".join(class_path)
            lines.append(f"### Class: `{class_title}`\n")
            for record in by_class[class_path]:
                signature = f"{record.name}({record.args})" if record.args else f"{record.name}()"
                description = simplify_doc(record.doc, record.name)
                lines.append(f"- **`{signature}`** — {description}\n")
        lines.append("\n")
    return "\n".join(lines).strip() + "\n"


def main() -> None:
    records = collect_functions()
    markdown = generate_markdown(records)
    OUTPUT_PATH.write_text(markdown, encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
