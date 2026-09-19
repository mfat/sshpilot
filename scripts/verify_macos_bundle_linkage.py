#!/usr/bin/env python3
"""Verify every bundled macOS library resolves inside the app bundle.

PyInstaller copies the libraries it can see, so a dependency that is missing
from the build host is simply absent from the bundle. Nothing fails until the
app runs on a user's Mac and dyld cannot find it -- GH #1266, where a pinned
libadwaita 1.9.3 needed ``libappstream.5.dylib`` and the app died at startup
with "could not create new GType". The daemon smoke test never noticed,
because ``--daemon`` exits before libadwaita is loaded.

This reads the Mach-O load commands directly (no ``otool``), so it also runs
on Linux against an unpacked bundle.
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

MH_MAGIC_64 = 0xFEEDFACF
MH_CIGAM_64 = 0xCFFAEDFE
FAT_MAGIC = 0xCAFEBABE
FAT_MAGIC_64 = 0xCAFEBABF

LC_REQ_DYLD = 0x80000000
LC_LOAD_DYLIB = 0x0C
LC_LOAD_WEAK_DYLIB = 0x18 | LC_REQ_DYLD
LC_REEXPORT_DYLIB = 0x1F | LC_REQ_DYLD
LC_RPATH = 0x1C | LC_REQ_DYLD
_DYLIB_COMMANDS = {LC_LOAD_DYLIB, LC_LOAD_WEAK_DYLIB, LC_REEXPORT_DYLIB}

# Libraries outside the bundle that macOS itself provides. They are absent from
# a Linux checkout too, so never treat them as missing.
_SYSTEM_PREFIXES = ("/usr/lib/", "/System/")


def _iter_macho_slices(data: bytes):
    """Yield (offset, little_endian) for each 64-bit Mach-O image in *data*."""
    if len(data) < 8:
        return
    magic = struct.unpack_from(">I", data, 0)[0]
    if magic in (FAT_MAGIC, FAT_MAGIC_64):
        count = struct.unpack_from(">I", data, 4)[0]
        entry_size = 20 if magic == FAT_MAGIC else 32
        for index in range(count):
            base = 8 + index * entry_size
            if magic == FAT_MAGIC:
                offset = struct.unpack_from(">I", data, base + 8)[0]
            else:
                offset = struct.unpack_from(">Q", data, base + 8)[0]
            if offset + 4 <= len(data):
                slice_magic = struct.unpack_from("<I", data, offset)[0]
                if slice_magic in (MH_MAGIC_64, MH_CIGAM_64):
                    yield offset, slice_magic == MH_MAGIC_64
        return
    slice_magic = struct.unpack_from("<I", data, 0)[0]
    if slice_magic in (MH_MAGIC_64, MH_CIGAM_64):
        yield 0, slice_magic == MH_MAGIC_64


def _read_load_commands(path: Path):
    """Return (dylib deps, rpaths) named by every slice of *path*."""
    data = path.read_bytes()
    deps: list[str] = []
    rpaths: list[str] = []
    for offset, little in _iter_macho_slices(data):
        endian = "<" if little else ">"
        ncmds = struct.unpack_from(endian + "I", data, offset + 16)[0]
        cursor = offset + 32  # mach_header_64 is 32 bytes
        for _ in range(ncmds):
            if cursor + 8 > len(data):
                break
            cmd, cmdsize = struct.unpack_from(endian + "II", data, cursor)
            if cmdsize == 0:
                break
            if cmd in _DYLIB_COMMANDS or cmd == LC_RPATH:
                str_offset = struct.unpack_from(endian + "I", data, cursor + 8)[0]
                if cmd == LC_RPATH:
                    start = cursor + str_offset
                else:
                    start = cursor + str_offset
                raw = data[start:cursor + cmdsize].split(b"\x00", 1)[0]
                value = raw.decode("utf-8", "replace")
                (rpaths if cmd == LC_RPATH else deps).append(value)
            cursor += cmdsize
    return deps, rpaths


def _resolve(dep: str, image: Path, bundle: Path, rpaths, names) -> bool:
    """Whether *dep*, as named by *image*, resolves to a file that exists."""
    if dep.startswith(_SYSTEM_PREFIXES):
        return True

    def _exists(candidate: str) -> bool:
        text = candidate.replace("@loader_path", str(image.parent))
        text = text.replace("@executable_path", str(bundle / "Contents" / "MacOS"))
        if Path(text).exists():
            return True
        # dyld would not do this, but PyInstaller flattens libraries into
        # Frameworks/ and rewrites load paths, so a basename match there is
        # the layout actually shipped.
        return Path(text).name in names

    if dep.startswith("@rpath/"):
        tail = dep[len("@rpath/"):]
        return any(_exists(f"{rpath}/{tail}") for rpath in rpaths) or tail in names
    return _exists(dep)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle", type=Path, help="path to SSHPilot.app")
    args = parser.parse_args()
    bundle = args.bundle.resolve()
    if not bundle.is_dir():
        raise SystemExit(f"bundle not found: {bundle}")

    images = [
        path
        for path in bundle.rglob("*")
        if path.is_file() and not path.is_symlink()
        and (path.suffix in {".dylib", ".so"} or path.parent.name == "MacOS")
    ]
    names = {path.name for path in images}
    print(f"=== LINKAGE: scanning {len(images)} bundled images ===")

    missing: list[str] = []
    for image in sorted(images):
        try:
            deps, rpaths = _read_load_commands(image)
        except Exception as exc:  # not a Mach-O file, or truncated
            print(f"  skipped {image.relative_to(bundle)}: {exc}")
            continue
        for dep in deps:
            if not _resolve(dep, image, bundle, rpaths, names):
                missing.append(f"{image.relative_to(bundle)} -> {dep}")

    if missing:
        print(f"=== LINKAGE: {len(missing)} unresolved dependencies ===")
        for entry in sorted(set(missing)):
            print(f"  MISSING {entry}")
        return 1
    print("=== LINKAGE: all bundled libraries resolve ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
