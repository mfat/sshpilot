"""GtkSourceView must land in Contents/Frameworks/ for GI typelib dlopen."""

import os
import sys
from pathlib import Path

sys.path.insert(
    0,
    os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "packaging", "pyinstaller")
    ),
)

from gtk_bundle import (  # noqa: E402
    GI_DYLIB_BUNDLE_DEST,
    collect_homebrew_dylibs,
    ensure_gtksourceview_abi_name,
    find_gtksourceview_dylibs,
    gtksourceview_bundle_ok,
    nested_frameworks_paths,
)


def test_gi_dylib_dest_is_frameworks_root():
    # Nested "Frameworks" dest was the PyInstaller bug that broke the SSH
    # config editor ("Failed to load ... libgtksourceview-5.0.dylib").
    assert GI_DYLIB_BUNDLE_DEST == "."


def test_collect_homebrew_dylibs_uses_root_dest(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "libgtksourceview-5.0.dylib").write_bytes(b"fake")
    (lib / "libvte-2.91.0.dylib").write_bytes(b"fake")

    pairs = collect_homebrew_dylibs(
        str(lib),
        ["libgtksourceview-5.*.dylib", "libvte-2.91.*.dylib"],
    )
    assert pairs
    assert all(dest == "." for _src, dest in pairs)
    names = {Path(src).name for src, _dest in pairs}
    assert "libgtksourceview-5.0.dylib" in names
    assert "libvte-2.91.0.dylib" in names


def test_ensure_abi_symlink_when_only_versioned_present(tmp_path):
    frameworks = tmp_path / "Frameworks"
    frameworks.mkdir()
    versioned = frameworks / "libgtksourceview-5.0.0.dylib"
    versioned.write_bytes(b"fake")

    assert not (frameworks / "libgtksourceview-5.0.dylib").exists()
    abi = ensure_gtksourceview_abi_name(frameworks)
    assert abi is not None
    assert abi.name == "libgtksourceview-5.0.dylib"
    assert abi.is_symlink()
    assert abi.resolve() == versioned.resolve()
    assert gtksourceview_bundle_ok(frameworks)


def test_gtksourceview_bundle_ok_requires_frameworks_root(tmp_path):
    frameworks = tmp_path / "Frameworks"
    frameworks.mkdir()
    nested = frameworks / "Frameworks"
    nested.mkdir()
    (nested / "libgtksourceview-5.0.dylib").write_bytes(b"fake")

    assert find_gtksourceview_dylibs(frameworks) == []
    assert nested_frameworks_paths(frameworks)
    assert not gtksourceview_bundle_ok(frameworks)

    # After placing at root, verification passes.
    (frameworks / "libgtksourceview-5.0.dylib").write_bytes(b"fake")
    assert gtksourceview_bundle_ok(frameworks)
