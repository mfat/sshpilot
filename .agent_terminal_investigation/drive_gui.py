#!/usr/bin/env python3
"""Drive the running SSH Pilot HWND on Windows (ctypes). No GTK needed."""
from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import sys
import time
from pathlib import Path

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

OUT = Path(__file__).resolve().parent
PW_RENDERFULLCONTENT = 2
SW_RESTORE = 9
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
KEYEVENTF_KEYUP = 0x0002
VK_RETURN = 0x0D
VK_ESCAPE = 0x1B

EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, w.HWND, w.LPARAM)


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
        if not user32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buf, 512)
        if buf.value == title:
            found.append(hwnd)
        return True

    user32.EnumWindows(EnumWindowsProc(cb), 0)
    return found[0] if found else 0


def focus(hwnd: int) -> None:
    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.3)


def bounds(hwnd: int) -> tuple[int, int, int, int]:
    r = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def screenshot(hwnd: int, path: Path) -> None:
    left, top, width, height = bounds(hwnd)
    if width <= 0 or height <= 0:
        raise RuntimeError(f"bad bounds {left},{top},{width},{height}")
    hdc_win = user32.GetWindowDC(hwnd)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_win)
    hbmp = gdi32.CreateCompatibleBitmap(hdc_win, width, height)
    gdi32.SelectObject(hdc_mem, hbmp)
    ok = user32.PrintWindow(hwnd, hdc_mem, PW_RENDERFULLCONTENT)
    if not ok:
        # fallback BitBlt from screen DC
        hdc_screen = user32.GetDC(0)
        gdi32.BitBlt(hdc_mem, 0, 0, width, height, hdc_screen, left, top, 0x00CC0020)
        user32.ReleaseDC(0, hdc_screen)

    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = width
    bmi.bmiHeader.biHeight = -height
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0
    buf = (ctypes.c_char * (width * height * 4))()
    gdi32.GetDIBits(hdc_mem, hbmp, 0, height, buf, ctypes.byref(bmi), 0)

    # write PNG via stdlib-less raw then pillow-free: use BMP file
    # Prefer png via optional PIL; else BMP
    try:
        from PIL import Image  # type: ignore

        img = Image.frombuffer("RGBA", (width, height), bytes(buf), "raw", "BGRA", 0, 1)
        img.convert("RGB").save(path)
    except Exception:
        bmp_path = path.with_suffix(".bmp")
        # BITMAPFILEHEADER + BITMAPINFOHEADER + pixels (bottom-up)
        row = ((width * 3 + 3) & ~3)
        pixel_size = row * height
        file_size = 14 + 40 + pixel_size
        # convert BGRA -> BGR bottom-up
        pixels = bytearray(pixel_size)
        for y in range(height):
            src_y = y  # already top-down from negative height
            dst_y = height - 1 - y
            for x in range(width):
                i = (src_y * width + x) * 4
                j = dst_y * row + x * 3
                pixels[j : j + 3] = buf[i : i + 3]
        with open(bmp_path, "wb") as f:
            f.write(b"BM")
            f.write(file_size.to_bytes(4, "little"))
            f.write((0).to_bytes(4, "little"))
            f.write((54).to_bytes(4, "little"))
            f.write((40).to_bytes(4, "little"))
            f.write(width.to_bytes(4, "little"))
            f.write(height.to_bytes(4, "little"))
            f.write((1).to_bytes(2, "little"))
            f.write((24).to_bytes(2, "little"))
            f.write((0).to_bytes(4, "little"))
            f.write(pixel_size.to_bytes(4, "little"))
            f.write((0).to_bytes(16, "little"))
            f.write(pixels)
        path = bmp_path

    gdi32.DeleteObject(hbmp)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(hwnd, hdc_win)
    print(f"saved {path} printwindow_ok={ok} size={width}x{height}")


def click(x: int, y: int, double: bool = False) -> None:
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.05)
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    if double:
        time.sleep(0.08)
        user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def key(vk: int) -> None:
    user32.keybd_event(vk, 0, 0, 0)
    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


def main() -> int:
    hwnd = find_window("SSH Pilot")
    if not hwnd:
        print("SSH Pilot window not found", file=sys.stderr)
        return 1
    print(f"hwnd={hwnd} bounds={bounds(hwnd)}")
    focus(hwnd)
    screenshot(hwnd, OUT / "10_focused.png")

    left, top, width, height = bounds(hwnd)
    # Layout from live screenshot: sidebar ~280px, Router row ~ y=145 from window top,
    # bottom terminal icon ~ y=height-36, x~ left+250
    router_x = left + 140
    router_y = top + 145
    print(f"select Router @ {router_x},{router_y}")
    click(router_x, router_y, double=False)
    time.sleep(0.4)
    screenshot(hwnd, OUT / "11_router_selected.png")

    # Open via Enter (common sshpilot binding)
    print("press Enter to open connection")
    key(VK_RETURN)
    time.sleep(3.5)
    screenshot(hwnd, OUT / "12_after_enter.png")

    # If still on welcome, try terminal button in sidebar footer
    term_btn_x = left + 250
    term_btn_y = top + height - 40
    print(f"click sidebar terminal button @ {term_btn_x},{term_btn_y}")
    click(term_btn_x, term_btn_y, double=False)
    time.sleep(3.5)
    screenshot(hwnd, OUT / "13_after_term_button.png")

    # Header terminal icon ~ left+70, top+55
    header_term_x = left + 75
    header_term_y = top + 55
    print(f"click header terminal @ {header_term_x},{header_term_y}")
    click(header_term_x, header_term_y, double=False)
    time.sleep(3.5)
    screenshot(hwnd, OUT / "14_after_header_term.png")

    # Double-click Router as last resort
    print("double-click Router")
    click(router_x, router_y, double=True)
    time.sleep(4.0)
    screenshot(hwnd, OUT / "15_after_dblclick.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
