"""ConPTY through ``ctypes``: the pseudo console without pywinpty.

This follows Microsoft's "Creating a Pseudoconsole session" recipe:

1. two anonymous pipes (``CreatePipe``);
2. ``CreatePseudoConsole(size, input_read, output_write, flags, &hpc)``;
3. a ``STARTUPINFOEXW`` whose attribute list carries
   ``PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE``;
4. ``CreateProcessW`` with ``EXTENDED_STARTUPINFO_PRESENT``, after which the
   pseudo console's ends of the pipes are closed here.

:class:`CtypesPty` speaks the pywinpty 2.x surface (UTF-8 ``bytes``) that
:class:`~sshpilot.daemon.conpty_runner.ConPtyProcessHandle` already drives, so
the handle's reader thread and the runner's reaper stay as they are.

The three pseudo-console entry points come from a bundled ``conpty.dll``
(Microsoft.Windows.Console.ConPTY, which starts the ``OpenConsole.exe`` next to
it) when one is found, and from ``kernel32`` otherwise.

``ClosePseudoConsole`` on Windows 10 waits for conhost to exit, and conhost
waits until its last frame is written to the output pipe. Closing therefore
runs on its own thread while the session's reader keeps draining the pipe.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)

CONPTY_DLL_ENV = "SSHPILOT_CONPTY_DLL"
CONPTY_FLAGS_ENV = "SSHPILOT_CONPTY_FLAGS"
INBOX_NAMES = frozenset({"kernel32", "inbox", "system"})

EXTENDED_STARTUPINFO_PRESENT = 0x00080000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
STARTF_USESTDHANDLES = 0x00000100
PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
INFINITE = 0xFFFFFFFF
WAIT_OBJECT_0 = 0
STILL_ACTIVE = 259
ERROR_BROKEN_PIPE = 109
ERROR_INSUFFICIENT_BUFFER = 122

# After the client exits, the pseudo console is closed once its output has
# been quiet this long (or after the cap), so the final frame gets through.
_EXIT_QUIET = 0.1
_EXIT_QUIET_CAP = 0.5
_CLOSE_TIMEOUT = 2.0

HPCON = wintypes.HANDLE


class COORD(ctypes.Structure):
    _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [
        ("StartupInfo", STARTUPINFOW),
        ("lpAttributeList", ctypes.c_void_p),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


# -- Library loading -----------------------------------------------------------


@dataclass(frozen=True)
class ConPtyApi:
    """The three pseudo-console calls plus where they came from."""

    create: Callable
    resize: Callable
    close: Callable
    source: str  # "kernel32" or the conpty.dll path


def _bind(function, restype, argtypes):
    function.restype = restype
    function.argtypes = argtypes
    return function


def _bind_api(library, prefix: str, source: str) -> ConPtyApi:
    return ConPtyApi(
        create=_bind(
            getattr(library, f"{prefix}CreatePseudoConsole"),
            ctypes.c_long,  # HRESULT; WinDLL's HRESULT type would raise itself
            [COORD, wintypes.HANDLE, wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(HPCON)],
        ),
        resize=_bind(
            getattr(library, f"{prefix}ResizePseudoConsole"),
            ctypes.c_long,
            [HPCON, COORD],
        ),
        close=_bind(getattr(library, f"{prefix}ClosePseudoConsole"), None, [HPCON]),
        source=source,
    )


def conpty_dll_candidates(
    environ=None,
    *,
    frozen: Optional[bool] = None,
    meipass: Optional[str] = None,
    executable: Optional[str] = None,
) -> list[str]:
    """Where a bundled ``conpty.dll`` may live, most specific first.

    ``SSHPILOT_CONPTY_DLL`` names a DLL (or the directory holding it) and
    replaces every other place; ``kernel32`` forces the inbox ConPTY. A frozen
    build looks next to the exe and in ``_MEIPASS``. A source checkout finds
    nothing by default, so it keeps the inbox ConPTY unless told otherwise.
    """

    environ = os.environ if environ is None else environ
    override = (environ.get(CONPTY_DLL_ENV) or "").strip()
    if override:
        if override.lower() in INBOX_NAMES:
            return []
        if os.path.isdir(override):
            return [os.path.join(override, "conpty.dll")]
        return [override]
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    if not frozen:
        return []
    meipass = getattr(sys, "_MEIPASS", "") if meipass is None else meipass
    executable = sys.executable if executable is None else executable
    directories = [meipass, os.path.dirname(executable)]
    candidates = []
    for directory in directories:
        if directory:
            path = os.path.join(directory, "conpty.dll")
            if path not in candidates:
                candidates.append(path)
    return candidates


def load_conpty_api(
    environ=None,
    *,
    loader: Optional[Callable[[str], object]] = None,
    kernel32=None,
    candidates: Optional[list[str]] = None,
) -> ConPtyApi:
    """Bind ``conpty.dll`` when one is found and usable, else ``kernel32``."""

    if loader is None:
        loader = lambda path: ctypes.WinDLL(path)  # noqa: E731
    if candidates is None:
        candidates = conpty_dll_candidates(environ)
    for path in candidates:
        if not os.path.isfile(path):
            logger.debug("conpty.dll not found at %s", path)
            continue
        try:
            api = _bind_api(loader(path), "Conpty", path)
        except (OSError, AttributeError):
            logger.warning("Could not load %s; using the inbox ConPTY", path, exc_info=True)
            continue
        opener = os.path.join(os.path.dirname(path), "OpenConsole.exe")
        if not os.path.isfile(opener):
            # conpty.dll falls back to the inbox conhost without it.
            logger.warning("%s has no OpenConsole.exe beside it", path)
        logger.info("ConPTY: using %s", path)
        return api
    if kernel32 is None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    logger.info("ConPTY: using the inbox kernel32 pseudo console")
    return _bind_api(kernel32, "", "kernel32")


def conpty_flags(environ=None) -> int:
    raw = ((os.environ if environ is None else environ).get(CONPTY_FLAGS_ENV) or "").strip()
    if not raw:
        return 0
    try:
        return int(raw, 0)
    except ValueError:
        logger.warning("Ignoring %s=%r", CONPTY_FLAGS_ENV, raw)
        return 0


# -- Win32 plumbing ------------------------------------------------------------


class Win32:
    """The kernel32 calls besides the pseudo console itself."""

    def __init__(self, kernel32=None) -> None:
        k = kernel32 or ctypes.WinDLL("kernel32", use_last_error=True)
        LPVOID = ctypes.c_void_p
        PSIZE_T = ctypes.POINTER(ctypes.c_size_t)
        self.CreatePipe = _bind(
            k.CreatePipe,
            wintypes.BOOL,
            [ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(wintypes.HANDLE), LPVOID, wintypes.DWORD],
        )
        self.CloseHandle = _bind(k.CloseHandle, wintypes.BOOL, [wintypes.HANDLE])
        self.InitializeProcThreadAttributeList = _bind(
            k.InitializeProcThreadAttributeList,
            wintypes.BOOL,
            [LPVOID, wintypes.DWORD, wintypes.DWORD, PSIZE_T],
        )
        self.UpdateProcThreadAttribute = _bind(
            k.UpdateProcThreadAttribute,
            wintypes.BOOL,
            [LPVOID, wintypes.DWORD, ctypes.c_size_t, LPVOID, ctypes.c_size_t, LPVOID, PSIZE_T],
        )
        self.DeleteProcThreadAttributeList = _bind(
            k.DeleteProcThreadAttributeList, None, [LPVOID]
        )
        self.CreateProcessW = _bind(
            k.CreateProcessW,
            wintypes.BOOL,
            [
                wintypes.LPCWSTR,
                wintypes.LPWSTR,
                LPVOID,
                LPVOID,
                wintypes.BOOL,
                wintypes.DWORD,
                LPVOID,
                wintypes.LPCWSTR,
                ctypes.POINTER(STARTUPINFOEXW),
                ctypes.POINTER(PROCESS_INFORMATION),
            ],
        )
        self.ReadFile = _bind(
            k.ReadFile,
            wintypes.BOOL,
            [wintypes.HANDLE, LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), LPVOID],
        )
        self.WriteFile = _bind(
            k.WriteFile,
            wintypes.BOOL,
            [wintypes.HANDLE, LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), LPVOID],
        )
        self.PeekNamedPipe = _bind(
            k.PeekNamedPipe,
            wintypes.BOOL,
            [
                wintypes.HANDLE,
                LPVOID,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD),
                ctypes.POINTER(wintypes.DWORD),
                ctypes.POINTER(wintypes.DWORD),
            ],
        )
        self.WaitForSingleObject = _bind(
            k.WaitForSingleObject, wintypes.DWORD, [wintypes.HANDLE, wintypes.DWORD]
        )
        self.GetExitCodeProcess = _bind(
            k.GetExitCodeProcess,
            wintypes.BOOL,
            [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)],
        )


def _raise_last_error(what: str):
    raise ctypes.WinError(ctypes.get_last_error(), what)


def create_pipe_pair(win32: Win32) -> tuple[int, int]:
    """Return ``(read_end, write_end)`` of a new anonymous pipe."""

    read_end, write_end = wintypes.HANDLE(), wintypes.HANDLE()
    if not win32.CreatePipe(ctypes.byref(read_end), ctypes.byref(write_end), None, 0):
        _raise_last_error("CreatePipe")
    return read_end.value, write_end.value


class AttributeList:
    """A ``PROC_THREAD_ATTRIBUTE_LIST`` holding one pseudo-console attribute.

    ``InitializeProcThreadAttributeList`` runs twice: first with no buffer to
    learn the size (it fails with ``ERROR_INSUFFICIENT_BUFFER``), then to
    initialise the buffer it asked for.
    """

    def __init__(self, win32: Win32, hpc: int) -> None:
        self._win32 = win32
        size = ctypes.c_size_t(0)
        win32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        if not size.value:
            _raise_last_error("InitializeProcThreadAttributeList (size)")
        self.buffer = ctypes.create_string_buffer(size.value)
        if not win32.InitializeProcThreadAttributeList(self.buffer, 1, 0, ctypes.byref(size)):
            _raise_last_error("InitializeProcThreadAttributeList")
        self._initialised = True
        # The attribute value is the HPCON itself, passed by value as lpValue.
        self._hpc = HPCON(hpc)
        if not win32.UpdateProcThreadAttribute(
            self.buffer,
            0,
            PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
            self._hpc,
            ctypes.sizeof(HPCON),
            None,
            None,
        ):
            self.delete()
            _raise_last_error("UpdateProcThreadAttribute")

    @property
    def address(self) -> int:
        return ctypes.addressof(self.buffer)

    def delete(self) -> None:
        if self._initialised:
            self._initialised = False
            self._win32.DeleteProcThreadAttributeList(self.buffer)


def build_startup_info(attributes: AttributeList) -> STARTUPINFOEXW:
    info = STARTUPINFOEXW()
    info.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
    # Without STARTF_USESTDHANDLES a child of a process whose own stdio is
    # redirected (the daemon logs to a file) inherits those handles instead
    # of the pseudo console's.
    info.StartupInfo.dwFlags = STARTF_USESTDHANDLES
    info.lpAttributeList = attributes.address
    return info


def _coord(columns: int, rows: int) -> COORD:
    return COORD(max(1, min(int(columns), 0x7FFF)), max(1, min(int(rows), 0x7FFF)))


# -- The pseudo console --------------------------------------------------------


class CtypesPty:
    """One pseudo console and the process attached to it (pywinpty 2.x API)."""

    def __init__(
        self,
        columns: int,
        rows: int,
        *,
        api: Optional[ConPtyApi] = None,
        win32: Optional[Win32] = None,
        flags: Optional[int] = None,
    ) -> None:
        self._api = api or default_api()
        self._win32 = win32 or Win32()
        # _lock guards the console and process handles (instant calls only);
        # _write_lock guards the input pipe, whose writes can block.
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._hpc: Optional[int] = None
        self._input_read: Optional[int] = None
        self._input_write: Optional[int] = None
        self._output_read: Optional[int] = None
        self._output_write: Optional[int] = None
        self._process: Optional[int] = None
        self._eof = False
        self._console_closed = threading.Event()
        self._close_thread: Optional[threading.Thread] = None
        self._exit_thread: Optional[threading.Thread] = None
        self._last_output = time.monotonic()
        self.pid: Optional[int] = None
        self.exit_code: Optional[int] = None

        win32 = self._win32
        try:
            self._input_read, self._input_write = create_pipe_pair(win32)
            self._output_read, self._output_write = create_pipe_pair(win32)
            hpc = HPCON()
            result = self._api.create(
                _coord(columns, rows),
                self._input_read,
                self._output_write,
                conpty_flags() if flags is None else flags,
                ctypes.byref(hpc),
            )
            if result < 0:
                raise OSError(f"CreatePseudoConsole failed: HRESULT 0x{result & 0xFFFFFFFF:08X}")
            self._hpc = hpc.value
        except BaseException:
            self._release_handles()
            raise

    # -- pywinpty surface -----------------------------------------------------

    def spawn(self, appname, cmdline=None, cwd=None, env=None) -> bool:
        """Start ``appname`` attached to the console (``bytes`` or ``str``)."""

        appname, cmdline, cwd, env = (_text(value) for value in (appname, cmdline, cwd, env))
        # shutil.which can hand back "C:\Windows\system32/cmd.exe" (MSYS2's
        # os.sep is "/"); cmd.exe would read "/cmd.exe" in argv[0] as a switch.
        appname = appname.replace("/", "\\")
        win32 = self._win32
        attributes = AttributeList(win32, self._hpc)
        try:
            startup = build_startup_info(attributes)
            process = PROCESS_INFORMATION()
            # CreateProcessW may write into lpCommandLine, so it gets a buffer.
            command = ctypes.create_unicode_buffer(_command_line(appname, cmdline))
            environment = ctypes.create_unicode_buffer(env) if env else None
            ok = win32.CreateProcessW(
                appname,
                command,
                None,
                None,
                False,
                EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT,
                environment,
                cwd,
                ctypes.byref(startup),
                ctypes.byref(process),
            )
            if not ok:
                _raise_last_error("CreateProcessW")
        except BaseException:
            # Nothing reads the output yet; the console's first frame fits in
            # the pipe buffer, so closing it here does not block.
            self.close()
            self._release_handles()
            raise
        finally:
            attributes.delete()
        win32.CloseHandle(process.hThread)
        self._process = process.hProcess
        self.pid = int(process.dwProcessId)
        # The console holds its own references to these ends now.
        self._close_handle("_input_read")
        self._close_handle("_output_write")
        self._exit_thread = threading.Thread(
            target=self._close_after_exit,
            name=f"sshpilot-conpty-exit-{self.pid}",
            daemon=True,
        )
        self._exit_thread.start()
        return True

    def read(self, length: int = 1000, blocking: bool = False) -> bytes:
        handle = self._output_read
        if handle is None or self._eof:
            raise EOFError("pseudo console closed")
        win32 = self._win32
        if not blocking:
            available = wintypes.DWORD(0)
            if not win32.PeekNamedPipe(handle, None, 0, None, ctypes.byref(available), None):
                self._mark_eof()
                raise EOFError("pseudo console closed")
            if not available.value:
                return b""
            length = min(length, available.value)
        buffer = ctypes.create_string_buffer(max(1, length))
        count = wintypes.DWORD(0)
        if not win32.ReadFile(handle, buffer, len(buffer), ctypes.byref(count), None):
            error = ctypes.get_last_error()
            self._mark_eof()
            if error not in (ERROR_BROKEN_PIPE, 0):
                logger.debug("ConPTY read failed: %s", ctypes.FormatError(error))
            raise EOFError("pseudo console closed")
        self._last_output = time.monotonic()
        return buffer.raw[: count.value]

    def write(self, data) -> int:
        if isinstance(data, str):
            data = data.encode("utf-8")
        with self._write_lock:
            handle = self._input_write
            if handle is None:
                raise OSError("pseudo console closed")
            view = memoryview(bytes(data))
            while view:
                written = wintypes.DWORD(0)
                chunk = (ctypes.c_char * len(view)).from_buffer_copy(view)
                if not self._win32.WriteFile(handle, chunk, len(view), ctypes.byref(written), None):
                    _raise_last_error("WriteFile")
                view = view[written.value :]
        return len(data)

    def set_size(self, columns: int, rows: int) -> None:
        with self._lock:
            hpc = self._hpc
            if hpc is None:
                return
            result = self._api.resize(hpc, _coord(columns, rows))
        if result < 0:
            raise OSError(f"ResizePseudoConsole failed: HRESULT 0x{result & 0xFFFFFFFF:08X}")

    def isalive(self) -> bool:
        with self._lock:
            process = self._process
            if process is None:
                return False
            return self._win32.WaitForSingleObject(process, 0) != WAIT_OBJECT_0

    def get_exitstatus(self) -> Optional[int]:
        with self._lock:
            process = self._process
            if process is not None:
                code = wintypes.DWORD(0)
                if self._win32.GetExitCodeProcess(process, ctypes.byref(code)):
                    if code.value != STILL_ACTIVE:
                        self.exit_code = int(code.value)
        return self.exit_code

    def iseof(self) -> bool:
        return self._eof

    # -- teardown -------------------------------------------------------------

    @property
    def api_source(self) -> str:
        return self._api.source

    def close(self, timeout: float = _CLOSE_TIMEOUT) -> None:
        """Close the console (draining is the reader's job) and the input.

        The process handle belongs to the exit watcher, which may still be
        waiting on it; the watcher releases it once the client has exited.
        """

        if self._wait_console_closed(timeout):
            self._close_handle("_input_write")

    def _close_after_exit(self) -> None:
        process = self._process
        if process is None:
            return
        self._win32.WaitForSingleObject(process, INFINITE)
        # The client is gone; conhost may still be rendering its last frame.
        deadline = time.monotonic() + _EXIT_QUIET_CAP
        while time.monotonic() < deadline and not self._eof:
            if time.monotonic() - self._last_output >= _EXIT_QUIET:
                break
            time.sleep(0.02)
        closed = self._wait_console_closed(_CLOSE_TIMEOUT)
        self.get_exitstatus()  # keep the code once the handle is gone
        if closed:
            self._close_handle("_input_write")
        self._close_handle("_process")

    def _wait_console_closed(self, timeout: float) -> bool:
        self._start_console_close()
        thread = self._close_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
            if thread.is_alive():
                logger.warning("ClosePseudoConsole did not return within %.1fs", timeout)
                return False
        return True

    def _start_console_close(self) -> None:
        with self._lock:
            if self._close_thread is not None:
                return
            hpc, self._hpc = self._hpc, None
            if hpc is None:
                self._console_closed.set()
                return
            self._close_thread = threading.Thread(
                target=self._close_console,
                args=(hpc,),
                name=f"sshpilot-conpty-close-{self.pid}",
                daemon=True,
            )
        self._close_thread.start()

    def _close_console(self, hpc: int) -> None:
        try:
            self._api.close(hpc)
        finally:
            self._console_closed.set()

    def _mark_eof(self) -> None:
        if self._eof:
            return
        self._eof = True
        # Only the reader uses this end, so the reader closes it.
        self._close_handle("_output_read")

    def _close_handle(self, name: str) -> None:
        with self._write_lock if name == "_input_write" else self._lock:
            handle = getattr(self, name)
            setattr(self, name, None)
        if handle:
            self._win32.CloseHandle(handle)

    def _release_handles(self) -> None:
        for name in ("_input_read", "_input_write", "_output_read", "_output_write", "_process"):
            self._close_handle(name)


def _text(value):
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8")
    return value


def _command_line(appname: str, cmdline: Optional[str]) -> str:
    """pywinpty's convention: ``cmdline`` holds the arguments after argv[0]."""

    quoted = f'"{appname}"' if " " in appname or "\t" in appname else appname
    return quoted + (cmdline or "")


_default_api: Optional[ConPtyApi] = None
_default_api_lock = threading.Lock()


def default_api() -> ConPtyApi:
    global _default_api
    with _default_api_lock:
        if _default_api is None:
            _default_api = load_conpty_api()
        return _default_api
