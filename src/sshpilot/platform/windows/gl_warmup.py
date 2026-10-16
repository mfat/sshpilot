"""Load the OpenGL driver on a background thread while the GUI starts.

GTK's default renderer on Windows draws through WGL. The first pixel format a
process chooses loads the graphics vendor's OpenGL driver (its ICD), which
took ~75 ms of the ~105 ms that the first GL context cost here, and GTK pays
it inside ``present()`` of the main window, on the GTK thread. Creating and
dropping a throwaway context on a worker before GTK is imported loads the
driver in parallel with the imports; GTK's own context then costs what any
later one does (~20 ms). ctypes releases the GIL during each call, so the
worker does not hold back the Python work on the main thread.

Stdlib only, Windows only, and best effort: any failure just leaves the
driver to load when GTK first needs it.
"""
from __future__ import annotations

import os
import sys
import threading

# Renderers that do not draw through WGL: warming the GL driver would only
# burn CPU next to the imports.
_NON_GL_RENDERERS = {"cairo", "vulkan"}

#: Set to ``1`` to leave the driver to load when GTK first needs it.
DISABLE_ENV = "SSHPILOT_NO_GL_WARMUP"

_started = False


def _warm() -> None:
    import ctypes
    from ctypes import wintypes

    class PIXELFORMATDESCRIPTOR(ctypes.Structure):
        _fields_ = [
            ("nSize", wintypes.WORD),
            ("nVersion", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("iPixelType", ctypes.c_ubyte),
            ("cColorBits", ctypes.c_ubyte),
            ("cRedBits", ctypes.c_ubyte),
            ("cRedShift", ctypes.c_ubyte),
            ("cGreenBits", ctypes.c_ubyte),
            ("cGreenShift", ctypes.c_ubyte),
            ("cBlueBits", ctypes.c_ubyte),
            ("cBlueShift", ctypes.c_ubyte),
            ("cAlphaBits", ctypes.c_ubyte),
            ("cAlphaShift", ctypes.c_ubyte),
            ("cAccumBits", ctypes.c_ubyte),
            ("cAccumRedBits", ctypes.c_ubyte),
            ("cAccumGreenBits", ctypes.c_ubyte),
            ("cAccumBlueBits", ctypes.c_ubyte),
            ("cAccumAlphaBits", ctypes.c_ubyte),
            ("cDepthBits", ctypes.c_ubyte),
            ("cStencilBits", ctypes.c_ubyte),
            ("cAuxBuffers", ctypes.c_ubyte),
            ("iLayerType", ctypes.c_ubyte),
            ("bReserved", ctypes.c_ubyte),
            ("dwLayerMask", wintypes.DWORD),
            ("dwVisibleMask", wintypes.DWORD),
            ("dwDamageMask", wintypes.DWORD),
        ]

    user32 = ctypes.WinDLL("user32")
    gdi32 = ctypes.WinDLL("gdi32")
    opengl32 = ctypes.WinDLL("opengl32")

    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    ]
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    pfd_p = ctypes.POINTER(PIXELFORMATDESCRIPTOR)
    gdi32.ChoosePixelFormat.argtypes = [wintypes.HDC, pfd_p]
    gdi32.SetPixelFormat.argtypes = [wintypes.HDC, ctypes.c_int, pfd_p]
    opengl32.wglCreateContext.restype = wintypes.HANDLE
    opengl32.wglCreateContext.argtypes = [wintypes.HDC]
    opengl32.wglMakeCurrent.argtypes = [wintypes.HDC, wintypes.HANDLE]
    opengl32.wglDeleteContext.argtypes = [wintypes.HANDLE]

    # A hidden 1x1 window of a system class: a pixel format can be set only
    # once per window, so never on one GTK will use.
    hwnd = user32.CreateWindowExW(0, "STATIC", "", 0, 0, 0, 1, 1,
                                  None, None, None, None)
    if not hwnd:
        return
    try:
        hdc = user32.GetDC(hwnd)
        if not hdc:
            return
        try:
            pfd = PIXELFORMATDESCRIPTOR()
            pfd.nSize = ctypes.sizeof(PIXELFORMATDESCRIPTOR)
            pfd.nVersion = 1
            # PFD_DRAW_TO_WINDOW | PFD_SUPPORT_OPENGL | PFD_DOUBLEBUFFER
            pfd.dwFlags = 0x4 | 0x20 | 0x1
            pfd.cColorBits = 32
            fmt = gdi32.ChoosePixelFormat(hdc, ctypes.byref(pfd))
            if not fmt or not gdi32.SetPixelFormat(hdc, fmt, ctypes.byref(pfd)):
                return
            context = opengl32.wglCreateContext(hdc)
            if not context:
                return
            if opengl32.wglMakeCurrent(hdc, context):
                opengl32.wglMakeCurrent(None, None)
            opengl32.wglDeleteContext(context)
        finally:
            user32.ReleaseDC(hwnd, hdc)
    finally:
        user32.DestroyWindow(hwnd)


def _run() -> None:
    try:
        _warm()
    except Exception:
        pass


def start() -> bool:
    """Warm the GL driver on a daemon thread (once; Windows GL renderers only)."""
    global _started
    if _started or sys.platform != "win32" or os.environ.get(DISABLE_ENV) == "1":
        return False
    renderer = os.environ.get("GSK_RENDERER", "").strip().lower()
    if renderer in _NON_GL_RENDERERS:
        return False
    _started = True
    threading.Thread(target=_run, name="sshpilot-gl-warmup", daemon=True).start()
    return True
