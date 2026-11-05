#!/usr/bin/env python3
"""Fetch the pinned gvsbuild GTK 4 runtime for python.org Python on Windows.

gvsbuild (github.com/wingtk/gvsbuild) builds GTK 4, libadwaita,
GtkSourceView 5 and PyGObject with Visual Studio, matching python.org
(MSVC) Python. The release zip carries the DLLs, typelibs and data under
``bin``/``lib``/``share`` and PyGObject and pycairo wheels under ``wheels``.
This script downloads the pinned release, checks its SHA-256 (and fails on a
mismatch), and extracts it into ``vendor/gtk``.

It then adds gtk4-frdp (github.com/mfat/gtk4-frdp), built against that same
gvsbuild release, for RDP connections in a tab: its DLLs (with FreeRDP, WinPR
and OpenSSL) go into ``bin``, the GtkFrdp-4.0 typelib into
``lib/girepository-1.0`` and its licence files into
``share/licenses/gtk4-frdp``. Same pinning and checksum rule.

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

FRDP_VERSION = "0.2.3"
FRDP_ASSET = f"gtk4-frdp-{FRDP_VERSION}-gvsbuild-{VERSION}-x64.zip"
FRDP_URL = f"https://github.com/mfat/gtk4-frdp/releases/download/v{FRDP_VERSION}/{FRDP_ASSET}"
FRDP_SHA256 = "e35bf30e5eb718fa4923d2a7f91882a6a699807330ee457a6c41cc84259fd32e"
#: Files at the top of the gtk4-frdp zip that are not part of the prefix.
FRDP_LICENCE_FILES = ("COPYING", "NOTICE")

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


def _download(url: str, asset: str, sha256: str) -> Path:
    """Download ``asset`` into vendor/ once and return it, checksum verified."""
    archive_path = VENDOR / asset
    if not archive_path.exists():
        print(f"Downloading {url}")
        with tempfile.NamedTemporaryFile(dir=VENDOR, delete=False, suffix=".part") as part:
            temporary = Path(part.name)
        try:
            with urllib.request.urlopen(url, timeout=120) as response, open(temporary, "wb") as out:
                shutil.copyfileobj(response, out, 1 << 20)
            temporary.replace(archive_path)
        finally:
            temporary.unlink(missing_ok=True)
    actual = _sha256(archive_path)
    if actual != sha256:
        archive_path.unlink()
        raise SystemExit(f"SHA-256 mismatch for {asset}: expected {sha256}, got {actual}")
    return archive_path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dest", type=Path, default=VENDOR / "gtk")
    args = parser.parse_args(argv)

    VENDOR.mkdir(parents=True, exist_ok=True)
    archive_path = _download(URL, ASSET, SHA256)
    frdp_path = _download(FRDP_URL, FRDP_ASSET, FRDP_SHA256)

    if args.dest.exists():
        shutil.rmtree(args.dest)
    args.dest.mkdir(parents=True)
    with zipfile.ZipFile(archive_path) as archive:
        _safe_extract(archive, args.dest)
    with zipfile.ZipFile(frdp_path) as archive:
        _safe_extract(archive, args.dest)
    licences = args.dest / "share" / "licenses" / "gtk4-frdp"
    licences.mkdir(parents=True, exist_ok=True)
    for name in FRDP_LICENCE_FILES:
        (args.dest / name).replace(licences / name)
    (args.dest / "VERSION").write_text(
        f"gvsbuild {VERSION}\ngtk4-frdp {FRDP_VERSION}\n", encoding="utf-8")
    print(args.dest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
