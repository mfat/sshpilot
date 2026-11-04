"""The bundled gtk4-frdp and the Windows build's gtk4-frdp are the same release.

Linux, Flatpak, AppImage and macOS builds compile subprojects/gtk4-frdp;
Windows downloads a prebuilt zip pinned in scripts/fetch-gtk.py. Both are set
by scripts/update-gtk4-frdp.sh vX.Y.Z, so a mismatch means half an update.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENDORED = ROOT / "subprojects" / "gtk4-frdp" / "VENDORED"
FETCH_GTK = ROOT / "scripts" / "fetch-gtk.py"
SUBPROJECT_MESON = ROOT / "subprojects" / "gtk4-frdp" / "meson.build"

HOW_TO_FIX = ("run scripts/update-gtk4-frdp.sh vX.Y.Z with a gtk4-frdp release tag; "
              "it updates both (docs/development/gtk4-frdp.md)")


def _windows_version() -> str:
    match = re.search(r'^FRDP_VERSION = "([^"]+)"$', FETCH_GTK.read_text(), re.M)
    assert match, "scripts/fetch-gtk.py has no FRDP_VERSION"
    return match.group(1)


def test_bundled_copy_comes_from_a_release_tag():
    lines = VENDORED.read_text().splitlines()
    tag = lines[2] if len(lines) > 2 else ""
    assert tag, f"subprojects/gtk4-frdp is an untagged commit ({lines[1]}); {HOW_TO_FIX}"


def test_windows_build_uses_the_same_release():
    lines = VENDORED.read_text().splitlines()
    tag = lines[2] if len(lines) > 2 else ""
    assert tag == f"v{_windows_version()}", (
        f"bundled gtk4-frdp is {tag or 'an untagged commit'} but the Windows build "
        f"pins {_windows_version()}; {HOW_TO_FIX}")


def test_bundled_source_is_the_version_its_tag_says():
    match = re.search(r"version:\s*'([^']+)'", SUBPROJECT_MESON.read_text())
    assert match, "subprojects/gtk4-frdp/meson.build has no version"
    lines = VENDORED.read_text().splitlines()
    tag = lines[2] if len(lines) > 2 else ""
    if tag:
        assert tag == f"v{match.group(1)}", (
            f"VENDORED says {tag} but the source is version {match.group(1)}")
