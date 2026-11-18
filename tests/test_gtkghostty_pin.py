"""The bundled gtkghostty and the Windows build's gtkghostty are the same release.

The Linux packages compile subprojects/gtkghostty; Windows downloads a
prebuilt zip pinned in scripts/fetch-gtk.py. Both are set by
scripts/update-gtkghostty.sh vX.Y.Z, so a mismatch means half an update.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENDORED = ROOT / "subprojects" / "gtkghostty" / "VENDORED"
FETCH_GTK = ROOT / "scripts" / "fetch-gtk.py"
SUBPROJECT_MESON = ROOT / "subprojects" / "gtkghostty" / "meson.build"

HOW_TO_FIX = ("run scripts/update-gtkghostty.sh vX.Y.Z with a gtkghostty release tag; "
              "it updates both (docs/development/gtkghostty.md)")


def _tag() -> str:
    lines = VENDORED.read_text().splitlines()
    return lines[2] if len(lines) > 2 else ""


def _windows_version() -> str:
    match = re.search(r'^GTKGHOSTTY_VERSION = "([^"]+)"$', FETCH_GTK.read_text(), re.M)
    assert match, "scripts/fetch-gtk.py has no GTKGHOSTTY_VERSION"
    return match.group(1)


def test_bundled_copy_comes_from_a_release_tag():
    commit = VENDORED.read_text().splitlines()[1]
    assert _tag(), f"subprojects/gtkghostty is an untagged commit ({commit}); {HOW_TO_FIX}"


def test_windows_build_uses_the_same_release():
    assert _tag() == f"v{_windows_version()}", (
        f"bundled gtkghostty is {_tag() or 'an untagged commit'} but the Windows build "
        f"pins {_windows_version()}; {HOW_TO_FIX}")


def test_bundled_source_is_the_version_its_tag_says():
    match = re.search(r"version:\s*'([^']+)'", SUBPROJECT_MESON.read_text())
    assert match, "subprojects/gtkghostty/meson.build has no version"
    if _tag():
        assert _tag() == f"v{match.group(1)}", (
            f"VENDORED says {_tag()} but the source is version {match.group(1)}")
