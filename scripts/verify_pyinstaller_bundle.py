#!/usr/bin/env python3
"""Utilities to validate PyInstaller bundles.

This module provides a CLI that performs two classes of checks:

1. Build-time validation that ensures the bundle contains every file that
   PyInstaller intended to ship.
2. Runtime validation that exercises the frozen executable with diagnostic
   environment variables so that we can verify it loads modules, dynamic
   libraries and data files from the bundle instead of the host system.

The script is intentionally defensive and surfaces actionable diagnostics in
addition to a pass/fail status.  It is designed to be used in CI pipelines after
producing a bundle with ``pyinstaller`` but can also be executed manually while
iterating locally.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shlex
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, MutableMapping, Optional, Sequence, Tuple


@dataclass
class BundleValidationResult:
    """Stores the outcome of the static bundle validation step."""

    missing_files: List[Path] = field(default_factory=list)
    warn_lines: List[str] = field(default_factory=list)
    toc_files_checked: List[Path] = field(default_factory=list)

    @property
    def is_successful(self) -> bool:
        return not self.missing_files and not self.warn_lines


@dataclass
class RuntimeValidationResult:
    """Stores runtime validation output and verdicts."""

    command: Sequence[str]
    returncode: Optional[int]
    stdout: str
    stderr: str
    loader_paths: List[Path] = field(default_factory=list)
    loader_failures: List[str] = field(default_factory=list)
    import_warnings: List[str] = field(default_factory=list)
    sys_path_entries: List[str] = field(default_factory=list)
    _meipass: Optional[Path] = None

    @property
    def combined_output(self) -> str:
        if self.stderr:
            return f"{self.stdout}\n{self.stderr}" if self.stdout else self.stderr
        return self.stdout

    @property
    def is_successful(self) -> bool:
        return (
            self.returncode == 0
            and not self.loader_failures
            and not self.import_warnings
        )


@dataclass
class BundleReport:
    """Aggregate report returned from :func:`validate_bundle`."""

    static: BundleValidationResult
    runtime: Optional[RuntimeValidationResult]

    @property
    def is_successful(self) -> bool:
        return self.static.is_successful and (self.runtime is None or self.runtime.is_successful)


def parse_arguments(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate that a PyInstaller bundle is self-contained and uses bundled resources at runtime.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """
            Examples
            --------
            Validate an onedir bundle and run it with ``--version`` to capture loader diagnostics::

                python scripts/verify_pyinstaller_bundle.py dist/sshpilot/sshpilot --run-args --version

            Validate a onefile bundle and allow the system OpenSSL library (required on many Linux distros)::

                python scripts/verify_pyinstaller_bundle.py dist/sshpilot --allow-system-path /usr/lib/libssl.so
            """
        ),
    )
    parser.add_argument(
        "bundle",
        type=Path,
        help="Path to the PyInstaller executable (onefile) or the executable inside the onedir bundle.",
    )
    parser.add_argument(
        "--bundle-root",
        type=Path,
        help="Explicit path to the bundle root directory (defaults to the executable's parent).",
    )
    parser.add_argument(
        "--build-dir",
        type=Path,
        help=(
            "Directory that contains PyInstaller build artefacts such as COLLECT-00.toc and warn-*.txt. "
            "If omitted, the script will search the parent directories for the most recent build directory."
        ),
    )
    parser.add_argument(
        "--run-args",
        nargs=argparse.REMAINDER,
        help=(
            "Arguments passed to the bundle during runtime validation.  Prefix the option with ``--run-args`` "
            "to signal the end of verifier options."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="Maximum number of seconds to wait for the runtime validation command to finish (default: 60).",
    )
    parser.add_argument(
        "--allow-system-path",
        action="append",
        default=[],
        metavar="PATH",
        help=(
            "Path prefixes that are permitted to appear in loader diagnostics.  Use this for unavoidable system dependencies "
            "such as graphics drivers or libc.  The prefix comparison is case-sensitive."
        ),
    )
    parser.add_argument(
        "--require",
        action="append",
        default=[],
        metavar="RELATIVE_PATH",
        help="Relative paths that must exist inside the bundle root.  Can be specified multiple times.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the final report as JSON for consumption in CI systems.",
    )
    parser.add_argument(
        "--skip-runtime",
        action="store_true",
        help="Skip the runtime validation stage (only perform static bundle checks).",
    )
    parser.add_argument(
        "--python-executable",
        type=Path,
        help=(
            "Override the executable that should be launched during runtime validation.  "
            "Useful for macOS .app bundles where the runnable binary lives under Contents/MacOS."
        ),
    )
    return parser.parse_args(argv)


def _infer_bundle_root(bundle_path: Path, explicit_root: Optional[Path]) -> Path:
    if explicit_root:
        return explicit_root.resolve()
    if bundle_path.is_dir():
        return bundle_path.resolve()
    return bundle_path.resolve().parent


def _discover_build_dir(bundle_root: Path, explicit_build_dir: Optional[Path]) -> Optional[Path]:
    if explicit_build_dir:
        return explicit_build_dir.resolve()

    # Search for a sibling "build" directory with the freshest timestamp.
    candidate_dirs = []
    for root in {bundle_root, bundle_root.parent, bundle_root.parent.parent}:
        build_dir = root / "build"
        if build_dir.is_dir():
            candidate_dirs.append(build_dir)
    if not candidate_dirs:
        return None
    candidate_dirs.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return candidate_dirs[0]


def _load_toc(path: Path) -> Optional[List[Tuple[str, str, str]]]:
    try:
        with path.open("r", encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        return None
    try:
        toc = ast.literal_eval(content)
    except (SyntaxError, ValueError) as exc:
        raise ValueError(f"Failed to parse TOC file {path}: {exc}") from exc
    if not isinstance(toc, list):
        raise ValueError(f"Unexpected TOC format in {path!s}")
    return toc


def _validate_collect_entries(bundle_root: Path, toc_entries: Iterable[Tuple[str, str, str]]) -> List[Path]:
    missing: List[Path] = []
    for entry in toc_entries:
        if not isinstance(entry, (tuple, list)) or len(entry) < 2:
            continue
        relative_dest = entry[0]
        dest_path = bundle_root / relative_dest
        if not dest_path.exists():
            missing.append(dest_path)
    return missing


def _read_warn_file(build_dir: Path) -> List[str]:
    warn_files = sorted(build_dir.glob("warn-*.txt"))
    lines: List[str] = []
    for warn_file in warn_files:
        with warn_file.open("r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("--"):
                    continue
                lines.append(line)
    return lines


def perform_static_validation(bundle_path: Path, bundle_root: Path, build_dir: Optional[Path], required_paths: Sequence[str]) -> BundleValidationResult:
    result = BundleValidationResult()
    if build_dir and build_dir.is_dir():
        collect_toc = build_dir / "COLLECT-00.toc"
        toc_entries = _load_toc(collect_toc)
        if toc_entries is not None:
            result.toc_files_checked.append(collect_toc)
            result.missing_files.extend(_validate_collect_entries(bundle_root, toc_entries))
        pkg_toc = build_dir / "PKG-00.toc"
        toc_entries = _load_toc(pkg_toc)
        if toc_entries is not None:
            result.toc_files_checked.append(pkg_toc)
            result.missing_files.extend(_validate_collect_entries(bundle_root, toc_entries))
        result.warn_lines.extend(_read_warn_file(build_dir))
    else:
        result.warn_lines.append("Unable to locate build directory and warn-*.txt files.")

    for rel_path in required_paths:
        target = bundle_root / rel_path
        if not target.exists():
            result.missing_files.append(target)
    return result


_LOADER_PATH_RE = re.compile(r"LOADER: (?:Loaded|Adding|Using) .*?from (.*)")
_IMPORT_FROM_RE = re.compile(r"import [\w.]+ # from '([^']+)'", re.IGNORECASE)
_SYSPATH_RE = re.compile(r"LOADER: sys\.path is \[(.*)]")
_MEIPASS_RE = re.compile(r"LOADER: (?:Python library directory|Temporary directory) is (.*)")


def _normalize_allowlist(bundle_root: Path, allow_paths: Sequence[str]) -> List[Path]:
    normalized: List[Path] = [bundle_root]
    for entry in allow_paths:
        try:
            normalized.append(Path(entry).resolve())
        except OSError:
            continue
    return normalized


def _path_is_allowed(path: Path, allowed_prefixes: Sequence[Path]) -> bool:
    for prefix in allowed_prefixes:
        try:
            path.relative_to(prefix)
            return True
        except ValueError:
            continue
    return False


def _parse_sys_path_entries(match: re.Match[str]) -> List[str]:
    raw = match.group(1)
    if not raw:
        return []
    # We parse the list literal using ast to respect quoting.
    try:
        parsed = ast.literal_eval(f"[{raw}]")
    except Exception:
        return [entry.strip().strip("'\"") for entry in raw.split(",")]
    return [str(item) for item in parsed]


def run_runtime_validation(
    executable: Path,
    bundle_root: Path,
    run_args: Sequence[str],
    timeout: float,
    allow_system_paths: Sequence[str],
) -> RuntimeValidationResult:
    command = [str(executable)] + list(run_args)
    env: MutableMapping[str, str] = dict(os.environ)
    env.update(
        {
            "PYINSTALLER_LAUNCHER_VERBOSE": "1",
            "PYTHONVERBOSE": "2",
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": "",
        }
    )
    env.pop("PYTHONHOME", None)

    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            timeout=timeout,
            check=False,
            text=True,
        )
        returncode = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except subprocess.TimeoutExpired as exc:
        return RuntimeValidationResult(
            command=command,
            returncode=None,
            stdout=exc.stdout or "",
            stderr=(exc.stderr or "") + f"\nProcess timed out after {timeout}s",
            loader_failures=[f"Process timed out after {timeout}s"],
        )
    except FileNotFoundError:
        return RuntimeValidationResult(
            command=command,
            returncode=None,
            stdout="",
            stderr=f"Executable {executable!s} does not exist or is not runnable.",
            loader_failures=["Executable not found"],
        )

    result = RuntimeValidationResult(
        command=command,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )

    combined_output = result.combined_output
    allowed_prefixes = _normalize_allowlist(bundle_root, allow_system_paths)

    for line in combined_output.splitlines():
        loader_match = _LOADER_PATH_RE.search(line)
        if loader_match:
            candidate = Path(loader_match.group(1).strip())
            result.loader_paths.append(candidate)
            if not _path_is_allowed(candidate, allowed_prefixes):
                result.loader_failures.append(
                    f"Loader referenced non-bundled path: {candidate}"
                )
            continue
        meipass_match = _MEIPASS_RE.search(line)
        if meipass_match:
            result._meipass = Path(meipass_match.group(1).strip())
            continue
        sys_path_match = _SYSPATH_RE.search(line)
        if sys_path_match:
            result.sys_path_entries = _parse_sys_path_entries(sys_path_match)
            for entry in result.sys_path_entries:
                try:
                    entry_path = Path(entry)
                except Exception:
                    continue
                if entry_path.exists() and not _path_is_allowed(entry_path, allowed_prefixes):
                    result.loader_failures.append(
                        f"sys.path entry resolves outside bundle: {entry_path}"
                    )
            continue
        import_match = _IMPORT_FROM_RE.search(line)
        if import_match:
            source = import_match.group(1)
            if source.startswith("<"):
                # Built-in or frozen module.
                continue
            if "PYZ-00.pyz" in source or source.startswith("zipimport://"):
                continue
            source_path = Path(source)
            if not _path_is_allowed(source_path, allowed_prefixes):
                result.import_warnings.append(
                    f"Module imported from non-bundled path: {source_path}"
                )

    if result._meipass and not _path_is_allowed(result._meipass, allowed_prefixes):
        result.loader_failures.append(
            f"sys._MEIPASS directory {result._meipass} is outside the bundle root."
        )
    if returncode not in (0, None):
        result.loader_failures.append(f"Process exited with non-zero return code {returncode}.")
    return result


def validate_bundle(args: argparse.Namespace) -> BundleReport:
    bundle_path = args.bundle.resolve()
    bundle_root = _infer_bundle_root(bundle_path, args.bundle_root)
    build_dir = _discover_build_dir(bundle_root, args.build_dir)

    static_result = perform_static_validation(
        bundle_path=bundle_path,
        bundle_root=bundle_root,
        build_dir=build_dir,
        required_paths=args.require,
    )

    runtime_result: Optional[RuntimeValidationResult] = None
    if not args.skip_runtime:
        executable = args.python_executable or (
            bundle_path if bundle_path.is_file() else bundle_root
        )
        if executable.is_dir():
            raise ValueError(
                "Runtime validation requires an executable. Use --python-executable to point to the binary inside the bundle."
            )
        runtime_result = run_runtime_validation(
            executable=executable,
            bundle_root=bundle_root,
            run_args=args.run_args or [],
            timeout=args.timeout,
            allow_system_paths=args.allow_system_path,
        )

    return BundleReport(static=static_result, runtime=runtime_result)


def _render_static_report(static: BundleValidationResult) -> str:
    lines: List[str] = []
    if static.toc_files_checked:
        lines.append("Checked TOC files:")
        for toc in static.toc_files_checked:
            lines.append(f"  - {toc}")
    if static.missing_files:
        lines.append("Missing bundled files:")
        for path in static.missing_files:
            lines.append(f"  - {path}")
    if static.warn_lines:
        lines.append("PyInstaller warnings:")
        for line in static.warn_lines:
            lines.append(f"  - {line}")
    if not lines:
        lines.append("Static bundle validation passed.")
    return "\n".join(lines)


def _render_runtime_report(runtime: RuntimeValidationResult) -> str:
    lines = [f"Executed: {' '.join(shlex.quote(part) for part in runtime.command)}"]
    if runtime.returncode is not None:
        lines.append(f"Exit code: {runtime.returncode}")
    else:
        lines.append("Exit code: unavailable")

    if runtime.loader_paths:
        lines.append("Loader paths observed:")
        for path in runtime.loader_paths:
            lines.append(f"  - {path}")
    if runtime.sys_path_entries:
        lines.append("sys.path entries:")
        for entry in runtime.sys_path_entries:
            lines.append(f"  - {entry}")

    if runtime.loader_failures:
        lines.append("Loader path violations:")
        for failure in runtime.loader_failures:
            lines.append(f"  - {failure}")
    if runtime.import_warnings:
        lines.append("Import warnings:")
        for warning in runtime.import_warnings:
            lines.append(f"  - {warning}")

    if runtime.stdout:
        lines.append("--- stdout ---")
        lines.append(runtime.stdout.rstrip())
    if runtime.stderr:
        lines.append("--- stderr ---")
        lines.append(runtime.stderr.rstrip())

    if runtime.is_successful:
        lines.append("Runtime bundle validation passed.")
    return "\n".join(lines)


def _emit_json_report(report: BundleReport) -> str:
    payload = {
        "static": {
            "missing_files": [str(path) for path in report.static.missing_files],
            "warn_lines": report.static.warn_lines,
            "toc_files_checked": [str(path) for path in report.static.toc_files_checked],
            "is_successful": report.static.is_successful,
        },
        "runtime": None,
        "is_successful": report.is_successful,
    }
    if report.runtime is not None:
        payload["runtime"] = {
            "command": list(report.runtime.command),
            "returncode": report.runtime.returncode,
            "stdout": report.runtime.stdout,
            "stderr": report.runtime.stderr,
            "loader_paths": [str(path) for path in report.runtime.loader_paths],
            "loader_failures": report.runtime.loader_failures,
            "import_warnings": report.runtime.import_warnings,
            "sys_path_entries": report.runtime.sys_path_entries,
            "meipass": str(report.runtime._meipass) if report.runtime._meipass else None,
            "is_successful": report.runtime.is_successful,
        }
    return json.dumps(payload, indent=2)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_arguments(argv)
    try:
        report = validate_bundle(args)
    except Exception as exc:  # pragma: no cover - defensive logging in CLI entrypoint
        print(f"Bundle validation failed: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(_emit_json_report(report))
    else:
        print(_render_static_report(report.static))
        if report.runtime is not None:
            print()
            print(_render_runtime_report(report.runtime))
    return 0 if report.is_successful else 1


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    sys.exit(main())
