#!/usr/bin/env python3
"""Drive an already-open SSH Pilot terminal tab: focus, type, search, scroll."""
from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import time
from pathlib import Path

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
OUT = Path(__file__).resolve().parent

EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, w.HWND, w.LPARAM)
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_WHEEL = 0x0800
VK_CONTROL = 0x11
VK_SHIFT = 0x10
VK_RETURN = 0x0D
VK_ESCAPE = 0x1B
VK_F3 = 0x72
ORD_C = 0x43
ORD_V = 0x56
ORD_F = 0x46
ORD_L = 0x4C


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", w.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
        ("biPlanes", w.WORD), ("biBitCount", w.WORD), ("biCompression", w.DWORD),
        ("biSizeImage", w.DWORD), ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", w.DWORD),
        ("biClrImportant", w.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", w.DWORD * 3)]


def find_window(title: str) -> int:
    found = []

    def cb(hwnd, _lp):
        if user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, buf, 512)
            if buf.value == title:
                found.append(hwnd)
        return True

    user32.EnumWindows(EnumWindowsProc(cb), 0)
    return found[0] if found else 0


def bounds(hwnd):
    r = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def focus(hwnd):
    user32.ShowWindow(hwnd, 9)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.25)


def screenshot(hwnd, path: Path):
    left, top, width, height = bounds(hwnd)
    hdc_win = user32.GetWindowDC(hwnd)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_win)
    hbmp = gdi32.CreateCompatibleBitmap(hdc_win, width, height)
    gdi32.SelectObject(hdc_mem, hbmp)
    user32.PrintWindow(hwnd, hdc_mem, 2)
    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = width
    bmi.bmiHeader.biHeight = -height
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    buf = (ctypes.c_char * (width * height * 4))()
    gdi32.GetDIBits(hdc_mem, hbmp, 0, height, buf, ctypes.byref(bmi), 0)
    row = (width * 3 + 3) & ~3
    pixels = bytearray(row * height)
    for y in range(height):
        for x in range(width):
            i = (y * width + x) * 4
            j = (height - 1 - y) * row + x * 3
            pixels[j : j + 3] = buf[i : i + 3]
    bmp = path.with_suffix(".bmp")
    with open(bmp, "wb") as f:
        f.write(b"BM")
        f.write((54 + len(pixels)).to_bytes(4, "little"))
        f.write((0).to_bytes(4, "little"))
        f.write((54).to_bytes(4, "little"))
        f.write((40).to_bytes(4, "little"))
        f.write(width.to_bytes(4, "little"))
        f.write(height.to_bytes(4, "little"))
        f.write((1).to_bytes(2, "little"))
        f.write((24).to_bytes(2, "little"))
        f.write((0).to_bytes(4, "little"))
        f.write(len(pixels).to_bytes(4, "little"))
        f.write((0).to_bytes(16, "little"))
        f.write(pixels)
    gdi32.DeleteObject(hbmp)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(hwnd, hdc_win)
    print("saved", bmp)


def click(x, y):
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.05)
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def key_combo(*vks):
    for vk in vks:
        user32.keybd_event(vk, 0, 0, 0)
    for vk in reversed(vks):
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


def type_text(text: str):
    for ch in text:
        if ch == "\n":
            user32.keybd_event(VK_RETURN, 0, 0, 0)
            user32.keybd_event(VK_RETURN, 0, KEYEVENTF_KEYUP, 0)
            continue
        user32.keybd_event(0, 0, KEYEVENTF_UNICODE, ord(ch) if False else 0)
        # Unicode path via keybd_event scan
        scan = ctypes.windll.user32
        # Use VkKeyScanW for ASCII
        vk = user32.VkKeyScanW(ord(ch))
        if vk == -1:
            # fallback SendInput unicode
            class KEYBDINPUT(ctypes.Structure):
                _fields_ = [
                    ("wVk", w.WORD), ("wScan", w.WORD), ("dwFlags", w.DWORD),
                    ("time", w.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
                ]

            class INPUT(ctypes.Structure):
                class _I(ctypes.Union):
                    _fields_ = [("ki", KEYBDINPUT)]

                _anonymous_ = ("i",)
                _fields_ = [("type", w.DWORD), ("i", _I)]

            inp = INPUT()
            inp.type = 1
            inp.ki = KEYBDINPUT(0, ord(ch), KEYEVENTF_UNICODE, 0, None)
            user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
            inp.ki.dwFlags = KEYEVENTF_UNICODE | KEYEVENTF_KEYUP
            user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
            continue
        lo = vk & 0xFF
        shift = bool(vk & 0x100)
        if shift:
            user32.keybd_event(VK_SHIFT, 0, 0, 0)
        user32.keybd_event(lo, 0, 0, 0)
        user32.keybd_event(lo, 0, KEYEVENTF_KEYUP, 0)
        if shift:
            user32.keybd_event(VK_SHIFT, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.02)


def main():
    hwnd = find_window("SSH Pilot")
    assert hwnd, "no window"
    focus(hwnd)
    left, top, width, height = bounds(hwnd)

    # Click into terminal body (right of sidebar)
    term_x = left + 500
    term_y = top + 450
    click(term_x, term_y)
    time.sleep(0.3)
    screenshot(hwnd, OUT / "20_focused_term")

    # Clear + simple command
    type_text("echo SSHPILOT_PROBE_OK\n")
    time.sleep(1.2)
    screenshot(hwnd, OUT / "21_after_echo")

    # Columns / stty
    type_text("stty size; echo COLS=$(tput cols) ROWS=$(tput lines)\n")
    time.sleep(1.5)
    screenshot(hwnd, OUT / "22_stty_size")

    # Color / CSI
    type_text("printf '\\033[31mRED\\033[0m \\033[1;32mBOLDGREEN\\033[0m\\n'\n")
    time.sleep(1.0)
    screenshot(hwnd, OUT / "23_colors")

    # Box drawing stress
    type_text("printf '\\u250c\\u2500\\u2510\\n\\u2502x\\u2502\\n\\u2514\\u2500\\u2518\\n'\n")
    time.sleep(1.0)
    screenshot(hwnd, OUT / "24_box_drawing")

    # Open search Ctrl+Shift+F or Ctrl+F
    key_combo(VK_CONTROL, VK_SHIFT, ORD_F)
    time.sleep(0.6)
    screenshot(hwnd, OUT / "25_search_open")
    type_text("OpenWrt")
    time.sleep(0.5)
    screenshot(hwnd, OUT / "26_search_query")
    user32.keybd_event(VK_ESCAPE, 0, 0, 0)
    user32.keybd_event(VK_ESCAPE, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.3)

    # Scroll wheel over terminal
    user32.SetCursorPos(term_x, term_y)
    user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, 120 * 3, 0)  # scroll up
    time.sleep(0.4)
    screenshot(hwnd, OUT / "27_scrolled_up")
    user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, ctypes.c_uint(-120 * 6).value if False else (0xFFFFFFFF - 120 * 6 + 1) & 0xFFFFFFFF, 0)
    # signed wheel down
    user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, ctypes.c_int(-720).value & 0xFFFFFFFF, 0)
    time.sleep(0.4)
    screenshot(hwnd, OUT / "28_scrolled_down")

    # less (alternate screen) briefly
    type_text("echo LINE1; echo LINE2; printf '%s\\n' {1..40} | less\n")
    time.sleep(1.5)
    screenshot(hwnd, OUT / "29_less")
    type_text("q")
    time.sleep(0.8)
    screenshot(hwnd, OUT / "30_after_less")

    # Copy selection attempt: drag across text
    x1, y1 = left + 420, top + 380
    x2, y2 = left + 700, top + 380
    user32.SetCursorPos(x1, y1)
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.05)
    user32.SetCursorPos(x2, y2)
    time.sleep(0.1)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(0.2)
    key_combo(VK_CONTROL, VK_SHIFT, ORD_C)
    time.sleep(0.3)
    screenshot(hwnd, OUT / "31_after_copy")

    # Title probe
    type_text("printf '\\033]0;TITLEPROBE\\007'; sleep 0.2; echo titled\n")
    time.sleep(1.2)
    screenshot(hwnd, OUT / "32_osc_title")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
