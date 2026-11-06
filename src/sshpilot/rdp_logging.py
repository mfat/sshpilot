"""Forward FreeRDP's WinPR text diagnostics to the normal application logs.

Install before starting the first session. The native callback and DLL must
remain alive for the process lifetime: FreeRDP logs from its worker threads.
Only text at INFO and above is enabled; binary/packet dumps are not captured.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import sys

logger = logging.getLogger(__name__)
_native = None
_references = None
_attempted = False


class _Message(ctypes.Structure):
    # Public wLogMessage prefix (winpr/wlog.h); trailing dump fields unused.
    _fields_ = [
        ("type", ctypes.c_uint32), ("level", ctypes.c_uint32),
        ("prefix", ctypes.c_char_p), ("format", ctypes.c_char_p),
        ("text", ctypes.c_char_p), ("line", ctypes.c_size_t),
        ("file", ctypes.c_char_p), ("function", ctypes.c_char_p),
    ]


_Callback = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(_Message))


class _Callbacks(ctypes.Structure):
    _fields_ = [(name, ctypes.c_void_p) for name in ("data", "image", "message", "packet")]


def _forward(message) -> int:
    try:
        entry = message.contents
        if entry.type == 0 and entry.level >= 2 and entry.text:
            level = {2: logging.INFO, 3: logging.WARNING, 4: logging.ERROR,
                     5: logging.CRITICAL}.get(entry.level, logging.INFO)
            prefix = (entry.prefix or b"").decode("utf-8", "replace")
            text = entry.text.decode("utf-8", "replace")
            logging.getLogger("sshpilot.rdp.native").log(level, "%s %s", prefix, text)
    except Exception:
        # Exceptions must never escape a callback on a native worker thread.
        pass
    return 1


def install_native_log_capture() -> bool:
    """Connect the loaded WinPR 3 runtime to Python logging, once."""
    global _attempted, _native, _references
    if _attempted:
        return _native is not None
    _attempted = True
    try:
        name = "winpr3.dll" if sys.platform == "win32" else ctypes.util.find_library("winpr3")
        if not name:
            raise OSError("WinPR 3 library not found")
        native = ctypes.CDLL(name)
        for symbol, args, result in (
            ("WLog_GetRoot", [], ctypes.c_void_p),
            ("WLog_SetLogAppenderType", [ctypes.c_void_p, ctypes.c_uint32], ctypes.c_int),
            ("WLog_GetLogAppender", [ctypes.c_void_p], ctypes.c_void_p),
            ("WLog_ConfigureAppender", [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p], ctypes.c_int),
            ("WLog_SetLogLevel", [ctypes.c_void_p, ctypes.c_uint32], ctypes.c_int),
            ("WLog_OpenAppender", [ctypes.c_void_p], ctypes.c_int),
        ):
            function = getattr(native, symbol)
            function.argtypes, function.restype = args, result
        root = native.WLog_GetRoot()
        callback = _Callback(_forward)
        callbacks = _Callbacks(None, None, ctypes.cast(callback, ctypes.c_void_p), None)
        # Pin before handing any pointers to native code, including failure paths.
        _references = (native, callback, callbacks)
        if not root or not native.WLog_SetLogAppenderType(root, 3):
            raise OSError("Could not select WinPR callback appender")
        appender = native.WLog_GetLogAppender(root)
        if not appender or not native.WLog_ConfigureAppender(appender, b"callbacks", ctypes.byref(callbacks)):
            native.WLog_SetLogAppenderType(root, 0)
            raise OSError("Could not configure WinPR text callback")
        if not native.WLog_SetLogLevel(root, 2) or not native.WLog_OpenAppender(root):
            native.WLog_SetLogAppenderType(root, 0)
            raise OSError("Could not open WinPR callback appender")
        _native = native
        logger.info("FreeRDP native diagnostics enabled (INFO and above)")
        return True
    except Exception:
        logger.warning("FreeRDP native diagnostics could not be captured", exc_info=True)
        return False
