"""Every build of the gtkghostty terminal uses the same libghostty-vt.

Some builds compile libghostty-vt from its release tarballs (Arch with its own
Zig, the Flatpak with Flathub's Zig extension, Windows and the prebuilt Linux
archives through packaging/windows/libghostty-vt/build.py), others take a
ready-made one (macOS's official XCFramework, the deb/rpm/AppImage archives).
The version and the source checksums are pinned in several files; a bump that
misses one would ship different terminal engines on different platforms.
"""

import importlib.util
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "libghostty_vt_build", ROOT / "packaging" / "windows" / "libghostty-vt" / "build.py")
build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build)

SOURCE_URL, SOURCE_SHA = build.SOURCE
DEPS_URL, DEPS_SHA = build.DEPS


def test_build_script_derives_every_download_from_one_version():
    for url in (SOURCE_URL, DEPS_URL, build.XCFRAMEWORK[0],
                *(url for url, _sha in build.PREBUILT_LINUX.values())):
        assert f"{build.VERSION}" in url, url


def test_arch_builds_the_same_release():
    pkgbuild = (ROOT / "packaging" / "ArchLinux" / "PKGBUILD").read_text()
    assert re.search(rf"^_ghostty_vt={re.escape(build.VERSION)}$", pkgbuild, re.M), \
        "PKGBUILD _ghostty_vt differs from build.py VERSION"
    assert SOURCE_SHA in pkgbuild and DEPS_SHA in pkgbuild, \
        "PKGBUILD libghostty-vt checksums differ from build.py"


def test_flatpak_builds_the_same_release():
    manifest = yaml.safe_load((ROOT / "flatpak" / "io.github.mfat.sshpilot.yaml").read_text())
    module = next(m for m in manifest["modules"]
                  if isinstance(m, dict) and m.get("name") == "libghostty-vt")
    sources = {source["url"]: source["sha256"] for source in module["sources"]}
    assert sources == {SOURCE_URL: SOURCE_SHA, DEPS_URL: DEPS_SHA}
