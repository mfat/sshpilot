"""Windows frozen-bundle checks include the GI repository DLL (issue #1357)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "verify_bundle", ROOT / "packaging" / "windows" / "verify_bundle.py"
)
verify_bundle = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(verify_bundle)


def _minimal_bundle(root: Path, *, gi_dll: str = "girepository-2.0-0.dll") -> None:
    internal = root / "_internal"
    (internal / "sshpilot" / "resources").mkdir(parents=True)
    (internal / "share" / "glib-2.0" / "schemas").mkdir(parents=True)
    (internal / "lib" / "gdk-pixbuf").mkdir(parents=True)
    (internal / "gi_typelibs").mkdir(parents=True)
    for name in (
        "sshPilot.exe",
        "sshpilot-cli.exe",
    ):
        (root / name).write_bytes(b"exe")
    for name in ("libvterm-0.dll", "conpty.dll", "OpenConsole.exe", gi_dll, "glib-2.0-0.dll", "gio-2.0-0.dll"):
        (internal / name).write_bytes(b"dll")
    (internal / "share" / "glib-2.0" / "schemas" / "gschemas.compiled").write_bytes(b"x")
    (internal / "lib" / "gdk-pixbuf" / "loaders.cache").write_bytes(b"x")
    (internal / "sshpilot" / "resources" / "sshpilot.gresource").write_bytes(b"x")
    for name in verify_bundle.REQUIRED_TYPELIBS:
        (internal / "gi_typelibs" / f"{name}.typelib").write_bytes(b"x")


def test_bundle_ok_with_girepository_2(tmp_path):
    root = tmp_path / "sshPilot"
    _minimal_bundle(root)
    assert verify_bundle.main([str(root)]) == 0


def test_bundle_rejects_missing_girepository_dll(tmp_path, capsys):
    root = tmp_path / "sshPilot"
    _minimal_bundle(root)
    (root / "_internal" / "girepository-2.0-0.dll").unlink()
    assert verify_bundle.main([str(root)]) == 1
    assert "GI repository DLL" in capsys.readouterr().out
