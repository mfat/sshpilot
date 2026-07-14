"""Helpers for bundling GTK/GI dylibs into the macOS PyInstaller .app.

GI typelibs dlopen bare sonames (e.g. ``libgtksourceview-5.0.dylib``) from
``Contents/Frameworks/``. Placing libraries under a nested
``Contents/Frameworks/Frameworks/`` directory leaves them invisible to that
lookup and breaks features such as the SSH config editor.

The stock PyInstaller hook ``hook-gi.repository.GtkSource``
(https://github.com/pyinstaller/pyinstaller/pull/3893) already collects the
shared library with dest ``"."`` (Frameworks root) and, on macOS, rewrites the
typelib to ``@loader_path/…``. It defaults to GtkSource 3.0 — use
``hooksconfig['gi']['module-versions']['GtkSource'] = '5'`` plus a
``gi.repository.GtkSource`` hiddenimport so the hook actually runs for v5.
These helpers remain as an explicit Homebrew fallback and for build-time
verification.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional, Sequence, Tuple


# Dest "." → Contents/Frameworks/<lib>.dylib (correct for GI dlopen).
# Dest "Frameworks" → Contents/Frameworks/Frameworks/<lib>.dylib (broken).
GI_DYLIB_BUNDLE_DEST = "."


def collect_homebrew_dylibs(
    hb_lib: str,
    patterns: Sequence[str],
    dest: str = GI_DYLIB_BUNDLE_DEST,
) -> List[Tuple[str, str]]:
    """Return ``(src, dest)`` pairs for Homebrew dylibs matching *patterns*."""
    import glob

    binaries: List[Tuple[str, str]] = []
    for pat in patterns:
        for src in glob.glob(os.path.join(hb_lib, pat)):
            binaries.append((src, dest))
    return binaries


def find_gtksourceview_dylibs(frameworks_dir: Path) -> List[Path]:
    """List ``libgtksourceview-5*.dylib`` entries directly under Frameworks."""
    if not frameworks_dir.is_dir():
        return []
    return sorted(frameworks_dir.glob("libgtksourceview-5*.dylib"))


def ensure_gtksourceview_abi_name(frameworks_dir: Path) -> Optional[Path]:
    """Ensure ``libgtksourceview-5.0.dylib`` exists (typelib ABI name).

    If only a versioned real file is present (e.g. ``libgtksourceview-5.0.0.dylib``),
    create a relative symlink with the ABI name. Returns the ABI path, or None
    if no gtksourceview dylib is present at Frameworks root.
    """
    abi = frameworks_dir / "libgtksourceview-5.0.dylib"
    if abi.exists() or abi.is_symlink():
        return abi

    versioned = [
        p for p in find_gtksourceview_dylibs(frameworks_dir)
        if p.name != abi.name
    ]
    if not versioned:
        return None

    target = versioned[0]
    abi.symlink_to(target.name)
    return abi


def gtksourceview_bundle_ok(frameworks_dir: Path) -> bool:
    """True when Frameworks root has the ABI-named GtkSourceView dylib."""
    ensure_gtksourceview_abi_name(frameworks_dir)
    abi = frameworks_dir / "libgtksourceview-5.0.dylib"
    return abi.exists() or abi.is_symlink()


def nested_frameworks_paths(frameworks_dir: Path) -> List[Path]:
    """Return gtksourceview dylibs wrongly nested under Frameworks/Frameworks."""
    nested = frameworks_dir / "Frameworks"
    return find_gtksourceview_dylibs(nested)
