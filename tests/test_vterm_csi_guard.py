"""libvterm 0.3.3 overflows ``args[16]`` on a CSI with 16+ separators.

The model filters CSI parameters before libvterm sees them. A regression
would be an access violation, so the crash tests feed in a subprocess.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from sshpilot.vterm_terminal_model import _CsiArgGuard, _CSI_MAX_SEPARATORS


def _guarded(*chunks: bytes) -> bytes:
    guard = _CsiArgGuard()
    return b"".join(guard.filter(chunk) for chunk in chunks)


def _separators_in_longest_csi(data: bytes) -> int:
    import re

    runs = re.findall(rb"\x1b\[([\x30-\x3f\x00-\x17\x19\x1c-\x1f\x7f]*)", data)
    return max((run.count(b";") + run.count(b":") for run in runs), default=0)


@pytest.mark.parametrize(
    "payload",
    [
        b"\x1b[" + b";" * 17 + b"m",
        b"\x1b[" + b";".join(b"1" for _ in range(40)) + b"m",
        b"\x1b[38:2:" + b":" * 30 + b"m",
        b"\x1b[" + b";\n" * 20 + b"m",  # C0 controls do not end a CSI
        b"\x1b[?" + b";" * 64 + b"h",
    ],
)
def test_long_csi_is_capped_in_one_feed(payload):
    out = _guarded(b"before" + payload + b"after")
    assert _separators_in_longest_csi(out) <= _CSI_MAX_SEPARATORS
    assert out.startswith(b"before") and out.endswith(b"after")


def test_long_csi_is_capped_across_every_split():
    payload = b"x\x1b[" + b"1;" * 30 + b"mY"
    for cut in range(len(payload) + 1):
        out = _guarded(payload[:cut], payload[cut:])
        assert _separators_in_longest_csi(out) <= _CSI_MAX_SEPARATORS, cut
        assert out.endswith(b"mY"), cut


def test_split_into_single_bytes():
    payload = b"\x1b[" + b";" * 40 + b"m"
    out = _guarded(*(payload[i:i + 1] for i in range(len(payload))))
    assert _separators_in_longest_csi(out) <= _CSI_MAX_SEPARATORS


def test_ordinary_sequences_pass_unchanged():
    data = (
        b"\x1b[0m\x1b[38;2;10;20;30;48;5;200mhi\x1b[1;2;3;4;5;6;7;8;9;10;11;12;13;14;15;16m"
        b"\x1b]0;a;b;c;d;e;f;g;h;i;j;k;l;m;n;o;p;q;r\x07 text;with;semicolons;"
        + b";" * 30
    )
    assert _guarded(data) == data
    assert _guarded(data[:7], data[7:]) == data


def test_can_and_esc_end_the_csi():
    data = b"\x1b[" + b";" * 10 + b"\x18" + b"\x1b[" + b";" * 10 + b"m"
    assert _guarded(data) == data


@pytest.mark.parametrize("chunked", [False, True])
def test_stock_libvterm_does_not_crash(chunked):
    pytest.importorskip("ctypes")
    script = "\n".join([
        "import sys",
        "from sshpilot.vterm_terminal_model import VtermTerminalModel, VtermUnavailable",
        "try:",
        "    model = VtermTerminalModel(80, 24)",
        "except VtermUnavailable:",
        "    sys.exit(77)",
        r"payload = b'\x1b[' + b';' * 17 + b'm' + b'\x1b[' + b'1;' * 200 + b'31mRED'",
        f"chunked = {chunked!r}",
        "if chunked:",
        "    for i in range(len(payload)):",
        "        model.feed(payload[i:i + 1])",
        "else:",
        "    model.feed(payload)",
        r"model.feed(b'\r\nok')",
        "print(model.row_text(model.history_length + 1).strip())",
    ])
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60,
    )
    if result.returncode == 77:
        pytest.skip("libvterm is not available")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_stock_dll_gets_the_python_guard(monkeypatch):
    from sshpilot import vterm_terminal_model as vtm

    class Lib:
        pass

    assert vtm.csi_args_bounded(Lib()) is False


def test_abi_self_test_rejects_a_foreign_cell_layout():
    import ctypes

    from sshpilot import vterm_terminal_model as vtm

    class ForeignLib:
        def vterm_new(self, rows, cols):
            return 1

        def vterm_set_utf8(self, vt, on):
            pass

        def vterm_obtain_screen(self, vt):
            return 2

        def vterm_screen_reset(self, screen, hard):
            pass

        def vterm_input_write(self, vt, data, length):
            return length

        def vterm_screen_get_cell(self, screen, pos, buffer):
            # Neovim's fork: an schar index where chars[6] would be.
            ctypes.memmove(buffer, (b"\x07\x00\x00\x00" + b"\xff" * 36), 40)
            return 1

        def vterm_free(self, vt):
            pass

    assert "layout" in vtm._abi_self_test(ForeignLib())
