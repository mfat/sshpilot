"""Native FreeRDP diagnostics reach the regular, sanitized application logs."""

import ctypes
import logging
import os
from pathlib import Path
import subprocess
import sys

import pytest

from sshpilot.rdp_logging import _Message, _forward


def test_text_callback_preserves_severity_and_ignores_packet_data(caplog):
    with caplog.at_level(logging.INFO, logger="sshpilot.rdp.native"):
        message = _Message(type=0, level=3, prefix=b"[com.freerdp.core]", text=b"transport failure")
        assert _forward(ctypes.pointer(message)) == 1
        message.type = 3
        message.text = b"packet payload"
        _forward(ctypes.pointer(message))
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING
    assert "transport failure" in caplog.text
    assert "packet payload" not in caplog.text


def test_bundled_winpr_callback_writes_viewer_log(tmp_path):
    runtime = Path(__file__).resolve().parents[1] / "vendor" / "gtk" / "bin"
    if sys.platform != "win32" or not (runtime / "winpr3.dll").exists():
        pytest.skip("requires bundled Windows WinPR runtime")
    # Keep the process-wide native appender isolated from the test runner.
    script = r'''
import ctypes, os, sys
handle = os.add_dll_directory(sys.argv[1])
from sshpilot.logging_support import configure_frontend_logging, close_managed_handlers
from sshpilot.rdp_logging import install_native_log_capture
configure_frontend_logging(sys.argv[2], "info")
assert install_native_log_capture()
assert install_native_log_capture()
native = ctypes.CDLL("winpr3.dll")
native.WLog_Get.argtypes = [ctypes.c_char_p]
native.WLog_Get.restype = ctypes.c_void_p
native.WLog_PrintTextMessage.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
    ctypes.c_size_t, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p]
assert native.WLog_PrintTextMessage(native.WLog_Get(b"com.freerdp.test"), 3,
    1, b"test", b"probe", b"RDP diagnostic password=hunter2")
close_managed_handlers()
'''
    result = subprocess.run([sys.executable, "-c", script, str(runtime), str(tmp_path)],
                            env=dict(os.environ), capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    for filename in ("sshpilot.log", "app.log"):
        text = (tmp_path / filename).read_text(encoding="utf-8")
        assert "sshpilot.rdp.native - WARNING" in text
        assert "RDP diagnostic" in text
        assert "hunter2" not in text
        assert text.count("native diagnostics enabled") == 1
