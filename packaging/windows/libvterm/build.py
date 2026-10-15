#!/usr/bin/env python3
"""Build a patched ``libvterm-0.dll`` for the built-in terminal on Windows.

Source: the ``github.com/neovim/libvterm`` mirror of Paul Evans' libvterm,
pinned by commit. It keeps upstream's ``VTermScreenCell`` layout
(``uint32_t chars[6]``) that ``sshpilot.vterm_terminal_model`` reads through
ctypes. Neovim's in-tree ``src/nvim/vterm`` does not: its cells hold an
index into Neovim's own string table.

Patch (applied as exact text replacements that fail loudly if the source
moves):

* ``CSI_ARGS_MAX`` 16 -> 32, and the parser stops advancing ``argi`` at the
  last slot. Unpatched, a CSI with 16+ separators writes past ``args[]``
  (a remotely triggerable crash; Neovim's fork raised the size but kept
  the unbounded index).
* An exported ``sshpilot_vterm_csi_args_bounded()`` returning the bound, so
  the model can tell a patched build from a distro DLL and skip its Python
  guard.

    python packaging/windows/libvterm/build.py               # gcc (MSYS2 UCRT64)
    python packaging/windows/libvterm/build.py --compiler msvc  # cl (VS dev shell)

The DLL lands in ``vendor/libvterm/<arch>/``, where the model finds it.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPOSITORY = "https://github.com/neovim/libvterm"
COMMIT = "934bc2fbf21800ac3458a499df8820ca5fb45fd3"  # 2025-11-20, VERSION 0.3.3
DLL_NAME = "libvterm-0.dll"

ROOT = Path(__file__).resolve().parents[3]

#: (file, old text, new text): each old text must occur exactly once.
PATCHES = [
    (
        "src/vterm_internal.h",
        "#define CSI_ARGS_MAX 16\n",
        "#define CSI_ARGS_MAX 32\n",
    ),
    (
        "src/parser.c",
        """      if(c == ';') {
        vt->parser.v.csi.argi++;
        vt->parser.v.csi.args[vt->parser.v.csi.argi] = CSI_ARG_MISSING;
        break;
      }
""",
        """      if(c == ';') {
        /* sshPilot: never index past args[]; extra arguments are dropped. */
        if(vt->parser.v.csi.argi < CSI_ARGS_MAX - 1) {
          vt->parser.v.csi.argi++;
          vt->parser.v.csi.args[vt->parser.v.csi.argi] = CSI_ARG_MISSING;
        }
        break;
      }
""",
    ),
    (
        "src/vterm.c",
        '#include "vterm_internal.h"\n',
        '''#include "vterm_internal.h"

/* sshPilot: marks a build whose CSI parser bounds its argument index. */
#ifdef _WIN32
__declspec(dllexport)
#endif
int sshpilot_vterm_csi_args_bounded(void)
{
  return CSI_ARGS_MAX;
}
''',
    ),
]


def _run(command, **kwargs) -> None:
    print("+", " ".join(str(part) for part in command), flush=True)
    subprocess.run(command, check=True, **kwargs)


def fetch(destination: Path) -> Path:
    source = destination / "libvterm"
    _run(["git", "clone", "--quiet", "--no-checkout", REPOSITORY, str(source)])
    _run(["git", "-C", str(source), "checkout", "--quiet", COMMIT])
    head = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    if head != COMMIT:
        raise SystemExit(f"checked out {head}, expected {COMMIT}")
    return source


def patch(source: Path) -> None:
    for relative, old, new in PATCHES:
        path = source / relative
        text = path.read_text(encoding="utf-8")
        if text.count(old) != 1:
            raise SystemExit(f"{relative}: patch context not found exactly once; update PATCHES")
        path.write_text(text.replace(old, new), encoding="utf-8", newline="\n")


def exported_functions(source: Path) -> list[str]:
    header = (source / "include" / "vterm.h").read_text(encoding="utf-8")
    names = re.findall(r"^[A-Za-z_][\w \*]*?\b(vterm_\w+)\s*\(", header, re.M)
    return sorted(set(names)) + ["sshpilot_vterm_csi_args_bounded"]


def build_gcc(source: Path, output: Path, compiler: str) -> None:
    sources = sorted(str(path) for path in (source / "src").glob("*.c"))
    _run([
        compiler, "-std=c99", "-O2", "-DNDEBUG", "-shared", "-static-libgcc",
        # One __declspec(dllexport) turns off MinGW's automatic export of the rest.
        "-Wl,--export-all-symbols",
        "-I", str(source / "include"), "-I", str(source / "src"),
        *sources, "-o", str(output),
    ])


def build_msvc(source: Path, output: Path) -> None:
    definition = output.with_suffix(".def")
    definition.write_text(
        "LIBRARY " + output.stem + "\nEXPORTS\n"
        + "".join(f"    {name}\n" for name in exported_functions(source)),
        encoding="ascii",
    )
    sources = sorted(str(path) for path in (source / "src").glob("*.c"))
    _run([
        "cl", "/nologo", "/O2", "/DNDEBUG", "/LD", "/utf-8",
        "/I", str(source / "include"), "/I", str(source / "src"),
        *sources, f"/Fe{output}", f"/Fo{output.parent}\\", "/link", f"/DEF:{definition}",
    ])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--compiler", choices=["gcc", "msvc"], default="gcc")
    parser.add_argument("--cc", default="gcc", help="gcc executable (default: gcc)")
    parser.add_argument("--arch", default="x64")
    parser.add_argument("--source", type=Path, help="an existing checkout of COMMIT (skips the clone)")
    parser.add_argument("--dest", type=Path, default=None, help="default: vendor/libvterm/<arch>")
    args = parser.parse_args(argv)
    dest = args.dest or ROOT / "vendor" / "libvterm" / args.arch
    dest.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="libvterm-build-") as work:
        work_path = Path(work)
        if args.source:
            source = work_path / "libvterm"
            shutil.copytree(args.source, source, ignore=shutil.ignore_patterns(".git"))
        else:
            source = fetch(work_path)
        patch(source)
        build_dir = work_path / "out"
        build_dir.mkdir()
        output = build_dir / DLL_NAME
        if args.compiler == "msvc":
            build_msvc(source, output)
        else:
            build_gcc(source, output, args.cc)
        shutil.copy2(output, dest / DLL_NAME)
        (dest / "SOURCE").write_text(f"{REPOSITORY} {COMMIT} (+ sshPilot CSI bound)\n", encoding="utf-8")
    print(dest / DLL_NAME)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
