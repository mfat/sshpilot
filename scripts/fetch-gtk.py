#!/usr/bin/env python3
"""Fetch the pinned gvsbuild GTK 4 runtime for python.org Python on Windows.

gvsbuild (github.com/wingtk/gvsbuild) builds GTK 4, libadwaita,
GtkSourceView 5 and PyGObject with Visual Studio, matching python.org
(MSVC) Python. The release zip carries the DLLs, typelibs and data under
``bin``/``lib``/``share`` and PyGObject and pycairo wheels under ``wheels``.
This script downloads the pinned release, checks its SHA-256 (and fails on a
mismatch), and extracts it into ``vendor/gtk``.

    py -3.14 scripts/fetch-gtk.py
    py -3.14 -m pip install vendor/gtk/wheels/PyGObject-*.whl vendor/gtk/wheels/pycairo-*.whl

Then put ``vendor\\gtk\\bin`` on PATH (``scripts/run-windows.ps1`` does).
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

VERSION = "2026.8.0"
ASSET = f"GTK4_Gvsbuild_{VERSION}_x64.zip"
URL = f"https://github.com/wingtk/gvsbuild/releases/download/{VERSION}/{ASSET}"
#: The digest GitHub publishes for this release asset.
SHA256 = "1f95a92d037f5292da05e6ab1037032ff21ddb7b20d4ac8e83e3674c864c07b0"

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "vendor"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_extract(archive: zipfile.ZipFile, destination: Path) -> None:
    root = destination.resolve()
    for member in archive.infolist():
        name = PurePosixPath(member.filename.replace("\\", "/"))
        if name.is_absolute() or ".." in name.parts:
            raise SystemExit(f"refusing unsafe path in archive: {member.filename}")
        target = (destination / Path(*name.parts)).resolve()
        if root not in target.parents and target != root:
            raise SystemExit(f"refusing path outside the destination: {member.filename}")
        if member.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(member) as source, open(target, "wb") as out:
            shutil.copyfileobj(source, out)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dest", type=Path, default=VENDOR / "gtk")
    args = parser.parse_args(argv)

    VENDOR.mkdir(parents=True, exist_ok=True)
    archive_path = VENDOR / ASSET
    if not archive_path.exists():
        print(f"Downloading {URL}")
        with tempfile.NamedTemporaryFile(dir=VENDOR, delete=False, suffix=".part") as part:
            temporary = Path(part.name)
        try:
            with urllib.request.urlopen(URL, timeout=120) as response, open(temporary, "wb") as out:
                shutil.copyfileobj(response, out, 1 << 20)
            temporary.replace(archive_path)
        finally:
            temporary.unlink(missing_ok=True)
    actual = _sha256(archive_path)
    if actual != SHA256:
        archive_path.unlink()
        raise SystemExit(f"SHA-256 mismatch for {ASSET}: expected {SHA256}, got {actual}")

    if args.dest.exists():
        shutil.rmtree(args.dest)
    args.dest.mkdir(parents=True)
    with zipfile.ZipFile(archive_path) as archive:
        _safe_extract(archive, args.dest)
    (args.dest / "VERSION").write_text(f"gvsbuild {VERSION}\n", encoding="utf-8")
    print(args.dest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
