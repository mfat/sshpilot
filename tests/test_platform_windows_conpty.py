"""The ctypes ConPTY binding: DLL choice, Win32 setup, and a real session."""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from sshpilot.api.models.terminal import TerminalDimensions
from sshpilot.platform.windows import conpty as conpty_ctypes
from sshpilot.platform.windows.conpty import (
    HPCON,
    PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
    STARTF_USESTDHANDLES,
    STARTUPINFOEXW,
    AttributeList,
    build_startup_info,
    conpty_dll_candidates,
    conpty_flags,
    load_conpty_api,
)
from sshpilot.daemon.conpty_runner import (
    ConPtySessionProcessRunner,
    pty_factory_from_environment,
)

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="needs kernel32 ConPTY")


# -- DLL selection ---------------------------------------------------------------


def test_candidates_of_a_source_checkout_are_vendor_conpty(monkeypatch):
    monkeypatch.setenv("PROCESSOR_ARCHITECTURE", "AMD64")
    monkeypatch.delenv("PROCESSOR_ARCHITEW6432", raising=False)
    (candidate,) = conpty_dll_candidates({}, frozen=False)
    parts = os.path.normpath(candidate).split(os.sep)
    assert parts[-4:] == ["vendor", "conpty", "x64", "conpty.dll"]
    root = os.sep.join(parts[:-4])
    assert os.path.isfile(os.path.join(root, "scripts", "fetch-conpty.py"))


def test_candidates_of_a_frozen_build_are_meipass_then_exe_dir(tmp_path):
    bundle, exe_dir = tmp_path / "_MEI", tmp_path / "app"
    assert conpty_dll_candidates(
        {},
        frozen=True,
        meipass=str(bundle),
        executable=str(exe_dir / "sshpilot.exe"),
    ) == [str(bundle / "conpty.dll"), str(exe_dir / "conpty.dll")]


def test_override_names_a_file_a_directory_or_the_inbox_console(tmp_path):
    dll = tmp_path / "custom.dll"
    assert conpty_dll_candidates({"SSHPILOT_CONPTY_DLL": str(dll)}, frozen=True) == [str(dll)]
    assert conpty_dll_candidates({"SSHPILOT_CONPTY_DLL": str(tmp_path)}, frozen=True) == [
        os.path.join(str(tmp_path), "conpty.dll")
    ]
    for name in ("kernel32", "Inbox"):
        assert conpty_dll_candidates({"SSHPILOT_CONPTY_DLL": name}, frozen=True) == []


class FakeLibrary:
    """Exposes whatever function names it is given, like a ctypes library."""

    def __init__(self, prefix):
        for name in ("CreatePseudoConsole", "ResizePseudoConsole", "ClosePseudoConsole"):
            setattr(self, prefix + name, _FakeFunction(prefix + name))


class _FakeFunction:
    def __init__(self, name):
        self.name = name

    def __call__(self, *args):
        return 0


def test_bundled_dll_is_bound_through_its_conpty_prefixed_exports(tmp_path, caplog):
    dll = tmp_path / "conpty.dll"
    dll.write_bytes(b"")
    (tmp_path / "OpenConsole.exe").write_bytes(b"")
    loaded = []

    def loader(path):
        loaded.append(path)
        return FakeLibrary("Conpty")

    with caplog.at_level(logging.INFO, logger=conpty_ctypes.__name__):
        api = load_conpty_api(loader=loader, kernel32=FakeLibrary(""), candidates=[str(dll)])
    assert loaded == [str(dll)]
    assert api.source == str(dll)
    assert api.create.name == "ConptyCreatePseudoConsole"
    assert api.close.name == "ConptyClosePseudoConsole"
    assert f"ConPTY: using {dll}" in caplog.text
    assert "OpenConsole.exe" not in caplog.text  # no warning: it is beside the DLL


def test_missing_or_broken_dll_falls_back_to_kernel32(tmp_path, caplog):
    broken = tmp_path / "broken" / "conpty.dll"
    broken.parent.mkdir()
    broken.write_bytes(b"")

    def loader(path):
        raise OSError("bad image")

    with caplog.at_level(logging.INFO, logger=conpty_ctypes.__name__):
        api = load_conpty_api(
            loader=loader,
            kernel32=FakeLibrary(""),
            candidates=[str(tmp_path / "absent.dll"), str(broken)],
        )
    assert api.source == "kernel32"
    assert api.create.name == "CreatePseudoConsole"
    assert "inbox kernel32" in caplog.text


def test_dll_without_open_console_is_used_with_a_warning(tmp_path, caplog):
    dll = tmp_path / "conpty.dll"
    dll.write_bytes(b"")
    with caplog.at_level(logging.WARNING, logger=conpty_ctypes.__name__):
        api = load_conpty_api(
            loader=lambda path: FakeLibrary("Conpty"),
            kernel32=FakeLibrary(""),
            candidates=[str(dll)],
        )
    assert api.source == str(dll)
    assert "no OpenConsole.exe" in caplog.text


def test_flags_come_from_the_environment():
    assert conpty_flags({}) == 0
    assert conpty_flags({"SSHPILOT_CONPTY_FLAGS": "0x1"}) == 1
    assert conpty_flags({"SSHPILOT_CONPTY_FLAGS": "nope"}) == 0


def test_backend_switch_defaults_to_ctypes(monkeypatch):
    fake_api = SimpleNamespace(source="kernel32")
    monkeypatch.setattr(conpty_ctypes, "default_api", lambda: fake_api)
    for environ in ({}, {"SSHPILOT_CONPTY_BACKEND": "ctypes"}, {"SSHPILOT_CONPTY_BACKEND": "bogus"}):
        assert pty_factory_from_environment(environ) is conpty_ctypes.CtypesPty
    assert pty_factory_from_environment({"SSHPILOT_CONPTY_BACKEND": "pywinpty"}) is None


# -- Attribute list and startup info ---------------------------------------------


class RecordingWin32:
    """Records the attribute-list calls the way kernel32 answers them."""

    REQUIRED = 48

    def __init__(self):
        self.calls = []

    def InitializeProcThreadAttributeList(self, buffer, count, flags, size_ref):
        size = size_ref._obj
        self.calls.append(("init", buffer is None, count, flags, size.value))
        if buffer is None:
            size.value = self.REQUIRED
            return False  # ERROR_INSUFFICIENT_BUFFER
        return size.value == self.REQUIRED

    def UpdateProcThreadAttribute(self, buffer, flags, attribute, value, size, previous, ret):
        self.calls.append(("update", attribute, value.value, size))
        return True

    def DeleteProcThreadAttributeList(self, buffer):
        self.calls.append(("delete",))


def test_attribute_list_sizes_then_initialises_then_sets_the_console():
    win32 = RecordingWin32()
    attributes = AttributeList(win32, 0x1234)
    assert win32.calls == [
        ("init", True, 1, 0, 0),
        ("init", False, 1, 0, RecordingWin32.REQUIRED),
        ("update", PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE, 0x1234, ctypes.sizeof(HPCON)),
    ]
    assert len(attributes.buffer) == RecordingWin32.REQUIRED

    info = build_startup_info(attributes)
    assert info.StartupInfo.cb == ctypes.sizeof(STARTUPINFOEXW)
    assert info.StartupInfo.dwFlags == STARTF_USESTDHANDLES
    assert info.lpAttributeList == attributes.address

    attributes.delete()
    attributes.delete()
    assert win32.calls[-1] == ("delete",)
    assert win32.calls.count(("delete",)) == 1


def test_command_line_quotes_the_program_and_appends_arguments():
    assert conpty_ctypes._command_line("C:/a b/ssh.exe", ' -p 22 "host"') == '"C:/a b/ssh.exe" -p 22 "host"'
    assert conpty_ctypes._command_line("ssh.exe", None) == "ssh.exe"


@windows_only
def test_real_pipes_and_attribute_list():
    win32 = conpty_ctypes.Win32()
    read_end, write_end = conpty_ctypes.create_pipe_pair(win32)
    try:
        written = ctypes.wintypes.DWORD()
        assert win32.WriteFile(write_end, b"ping", 4, ctypes.byref(written), None)
        buffer = ctypes.create_string_buffer(8)
        count = ctypes.wintypes.DWORD()
        assert win32.ReadFile(read_end, buffer, 8, ctypes.byref(count), None)
        assert buffer.raw[: count.value] == b"ping"
    finally:
        win32.CloseHandle(read_end)
        win32.CloseHandle(write_end)
    attributes = AttributeList(win32, 0x1)  # the value is not validated here
    assert len(attributes.buffer) > 0
    attributes.delete()


# -- A real session --------------------------------------------------------------


@windows_only
@pytest.mark.parametrize("backend", ["kernel32", "bundled"])
def test_session_delivers_output_then_eof_then_exit(backend):
    if backend == "bundled":
        bundled = os.environ.get("SSHPILOT_CONPTY_DLL", "")
        if not bundled or bundled.lower() in conpty_ctypes.INBOX_NAMES:
            pytest.skip("set SSHPILOT_CONPTY_DLL to test a bundled conpty.dll")
        api = load_conpty_api()
        if api.source == "kernel32":
            pytest.skip("SSHPILOT_CONPTY_DLL did not load")
    else:
        api = load_conpty_api({"SSHPILOT_CONPTY_DLL": "kernel32"})

    comspec = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
    runner = ConPtySessionProcessRunner(
        lambda spec: ([comspec, "/d", "/c", "echo", "hello-conpty&exit", "3"], dict(os.environ)),
        pty_factory=lambda columns, rows: ptys.append(
            conpty_ctypes.CtypesPty(columns, rows, api=api)
        )
        or ptys[-1],
    )
    events, output, ptys = [], [], []
    exited = threading.Event()
    spec = SimpleNamespace(
        session_id="session-1",
        connection_id="connection-1",
        dimensions=TerminalDimensions(rows=24, columns=80),
    )

    started = threading.Event()
    holder = {}

    def on_output(data):
        output.append(data)
        if b"\x1b[c" in data:  # the bundled ConPTY asks for DA1
            started.wait(5)
            holder["handle"].write(b"\x1b[?6c")

    try:
        handle = runner.start(
            spec,
            lambda info: (events.append(("exit", info.exit_code)), exited.set()),
            on_output,
            lambda: events.append("eof"),
        )
        holder["handle"] = handle
        started.set()
        handle.resize(TerminalDimensions(rows=30, columns=100))
        assert exited.wait(15)
        assert events == ["eof", ("exit", 3)]
        assert b"hello-conpty" in b"".join(output)
        # The exit watcher releases the process and input handles on its own.
        pty = ptys[-1]
        deadline = time.monotonic() + 5
        while (pty._process or pty._input_write) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert (pty._process, pty._input_write, pty._output_read) == (None, None, None)
        assert pty.get_exitstatus() == 3 and not pty.isalive()
    finally:
        runner.close()
