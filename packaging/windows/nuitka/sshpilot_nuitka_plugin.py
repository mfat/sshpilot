"""Nuitka user plugin for the experimental Windows build (build.ps1 here).

gi namespaces without a typelib
    Nuitka 4.3's gi plugin aborts the build when code imports a namespace
    whose typelib is absent, even inside try/except. On Windows that is Vte,
    Secret, WebKit, GdkX11 and GLibUnix. Treat them as unavailable, as
    PyInstaller does.

GTK libraries
    gi loads each namespace's library at runtime from the typelib's
    shared-library field, so they are not link-time dependencies of
    gi/_gi.pyd and Nuitka's DLL scan misses them; its gi plugin only names a
    fixed GTK 3 list. Read the field from each included typelib and hand the
    libraries to Nuitka as DLLs; it then collects their own dependencies.

Vendored binaries
    libvterm, ConPTY and OpenConsole (vendor/), GLib's gspawn helpers and the
    SVG pixbuf loader are loaded by path at runtime, not linked. Nuitka's
    documentation says DLLs and EXEs must not be included as data files.

Modules kept as bytecode
    MSVC spent 2 h 23 min optimising the C that Nuitka generates for
    sshpilot.log_viewer (every other module took under 30 s). Ship it as
    bytecode instead; it is not on the startup path.
"""

import os
import struct

from nuitka.plugins.PluginBase import NuitkaPluginBase
from nuitka.plugins.standard import GiPlugin
from nuitka.utils.ModuleNames import ModuleName

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
GTK = os.path.join(ROOT, "vendor", "gtk")
PIXBUF_LOADER = "lib/gdk-pixbuf-2.0/2.10.0/loaders/pixbufloader_svg.dll"

# (source relative to ROOT, destination in the app folder, is an executable)
VENDORED = (
    ("vendor/libvterm/x64/libvterm-0.dll", "libvterm-0.dll", False),
    ("vendor/conpty/x64/conpty.dll", "conpty.dll", False),
    ("vendor/conpty/x64/OpenConsole.exe", "OpenConsole.exe", True),
    ("vendor/gtk/bin/gspawn-win64-helper.exe", "gspawn-win64-helper.exe", True),
    ("vendor/gtk/bin/gspawn-win64-helper-console.exe", "gspawn-win64-helper-console.exe", True),
    ("vendor/gtk/" + PIXBUF_LOADER, PIXBUF_LOADER, False),
)

BYTECODE_MODULES = ("sshpilot.log_viewer",)

_gi_repository = ModuleName("gi.repository")
_get_namespace_info = GiPlugin.NuitkaPluginGi._getNamespaceInfo


def _get_namespace_info_or_none(self, namespace):
    try:
        return _get_namespace_info(self, namespace)
    except Exception:
        self.info("no typelib for gi namespace '%s', skipping" % namespace)
        return None


GiPlugin.NuitkaPluginGi._getNamespaceInfo = _get_namespace_info_or_none


def _typelib_shared_libraries(typelib_path):
    """The shared-library field of a typelib: DLL names, comma separated.

    It is a string offset at byte 52 of the header (girepository's
    gitypelib-internal.h: Header.shared_library).
    """
    with open(typelib_path, "rb") as typelib:
        data = typelib.read()
    if not data.startswith(b"GOBJ\nMETADATA\r\n\x1a"):
        return []
    (offset,) = struct.unpack_from("<I", data, 52)
    if not offset:
        return []
    return [name for name in data[offset : data.index(b"\0", offset)].decode().split(",") if name]


class NuitkaPluginSshPilot(NuitkaPluginBase):
    plugin_name = "sshpilot"
    plugin_desc = "sshPilot Windows build: GTK libraries, vendored binaries, bytecode modules."

    # Several namespaces share a library (Gtk, Gdk and Gsk are gtk-4-1.dll).
    _gtk_libraries = set()

    def decideCompilation(self, module_name):
        if module_name in BYTECODE_MODULES:
            return "bytecode"
        return None

    def getExtraDlls(self, module):
        full_name = module.getFullName()

        if full_name.getPackageName() == _gi_repository:
            info = _get_namespace_info_or_none(self, full_name.getBasename())
            if info is None or not info.typelib_path:
                return
            for dll_name in _typelib_shared_libraries(info.typelib_path):
                path = os.path.normpath(os.path.join(GTK, "bin", dll_name))
                if dll_name in self._gtk_libraries or not os.path.isfile(path):
                    continue
                self._gtk_libraries.add(dll_name)
                # Attached to gi._gi, like Nuitka's gi plugin does: the
                # gi.repository modules are virtual and cannot own a DLL.
                yield self.makeDllEntryPoint(
                    source_path=path,
                    dest_path=dll_name,
                    module_name=ModuleName("gi._gi"),
                    package_name=ModuleName("gi"),
                    reason="shared library of gi namespace '%s'" % full_name.getBasename(),
                )

        elif full_name == "sshpilot":
            for source, dest, executable in VENDORED:
                make = self.makeExeEntryPoint if executable else self.makeDllEntryPoint
                yield make(
                    source_path=os.path.normpath(os.path.join(ROOT, source)),
                    dest_path=os.path.normpath(dest),
                    module_name=full_name,
                    package_name=None,
                    reason="loaded by path at runtime",
                )
