#!/usr/bin/env python3
"""Build the OpenSSL legacy provider matching the pinned FreeRDP runtime.

Run in an x64 Visual Studio developer shell with native Windows Perl:
    python packaging/windows/openssl/build.py [--perl C:/Strawberry/perl/bin/perl.exe]

The provider is dynamically loaded, so PyInstaller cannot discover it from
the OpenSSL DLL's imports. Keep this version aligned with gtk4-frdp's OpenSSL.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import urllib.request

VERSION = "3.6.5"
SHA256 = "a2157c2830efdec3788939b00c9b0638306d3f0bbb76dc4832ee503bb397df98"
ROOT = Path(__file__).resolve().parents[3]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--perl", default=shutil.which("perl"))
    parser.add_argument("--make", default=shutil.which("jom") or shutil.which("nmake"))
    args = parser.parse_args()
    if not args.perl or not args.make:
        parser.error("requires native Windows Perl and an x64 Visual Studio developer shell")
    perl_os = subprocess.check_output([args.perl, "-e", "print $^O"], text=True)
    if perl_os != "MSWin32":
        parser.error("requires native Windows Perl (for example Strawberry Perl), not Git/MSYS Perl")
    work = ROOT / "build" / "openssl-provider"
    work.mkdir(parents=True, exist_ok=True)
    archive = work / f"openssl-{VERSION}.tar.gz"
    if not archive.exists():
        url = f"https://github.com/openssl/openssl/releases/download/openssl-{VERSION}/{archive.name}"
        temporary = archive.with_suffix(".part")
        try:
            urllib.request.urlretrieve(url, temporary)
            temporary.replace(archive)
        finally:
            temporary.unlink(missing_ok=True)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != SHA256:
        raise SystemExit(f"SHA-256 mismatch: {archive}")
    source = work / f"openssl-{VERSION}"
    if not source.exists():
        with tarfile.open(archive) as package:
            package.extractall(work, filter="data")
    build_environment = dict(os.environ, LC_ALL="C", LANG="C")
    build_environment["CL"] = build_environment.get("CL", "") + " /FS"
    subprocess.run([args.perl, "Configure", "VC-WIN64A", "shared", "no-asm", "no-tests"],
                   cwd=source, env=build_environment, check=True)
    make_arguments = ["-j", "8"] if Path(args.make).stem.lower() == "jom" else ["/nologo"]
    for target in ("build_generated", "providers\\legacy.dll"):
        subprocess.run([args.make, *make_arguments, target], cwd=source,
                       env=build_environment, check=True)
    destination = ROOT / "vendor" / "openssl" / "x64" / "ossl-modules"
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source / "providers" / "legacy.dll", destination / "legacy.dll")
    shutil.copy2(source / "LICENSE.txt", destination.parent / "LICENSE.txt")
    (destination.parent / "VERSION").write_text(VERSION + "\n", encoding="utf-8")
    subprocess.run([sys.executable, str(Path(__file__).with_name("check.py")),
                    "--runtime", str(ROOT / "vendor/gtk/bin"),
                    "--provider", str(destination)], check=True)
    print(destination)


if __name__ == "__main__":
    main()
