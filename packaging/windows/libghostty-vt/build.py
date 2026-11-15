#!/usr/bin/env python3
"""Build ``ghostty-vt.dll`` (libghostty-vt) for the built-in terminal on Windows.

libghostty-vt is Ghostty's terminal core, MIT-licensed, with a stable C ABI
since 1.0. ``sshpilot.ghostty_terminal_model`` loads it through ctypes.

The build needs no C compiler: it downloads the Zig toolchain the release
requires and builds the official release tarball offline against its
dependency tarball, as upstream's PACKAGING-LIBGHOSTTY.md describes. Every
download is pinned by SHA-256 (the release's SHA256SUMS is minisign-signed
by Ghostty; the hashes below were taken from it after checking that
signature). Zig cross-compiles, so the Windows DLL builds on Linux too.

    python packaging/windows/libghostty-vt/build.py            # Windows x64 DLL
    python packaging/windows/libghostty-vt/build.py --target native   # this host (tests)

Output: ``vendor/libghostty-vt/<arch>/``, where the model finds it, with the
library's LICENSE and third-party manifest beside it. Downloads are kept in
``vendor/.cache/libghostty-vt`` and reused.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

VERSION = "1.0.0"
RELEASE = f"https://release.files.ghostty.org/libghostty-vt/{VERSION}"
SOURCE = (f"{RELEASE}/libghostty-vt-{VERSION}.tar.gz",
          "4a6ef758be721923e1969e7b81b443dcc92d8dd3fb5a39600c03a8817a753676")
DEPS = (f"{RELEASE}/libghostty-vt-{VERSION}-deps.tar.gz",
        "4d5ea5e448e4c7ea8fb55ccbaa8668f2a97d44f438cee831e2b7ecd162d85c29")

ZIG_VERSION = "0.17.1"  # libghostty-vt 1.0.0 needs Zig 0.17.x
ZIG = {
    "x86_64-windows": ("zip", "b03f477a1943673e02aca0f570e8030dffd536ce1f45cbc4d3326ec31159562e"),
    "aarch64-windows": ("zip", "4789895bdb1e312a2ea73b2a7c0a0cc8d2ab62ba6976e834a7c54cf443377c15"),
    "x86_64-linux": ("tar.xz", "3581863e3cbcfd0fce0efc682ce7e9e93da96668a977791d356a26d2a7e9b17a"),
    "aarch64-linux": ("tar.xz", "a1546d5646eb184437918d6635ca6b762a0e889eac09cef720ee7a1ed3fe7795"),
}

#: --arch -> Zig target for the Windows DLL.
WINDOWS_TARGETS = {"x64": "x86_64-windows-gnu", "arm64": "aarch64-windows-gnu"}

ROOT = Path(__file__).resolve().parents[3]


def _run(command, **kwargs) -> None:
    print("+", " ".join(str(part) for part in command), flush=True)
    subprocess.run(command, check=True, **kwargs)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, sha256: str, cache: Path) -> Path:
    """``url`` in ``cache``, downloaded once and checked against ``sha256``."""

    cache.mkdir(parents=True, exist_ok=True)
    path = cache / url.rsplit("/", 1)[1]
    if not path.exists() or _sha256(path) != sha256:
        print(f"downloading {url}", flush=True)
        partial = path.with_suffix(path.suffix + ".part")
        # release.files.ghostty.org answers 403 to Python's default User-Agent.
        request = urllib.request.Request(url, headers={"User-Agent": "sshpilot-build"})
        with urllib.request.urlopen(request, timeout=120) as response, partial.open("wb") as out:
            shutil.copyfileobj(response, out)
        actual = _sha256(partial)
        if actual != sha256:
            partial.unlink()
            raise SystemExit(f"{url}: SHA-256 {actual}, expected {sha256}")
        partial.replace(path)
    return path


def unpack(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    if archive.suffix == ".zip":
        root = destination.resolve()
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.namelist():
                if not (destination / member).resolve().is_relative_to(root):
                    raise SystemExit(f"{archive.name}: unsafe path {member}")
            bundle.extractall(destination)
        return
    with tarfile.open(archive) as bundle:
        bundle.extractall(destination, filter="data")


def zig_host() -> str:
    machine = platform.machine().lower()
    arch = {"amd64": "x86_64", "x86_64": "x86_64", "arm64": "aarch64", "aarch64": "aarch64"}.get(machine)
    system = {"Windows": "windows", "Linux": "linux"}.get(platform.system())
    host = f"{arch}-{system}"
    if host not in ZIG:
        raise SystemExit(f"no pinned Zig {ZIG_VERSION} download for {host}; add it to ZIG")
    return host


def fetch_zig(cache: Path, work: Path) -> Path:
    host = zig_host()
    kind, sha256 = ZIG[host]
    name = f"zig-{host}-{ZIG_VERSION}"
    archive = download(f"https://ziglang.org/download/{ZIG_VERSION}/{name}.{kind}", sha256, cache)
    unpack(archive, work)
    executable = work / name / ("zig.exe" if host.endswith("windows") else "zig")
    if not executable.exists():
        raise SystemExit(f"{executable} missing after unpacking {archive.name}")
    return executable


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--arch", default="x64", choices=sorted(WINDOWS_TARGETS))
    parser.add_argument("--target", default=None,
                        help="Zig target triple, or 'native' for this host (default: the Windows DLL)")
    parser.add_argument("--dest", type=Path, default=None, help="default: vendor/libghostty-vt/<arch>")
    parser.add_argument("--cache", type=Path, default=ROOT / "vendor" / ".cache" / "libghostty-vt")
    args = parser.parse_args(argv)
    target = args.target or WINDOWS_TARGETS[args.arch]
    dest = args.dest or ROOT / "vendor" / "libghostty-vt" / args.arch
    dest.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="libghostty-vt-build-") as work:
        work_path = Path(work)
        zig = fetch_zig(args.cache, work_path / "zig")
        unpack(download(*SOURCE, args.cache), work_path)
        unpack(download(*DEPS, args.cache), work_path)
        source = work_path / f"libghostty-vt-{VERSION}"
        if (source / "LIB_VERSION").read_text(encoding="utf-8").strip() != VERSION:
            raise SystemExit("unexpected LIB_VERSION in the source tarball")
        prefix = work_path / "out"
        # Upstream: the global cache must be new and empty with Zig 0.17, or a
        # cached package archive can crash the build ("reached unreachable").
        environment = dict(os.environ, ZIG_GLOBAL_CACHE_DIR=str(work_path / "zig-cache"))
        command = [
            str(zig), "build", "--prefix", str(prefix),
            "--system", str(work_path / f"libghostty-vt-{VERSION}-deps"),
            "-Doptimize=ReleaseFast", "-Dcpu=baseline", "-Dstrip=true",
        ]
        if target != "native":
            command.append(f"-Dtarget={target}")
        _run(command, cwd=source, env=environment)

        if target == "native" and platform.system() != "Windows":
            built = prefix / "lib" / f"libghostty-vt.so.{VERSION}"
            name = "libghostty-vt.so.1"
        else:
            built = prefix / "bin" / "ghostty-vt.dll"
            name = "ghostty-vt.dll"
        shutil.copy2(built, dest / name)
        shutil.copy2(source / "LICENSE", dest / "LICENSE")
        shutil.copy2(source / "deps" / "libghostty-vt-third-party.json", dest / "third-party.json")
        (dest / "SOURCE").write_text(
            f"libghostty-vt {VERSION} ({SOURCE[0]}, sha256 {SOURCE[1]}), "
            f"built with Zig {ZIG_VERSION} for {target}\n",
            encoding="utf-8",
        )
    print(dest / name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
