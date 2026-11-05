"""Exercise the C Windows key translation against the bundled WinPR DLL."""

import ctypes
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows input path")


@pytest.fixture(scope="module")
def translate(tmp_path_factory):
    compiler = shutil.which("gcc")
    if compiler is None and Path("C:/msys64/ucrt64/bin/gcc.exe").exists():
        compiler = "C:/msys64/ucrt64/bin/gcc.exe"
    runtime = ROOT / "vendor/gtk/bin"
    if compiler is None or not (runtime / "winpr3.dll").exists():
        pytest.skip("requires GCC and the bundled Windows runtime")

    # Compile the production Windows branch, without needing the GTK/FreeRDP
    # development headers. The lookup itself runs in the real WinPR library.
    source = (ROOT / "subprojects/gtk4-frdp/src/frdp-session.c").read_text()
    send_key = source.split("frdp_session_send_key (", 1)[1]
    branch = send_key.split("#if defined(G_OS_WIN32)", 1)[1].split("#elif", 1)[0]
    directory = tmp_path_factory.mktemp("rdp-keyboard")
    c_file = directory / "translate.c"
    dll_file = directory / "translate.dll"
    c_file.write_text(
        '#include <windows.h>\n#define KBDEXT 0x100\n'
        '__declspec(dllexport) unsigned int translate(unsigned int keycode) {\n'
        'unsigned int virtual_code;\n' + branch + '\nreturn virtual_code;\n}\n'
    )
    compiler_env = dict(os.environ)
    compiler_env["PATH"] = str(Path(compiler).resolve().parent) + os.pathsep + compiler_env["PATH"]
    result = subprocess.run([compiler, "-shared", "-o", str(dll_file), str(c_file)],
                            env=compiler_env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    native = ctypes.CDLL(str(dll_file))
    native.translate.argtypes = [ctypes.c_uint32]
    native.translate.restype = ctypes.c_uint32
    with os.add_dll_directory(str(runtime)):
        winpr = ctypes.CDLL(str(runtime / "winpr3.dll"))
        lookup = winpr.GetVirtualScanCodeFromVirtualKeyCode
        lookup.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
        lookup.restype = ctypes.c_uint32
        yield lambda vk: lookup(native.translate(vk), 4)


@pytest.mark.parametrize("vk,scan", [
    (0x25, 0x14B), (0x26, 0x148), (0x27, 0x14D), (0x28, 0x150),
    (0x24, 0x147), (0x23, 0x14F), (0x21, 0x149), (0x22, 0x151),
    (0x2D, 0x152), (0x2E, 0x153),
    (0x41, 0x1E), (0x0D, 0x1C), (0x08, 0x0E),
    (0x60, 0x52), (0x64, 0x4B), (0x66, 0x4D), (0x68, 0x48),
])
def test_windows_keys_produce_expected_rdp_scancodes(translate, vk, scan):
    assert translate(vk) == scan
