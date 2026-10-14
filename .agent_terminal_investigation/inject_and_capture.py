#!/usr/bin/env python3
"""Send Unicode input to SSH Pilot via SendInput and capture with BitBlt/CopyFromScreen."""
from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import struct
import time
from pathlib import Path

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
OUT = Path(__file__).resolve().parent

EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, w.HWND, w.LPARAM)
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK_RETURN = 0x0D
VK_CONTROL = 0x11
VK_SHIFT = 0x10
ORD_F = 0x46


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", w.WORD),
        ("wScan", w.WORD),
        ("dwFlags", w.DWORD),
        ("time", w.DWORD),
        ("dwExtraInfo", ctypes.c_ulonglong),  # ULONG_PTR
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", w.LONG), ("dy", w.LONG), ("mouseData", w.DWORD),
        ("dwFlags", w.DWORD), ("time", w.DWORD), ("dwExtraInfo", ctypes.c_ulonglong),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", w.DWORD), ("wParamL", w.WORD), ("wParamH", w.WORD)]


class INPUT_UNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", w.DWORD), ("u", INPUT_UNION)]


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
                found.append(int(hwnd))
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
    time.sleep(0.35)


def send_unicode(text: str):
    for ch in text:
        if ch == "\n":
            down = INPUT(type=INPUT_KEYBOARD, u=INPUT_UNION(ki=KEYBDINPUT(VK_RETURN, 0, 0, 0, 0)))
            up = INPUT(type=INPUT_KEYBOARD, u=INPUT_UNION(ki=KEYBDINPUT(VK_RETURN, 0, KEYEVENTF_KEYUP, 0, 0)))
            user32.SendInput(1, ctypes.byref(down), ctypes.sizeof(INPUT))
            user32.SendInput(1, ctypes.byref(up), ctypes.sizeof(INPUT))
            time.sleep(0.03)
            continue
        code = ord(ch)
        down = INPUT(
            type=INPUT_KEYBOARD,
            u=INPUT_UNION(ki=KEYBDINPUT(0, code, KEYEVENTF_UNICODE, 0, 0)),
        )
        up = INPUT(
            type=INPUT_KEYBOARD,
            u=INPUT_UNION(ki=KEYBDINPUT(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0)),
        )
        user32.SendInput(1, ctypes.byref(down), ctypes.sizeof(INPUT))
        user32.SendInput(1, ctypes.byref(up), ctypes.sizeof(INPUT))
        time.sleep(0.015)


def click(x, y):
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.05)
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    user32.mouse_event(0x0004, 0, 0, 0, 0)


def capture_screen(hwnd, path: Path):
    left, top, width, height = bounds(hwnd)
    hdc_screen = user32.GetDC(0)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    hbmp = gdi32.CreateCompatibleBitmap(hdc_screen, width, height)
    gdi32.SelectObject(hdc_mem, hbmp)
    gdi32.BitBlt(hdc_mem, 0, 0, width, height, hdc_screen, left, top, 0x00CC0020)
    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = width
    bmi.bmiHeader.biHeight = -height
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    buf = (ctypes.c_ubyte * (width * height * 4))()
    gdi32.GetDIBits(hdc_mem, hbmp, 0, height, buf, ctypes.byref(bmi), 0)
    # fingerprint
    total = sum(buf[i] for i in range(0, len(buf), 16))
    row = (width * 3 + 3) & ~3
    pixels = bytearray(row * height)
    for y in range(height):
        for x in range(width):
            i = (y * width + x) * 4
            j = (height - 1 - y) * row + x * 3
            pixels[j] = buf[i]
            pixels[j + 1] = buf[i + 1]
            pixels[j + 2] = buf[i + 2]
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
    user32.ReleaseDC(0, hdc_screen)
    print(f"saved {bmp} fingerprint={total} size={width}x{height}")
    return total


def main():
    hwnd = find_window("SSH Pilot")
    assert hwnd, "no window"
    print("hwnd", hwnd, "bounds", bounds(hwnd))
    focus(hwnd)
    left, top, width, height = bounds(hwnd)

    # Dismiss tip banner first (click Dismiss ~ center-left of blue bar)
    click(left + 420, top + 95)
    time.sleep(0.4)

    # Click deep into terminal body
    click(left + 640, top + 520)
    time.sleep(0.3)
    fp0 = capture_screen(hwnd, OUT / "40_before_inject")

    # Ctrl-C then unique probe
    for vk in (VK_CONTROL, 0x43):
        user32.keybd_event(vk, 0, 0, 0)
    for vk in (0x43, VK_CONTROL):
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.4)
    send_unicode("echo SSHPILOT_PROBE_42\n")
    time.sleep(1.5)
    fp1 = capture_screen(hwnd, OUT / "41_after_echo")
    print("delta echo", fp1 - fp0)

    send_unicode("stty size\n")
    time.sleep(1.0)
    fp2 = capture_screen(hwnd, OUT / "42_stty")
    print("delta stty", fp2 - fp1)

    send_unicode("printf '\\033[31mRED\\033[0m OK\\n'\n")
    time.sleep(1.0)
    fp3 = capture_screen(hwnd, OUT / "43_color")
    print("delta color", fp3 - fp2)

    # title
    send_unicode("printf '\\033]0;TITLEPROBE\\007'; echo titled\n")
    time.sleep(1.2)
    fp4 = capture_screen(hwnd, OUT / "44_title")
    print("delta title", fp4 - fp3)

    # less
    send_unicode("seq 1 30 | less\n")
    time.sleep(1.5)
    fp5 = capture_screen(hwnd, OUT / "45_less")
    print("delta less", fp5 - fp4)
    send_unicode("q")
    time.sleep(0.8)
    fp6 = capture_screen(hwnd, OUT / "46_after_less")
    print("delta q", fp6 - fp5)

    # search
    for vk in (VK_CONTROL, VK_SHIFT, ORD_F):
        user32.keybd_event(vk, 0, 0, 0)
    for vk in (ORD_F, VK_SHIFT, VK_CONTROL):
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
    time.sleep(0.5)
    send_unicode("OpenWrt")
    time.sleep(0.6)
    fp7 = capture_screen(hwnd, OUT / "47_search")
    print("delta search", fp7 - fp6)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
