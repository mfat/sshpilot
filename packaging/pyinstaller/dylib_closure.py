"""Close the dylib dependency graph of the macOS .app bundle.

PyInstaller collects the libraries it can resolve and rewrites their
dependency references to ``@rpath/…`` pointing at ``Contents/Frameworks``.
A dependency it fails to resolve at analysis time is still rewritten, but
never copied — so the bundle builds, signs and ships, and only blows up on
the user's machine:

    Library not loaded: @rpath/libappstream.5.dylib
      Reason: tried: '…/Contents/Frameworks/libappstream.5.dylib' (no such file)

That one (libadwaita → libappstream, GH #1266) took down every GTK import:
``Adw`` never loaded, so ``Adw.Application`` was ``void`` and the frozen app
died with "could not create new GType … (subclass of void)".

This module walks every Mach-O file in the finished bundle, resolves each of
its dependency references against ``Contents/Frameworks``, copies in whatever
Homebrew library is missing (recursively — a copied library's own
dependencies are walked too), and rewrites absolute Homebrew references to
``@rpath/…``. Anything still unresolved fails the build, which is the point:
a missing dylib must never reach a DMG again.

macOS system libraries (``/usr/lib``, ``/System/…``) are left alone — dyld
resolves those from the shared cache on the target machine.
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple


# dyld resolves these from the shared cache; they are never bundled.
SYSTEM_PREFIXES = ("/usr/lib/", "/System/")

# Extensions of files worth asking otool about.
MACHO_SUFFIXES = (".dylib", ".so")

# ``	/opt/homebrew/lib/libfoo.1.dylib (compatibility version …)``
_OTOOL_DEP_RE = re.compile(r"^\s+(\S+)\s+\(compatibility version")


def is_system_reference(ref: str) -> bool:
    """True for a dependency dyld serves from the OS, not from the bundle."""
    return ref.startswith(SYSTEM_PREFIXES)


def parse_otool_dependencies(output: str) -> List[str]:
    """Extract dependency references from ``otool -L`` output.

    The first line names the inspected file and is skipped; every remaining
    indented line is a load-command reference (including the library's own
    ``LC_ID_DYLIB`` install name, which resolves to itself and is harmless).
    """
    deps: List[str] = []
    for line in output.splitlines():
        match = _OTOOL_DEP_RE.match(line)
        if match:
            deps.append(match.group(1))
    return deps


def iter_macho_files(root: Path) -> List[Path]:
    """List candidate Mach-O files under *root*, skipping symlinks."""
    if not root.is_dir():
        return []
    found = [
        path
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and not path.is_symlink()
        and path.name.endswith(MACHO_SUFFIXES)
    ]
    return found


def homebrew_search_roots(prefix: str) -> List[str]:
    """Directories to look in for a Homebrew library, best match first.

    ``<prefix>/lib`` holds the linked symlinks; ``opt``/``Cellar`` cover
    formulae that are keg-only or simply not linked on the build runner.
    """
    return [
        os.path.join(prefix, "lib"),
        os.path.join(prefix, "opt", "*", "lib"),
        os.path.join(prefix, "Cellar", "*", "*", "lib"),
    ]


def find_library(name: str, search_roots: Sequence[str]) -> Optional[str]:
    """Locate *name* in *search_roots* (each may be a glob pattern)."""
    for root in search_roots:
        for candidate in sorted(glob.glob(os.path.join(root, name))):
            if os.path.isfile(candidate):
                return candidate
    return None


@dataclass
class ClosureReport:
    """What the closure pass did, for build logs and assertions."""

    copied: List[str] = field(default_factory=list)
    rewritten: List[Tuple[str, str]] = field(default_factory=list)
    unresolved: List[Tuple[str, str]] = field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        return not self.unresolved


class MachOTool:
    """The macOS binaries this pass drives; swapped out in tests."""

    def dependencies(self, path: Path) -> List[str]:
        out = subprocess.run(
            ["otool", "-L", str(path)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        return parse_otool_dependencies(out)

    def set_id(self, path: Path, new_id: str) -> None:
        subprocess.run(
            ["install_name_tool", "-id", new_id, str(path)],
            check=True,
            capture_output=True,
        )

    def change_reference(self, path: Path, old: str, new: str) -> None:
        subprocess.run(
            ["install_name_tool", "-change", old, new, str(path)],
            check=True,
            capture_output=True,
        )

    def add_rpath(self, path: Path, rpath: str) -> None:
        # Already-present rpaths make this fail; that is not an error.
        subprocess.run(
            ["install_name_tool", "-add_rpath", rpath, str(path)],
            check=False,
            capture_output=True,
        )


LOADER_PREFIXES = ("@rpath/", "@loader_path/", "@executable_path/")


def reference_tail(ref: str) -> str:
    """The path part of a reference, with any ``@…/`` prefix stripped."""
    for prefix in LOADER_PREFIXES:
        if ref.startswith(prefix):
            return ref[len(prefix):]
    return ref


def is_leaf_reference(ref: str) -> bool:
    """True for a plain library name such as ``@rpath/libappstream.5.dylib``.

    Multi-component references (``@rpath/Python.framework/Versions/3.13/
    Python``) name something structural that must not be "fixed" by dropping
    a single file at Frameworks root.
    """
    tail = reference_tail(ref)
    return bool(tail) and "/" not in tail and not os.path.isabs(ref)


def bundle_names(frameworks_dir: Path) -> Dict[str, Path]:
    """Index every file in the bundle by basename.

    A dependency whose name appears nowhere in here cannot resolve on a
    machine without Homebrew, whatever rpath dyld walks.
    """
    index: Dict[str, Path] = {}
    for path in frameworks_dir.rglob("*"):
        index.setdefault(path.name, path)
    return index


def loader_path_reference(binary: Path, frameworks_dir: Path, name: str) -> str:
    """A ``@loader_path``-relative reference from *binary* to *name*.

    Preferred over ``@rpath/…`` when rewriting: it resolves on its own instead
    of depending on the referring binary carrying the right ``LC_RPATH``,
    which nested files (``Frameworks/gi/_gi_cairo.so``) may not.
    """
    rel = os.path.relpath(frameworks_dir, binary.parent)
    return f"@loader_path/{name}" if rel == "." else f"@loader_path/{rel}/{name}"


def close_dylib_graph(
    frameworks_dir: Path,
    search_roots: Sequence[str],
    tool: Optional[MachOTool] = None,
    copy: Optional[Callable[[str, Path], None]] = None,
) -> ClosureReport:
    """Copy in every missing Homebrew dylib and rewrite stale references.

    Returns a :class:`ClosureReport`; ``unresolved`` being non-empty means the
    bundle would crash at launch and the build must fail.
    """
    tool = tool or MachOTool()
    if copy is None:
        def copy(src: str, dest: Path) -> None:
            # follow_symlinks: Homebrew's lib/ entries point into the Cellar.
            shutil.copy2(src, dest, follow_symlinks=True)

    report = ClosureReport()
    # Anything already in the bundle, by basename — the root-level entries are
    # where @rpath/@loader_path resolve, the nested ones (Python.framework/…)
    # are reachable through the references PyInstaller wrote for them.
    present = bundle_names(frameworks_dir)
    at_root = {path.name for path in frameworks_dir.glob("*")}

    queue: List[Path] = iter_macho_files(frameworks_dir)
    seen: set = set()

    while queue:
        binary = queue.pop(0)
        key = str(binary)
        if key in seen:
            continue
        seen.add(key)

        try:
            deps = tool.dependencies(binary)
        except (subprocess.CalledProcessError, OSError):
            # Not a Mach-O file (a stray .so data blob, say) — nothing to do.
            continue

        for ref in deps:
            if is_system_reference(ref):
                continue
            name = os.path.basename(ref)
            if not name or name == binary.name:
                continue

            was_missing = name not in present
            if was_missing:
                # A structural reference (into Python.framework, say) names
                # something the bundle should already contain; dropping a lone
                # file at Frameworks root cannot fix it, so report and move on.
                copyable = is_leaf_reference(ref) or (
                    os.path.isabs(ref) and name.endswith(MACHO_SUFFIXES)
                )
                src = None
                if copyable:
                    # An absolute reference already points at the library on
                    # the build machine; otherwise go looking in Homebrew.
                    if os.path.isabs(ref) and os.path.isfile(ref):
                        src = ref
                    else:
                        src = find_library(name, search_roots)
                if src is None:
                    report.unresolved.append((binary.name, ref))
                    continue
                dest = frameworks_dir / name
                copy(src, dest)
                try:
                    dest.chmod(dest.stat().st_mode | 0o200)
                except OSError:
                    pass
                tool.set_id(dest, f"@rpath/{name}")
                # Its own @rpath deps resolve next to it, at Frameworks root.
                tool.add_rpath(dest, "@loader_path")
                present[name] = dest
                at_root.add(name)
                report.copied.append(name)
                queue.append(dest)

            # Rewrite an absolute build-machine path, which must never survive
            # into the bundle, and any reference we just satisfied by copying —
            # the latter was broken until now, so don't trust its @rpath to
            # find the new file. Only safe when the library really sits at
            # Frameworks root; a nested one keeps the reference PyInstaller
            # already wrote for it.
            if (os.path.isabs(ref) or was_missing) and name in at_root:
                target = loader_path_reference(binary, frameworks_dir, name)
                if target != ref:
                    tool.change_reference(binary, ref, target)
                    report.rewritten.append((binary.name, ref))

    return report


def format_report(report: ClosureReport) -> str:
    """Human-readable summary for the build log."""
    lines = []
    if report.copied:
        lines.append(f"Copied {len(report.copied)} missing dylib(s):")
        lines.extend(f"  + {name}" for name in report.copied)
    if report.rewritten:
        lines.append(f"Rewrote {len(report.rewritten)} absolute reference(s).")
    if report.unresolved:
        lines.append("UNRESOLVED dependencies (the bundle would crash at launch):")
        lines.extend(f"  ! {owner}: {ref}" for owner, ref in report.unresolved)
    if not lines:
        lines.append("Dependency graph already closed; nothing to do.")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse
    import platform

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", help="path to the built SSHPilot.app")
    parser.add_argument(
        "--homebrew",
        default=None,
        help="Homebrew prefix (default: /opt/homebrew on arm64, /usr/local on Intel)",
    )
    args = parser.parse_args(argv)

    prefix = args.homebrew
    if prefix is None:
        prefix = "/opt/homebrew" if platform.machine() == "arm64" else "/usr/local"

    frameworks = Path(args.app) / "Contents" / "Frameworks"
    if not frameworks.is_dir():
        print(f"❌ {frameworks} not found — is {args.app} a built bundle?")
        return 1

    report = close_dylib_graph(frameworks, homebrew_search_roots(prefix))
    print(format_report(report))
    if not report.is_complete:
        return 1
    print("✅ Every bundled library resolves inside the app")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
