"""Windows GTK/GI runtime pinning (issue #1357)."""

from __future__ import annotations

import os
import types
from pathlib import Path

from sshpilot.gi_versions import require_gui_versions
from sshpilot.platform.windows import gtk_runtime as gr


def _gi_dir(path: Path, *dlls: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for name in dlls:
        (path / name).write_bytes(b"dll")
    return path


def test_configure_is_noop_off_windows(monkeypatch):
    monkeypatch.setattr(gr.sys, "platform", "linux")
    gr.reset_for_tests()
    setup = gr.configure()
    assert setup.skipped == "not-windows"
    assert setup.dll_dirs == []


def test_girepository_2_dir_is_recognised(tmp_path):
    bundled = _gi_dir(tmp_path / "internal", "girepository-2.0-0.dll", "gio-2.0-0.dll")
    foreign = _gi_dir(
        tmp_path / "msys" / "ucrt64" / "bin",
        "girepository-1.0-1.dll",
        "gio-2.0-0.dll",
    )
    exe = tmp_path / "sshPilot.exe"
    exe.write_bytes(b"exe")
    dirs = gr.discover_dll_dirs(
        frozen=True,
        meipass=str(bundled),
        executable=str(exe),
        environ={"PATH": str(foreign)},
    )
    assert os.path.normcase(dirs[0]) == os.path.normcase(str(bundled))
    assert any(os.path.normcase(d) == os.path.normcase(str(foreign)) for d in dirs)


def test_typelib_dirs_include_pyinstaller_gi_typelibs(tmp_path):
    meipass = _gi_dir(tmp_path / "internal", "girepository-2.0-0.dll")
    typelibs = meipass / "gi_typelibs"
    typelibs.mkdir()
    (typelibs / "Gio-2.0.typelib").write_bytes(b"typelib")
    found = gr.discover_typelib_dirs([str(meipass)], meipass=str(meipass))
    assert any(os.path.normcase(d) == os.path.normcase(str(typelibs)) for d in found)


def test_configure_pins_path_and_keeps_dll_directory_cookies(tmp_path, monkeypatch):
    gr.reset_for_tests()
    monkeypatch.setattr(gr.sys, "platform", "win32")
    cookies = []

    def add_dll_directory(path):
        token = types.SimpleNamespace(path=path, closed=False)

        def close():
            token.closed = True

        token.close = close
        cookies.append(token)
        return token

    monkeypatch.setattr(gr.os, "add_dll_directory", add_dll_directory, raising=False)

    bundled = _gi_dir(tmp_path / "internal", "girepository-2.0-0.dll", "gio-2.0-0.dll")
    typelibs = bundled / "gi_typelibs"
    typelibs.mkdir()
    (typelibs / "Gtk-4.0.typelib").write_bytes(b"typelib")
    foreign = tmp_path / "elsewhere"
    foreign.mkdir()
    env = {"PATH": str(foreign), "GI_TYPELIB_PATH": "", "PYGI_DLL_PATH": ""}
    setup = gr.configure(
        environ=env,
        frozen=True,
        meipass=str(bundled),
        executable=str(tmp_path / "sshPilot.exe"),
        write_log=False,
    )
    assert setup.dll_dirs
    assert os.path.normcase(setup.dll_dirs[0]) == os.path.normcase(str(bundled))
    path_entries = env["PATH"].split(os.pathsep)
    assert os.path.normcase(path_entries[0]) == os.path.normcase(str(bundled))
    assert os.path.normcase(str(foreign)) in {
        os.path.normcase(e) for e in path_entries
    }
    assert os.path.normcase(str(bundled)) in {
        os.path.normcase(e) for e in env["PYGI_DLL_PATH"].split(os.pathsep)
    }
    assert os.path.normcase(str(typelibs)) in {
        os.path.normcase(e) for e in env["GI_TYPELIB_PATH"].split(os.pathsep)
    }
    assert cookies
    assert all(not cookie.closed for cookie in cookies)
    assert gr._DLL_DIRECTORY_COOKIES == cookies


def test_require_gui_versions_configures_before_importing_gi(monkeypatch):
    order = []
    monkeypatch.setattr("sshpilot.gi_versions.sys.platform", "win32")

    def configure():
        order.append("configure")

    monkeypatch.setattr(
        "sshpilot.platform.windows.gtk_runtime.configure", configure
    )
    require_gui_versions()
    assert order == ["configure"]


def test_log_import_failure_records_interface_error(tmp_path, monkeypatch):
    gr.reset_for_tests()
    monkeypatch.setattr(gr.sys, "platform", "win32")
    log = tmp_path / "startup-gtk.log"
    monkeypatch.setattr(gr, "startup_log_path", lambda: str(log))
    try:
        raise TypeError("must be an interface")
    except TypeError as exc:
        path = gr.log_import_failure(exc)
    assert path == str(log)
    text = log.read_text(encoding="utf-8")
    assert "TypeError: must be an interface" in text
    assert "gio-2.0-0.dll" in text


def test_run_py_configures_gtk_before_openssh():
    source = Path(__file__).resolve().parents[1] / "run.py"
    text = source.read_text(encoding="utf-8")
    assert text.index("configure_gtk_runtime") < text.index("prefer_builtin_openssh")
