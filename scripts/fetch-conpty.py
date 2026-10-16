#!/usr/bin/env python3
"""Fetch Microsoft's ConPTY (conpty.dll + OpenConsole.exe) for Windows builds.

Downloads the pinned ``Microsoft.Windows.Console.ConPTY`` NuGet package
(MIT, published from microsoft/terminal) from nuget.org, checks its SHA-256,
and extracts the two files for one architecture into ``vendor/conpty/<arch>``.
A source checkout finds them there; the PyInstaller spec bundles them from
there. ``conpty.dll`` only finds ``OpenConsole.exe`` beside itself, and both
must come from the same release, so they are always extracted together.

    python scripts/fetch-conpty.py              # x64
    python scripts/fetch-conpty.py --arch arm64
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

PACKAGE = "Microsoft.Windows.Console.ConPTY"
VERSION = "1.25.260930003"
#: Matches the SHA-512 nuget.org's catalog publishes for this version.
SHA256 = "02b07b349af66d801159bdf9e440d4a1ce78bb951f37fc8609731665afdae7ee"
URL = f"https://api.nuget.org/v3-flatcontainer/{PACKAGE.lower()}/{VERSION}/{PACKAGE.lower()}.{VERSION}.nupkg"

#: Members to extract per architecture: (path in the package, file name).
MEMBERS = {
    "x64": ("runtimes/win-x64/native/conpty.dll", "build/native/runtimes/x64/OpenConsole.exe"),
    "arm64": ("runtimes/win-arm64/native/conpty.dll", "build/native/runtimes/arm64/OpenConsole.exe"),
    "x86": ("runtimes/win-x86/native/conpty.dll", "build/native/runtimes/x86/OpenConsole.exe"),
}

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "vendor" / "conpty"


def _download(destination: Path) -> None:
    with urllib.request.urlopen(URL, timeout=60) as response, open(destination, "wb") as out:
        shutil.copyfileobj(response, out)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _check_signature(path: Path) -> None:
    """Require a valid Microsoft Authenticode signature, where PowerShell can check."""

    if sys.platform != "win32":
        return
    script = (
        "$s = Get-AuthenticodeSignature -LiteralPath $env:SSHPILOT_SIGNED_FILE; "
        "\"$($s.Status)|$($s.SignerCertificate.Subject)\""
    )
    # Run from PowerShell 7 (as CI steps are), Windows PowerShell inherits its
    # PSModulePath and then fails to load the 7.x Microsoft.PowerShell.Security
    # module that provides Get-AuthenticodeSignature. Without the variable it
    # falls back to its own module paths.
    env = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
    env["SSHPILOT_SIGNED_FILE"] = str(path)
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=60, env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"warning: could not check the signature of {path.name}: {exc}", file=sys.stderr)
        return
    status, _, subject = result.stdout.strip().partition("|")
    if status != "Valid" or "O=Microsoft Corporation" not in subject:
        raise SystemExit(f"{path.name}: bad signature ({status or result.stderr.strip()}, {subject})")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--arch", choices=sorted(MEMBERS), default="x64")
    parser.add_argument("--dest", type=Path, default=None, help="default: vendor/conpty/<arch>")
    args = parser.parse_args(argv)
    dest = args.dest or VENDOR / args.arch

    VENDOR.mkdir(parents=True, exist_ok=True)
    package = VENDOR / f"{PACKAGE}.{VERSION}.nupkg"
    if not package.exists():
        print(f"Downloading {URL}")
        with tempfile.NamedTemporaryFile(dir=VENDOR, delete=False, suffix=".part") as part:
            temporary = Path(part.name)
        try:
            _download(temporary)
            temporary.replace(package)
        finally:
            temporary.unlink(missing_ok=True)
    actual = _sha256(package)
    if actual != SHA256:
        package.unlink()
        raise SystemExit(f"SHA-256 mismatch for {package.name}: expected {SHA256}, got {actual}")

    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(package) as archive:
        for member in MEMBERS[args.arch]:
            target = dest / os.path.basename(member)
            with archive.open(member) as source, open(target, "wb") as out:
                shutil.copyfileobj(source, out)
            _check_signature(target)
            print(f"{target.relative_to(ROOT) if target.is_relative_to(ROOT) else target}")
    (dest / "VERSION").write_text(f"{PACKAGE} {VERSION}\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
