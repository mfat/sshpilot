#!/usr/bin/env bash
# Build a self-contained SSH Pilot AppImage.
#
#   packaging/appimage/build-appimage.sh [VERSION] [OUTDIR]
#
# VERSION defaults to src/sshpilot/__init__.py::__version__, OUTDIR to dist/.
#
# The AppImage bundles the GTK 4 stack (GTK, libadwaita, VTE, GtkSourceView,
# libsecret, WebKitGTK), their typelibs, a CPython interpreter, PyGObject and
# the Python dependencies from pyproject.toml. Not bundled: anything
# host-coupled (see packaging/appimage/excludelist), and OpenSSH -- the app
# drives the host's ssh, as it does in every other package. glibc is bundled
# too, but off to the side, for hosts older than the build host only (see 8b).
#
# WebKitGTK is the expensive part of that list and the fiddly one: it runs its
# renderer out of process, so the WebKit*Process helpers and the injected
# bundle are bundled too and AppRun points WEBKIT_EXEC_PATH /
# WEBKIT_INJECTED_BUNDLE_PATH at them. It earns its size -- both the
# PyXterm.js terminal backend and the Host Info tab are WebKit-only, and an
# AppImage without it would quietly have fewer features than the .deb.
#
# What makes the result relocatable:
#
#   * every bundled ELF gets an $ORIGIN RPATH, so nothing needs
#     LD_LIBRARY_PATH and nothing the app spawns (the user's shell, ssh,
#     sshpass) inherits our libraries;
#   * the bundled interpreter sits in the usual $prefix/bin + $prefix/lib
#     layout, so it finds usr/lib/python3/dist-packages -- where Meson put the
#     app -- with no PYTHONHOME or PYTHONPATH;
#   * the Meson-generated launcher and build_config.py are rewritten to derive
#     $prefix from their own location instead of the /usr baked in at install
#     time, which would otherwise resolve to the *host's* sshpilot.
#
# Build host: Ubuntu 24.04, the oldest release carrying libadwaita >= 1.5.
# Hosts with an older glibc than the build host's run on the bundled copy
# (step 8b and AppRun); the host libraries in the excludelist -- GL, X11,
# Wayland, fonts -- are what still sets a floor there.
set -euo pipefail

log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)

APP_ID=io.github.mfat.sshpilot
ARCH=$(uname -m)
PYTHON_BIN=${PYTHON_BIN:-/usr/bin/python3}
BUILD_DIR=${BUILD_DIR:-$ROOT/build/appimage}
APPDIR=$BUILD_DIR/AppDir
OUTDIR=${2:-$ROOT/dist}

VERSION=${1:-}
if [ -z "$VERSION" ]; then
    VERSION=$("$PYTHON_BIN" - "$ROOT/src/sshpilot/__init__.py" <<'PY'
import re
import sys

text = open(sys.argv[1], encoding="utf-8").read()
match = re.search(r"""__version__\s*=\s*['"]([^'"]+)['"]""", text)
sys.exit("no __version__ in " + sys.argv[1]) if not match else print(match.group(1))
PY
)
fi
VERSION=${VERSION#v}

[ -x "$PYTHON_BIN" ] || die "no Python interpreter at $PYTHON_BIN (set PYTHON_BIN)"
for tool in meson ninja patchelf glib-compile-schemas ldconfig; do
    command -v "$tool" >/dev/null || die "$tool is required but not installed"
done

# The bundled interpreter, PyGObject and every compiled wheel have to be the
# same build: PYTHON_BIN is both what Meson configures against and what the
# AppImage ships.
PY_VER=$("$PYTHON_BIN" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
PY_REAL=$("$PYTHON_BIN" -c 'import os, sys; print(os.path.realpath(sys.executable))')
PY_STDLIB=$("$PYTHON_BIN" -c 'import sysconfig; print(sysconfig.get_paths()["stdlib"])')

log "Building sshpilot $VERSION AppImage for $ARCH (python $PY_VER from $PY_REAL)"

rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR" "$OUTDIR"

# --------------------------------------------------------------------------
# 1. Install the app the same way every distro package does.
# --------------------------------------------------------------------------
log "Installing with Meson"
# Prepend PYTHON_BIN's directory so python.find_installation() cannot pick a
# different interpreter than the one being bundled.
PATH="$(dirname "$PY_REAL"):$PATH" \
    meson setup "$BUILD_DIR/meson" "$ROOT" --prefix=/usr --buildtype=release
meson install -C "$BUILD_DIR/meson" --destdir "$APPDIR"

# Where Meson put the Python package. Asking the AppDir rather than sysconfig:
# Meson installs into the interpreter's *system* scheme (dist-packages on
# Debian), which is not what sysconfig reports by default there.
MODULEDIR=$(dirname "$(find "$APPDIR/usr" -type d -path '*-packages/sshpilot' -print -quit)")
[ -d "$MODULEDIR/sshpilot" ] || die "Meson did not install the Python package under $APPDIR/usr"
echo "package installed into ${MODULEDIR#"$APPDIR"}"

LIBDIR="$APPDIR/usr/lib"
TYPELIBDIR="$LIBDIR/girepository-1.0"
PIXBUF_DIR="$LIBDIR/gdk-pixbuf-2.0/2.10.0"
mkdir -p "$LIBDIR" "$TYPELIBDIR" "$PIXBUF_DIR/loaders" "$LIBDIR/gio/modules"

# --------------------------------------------------------------------------
# 2. The interpreter and its standard library.
# --------------------------------------------------------------------------
log "Bundling CPython $PY_VER"
install -Dm755 "$PY_REAL" "$APPDIR/usr/bin/python$PY_VER"
ln -sf "python$PY_VER" "$APPDIR/usr/bin/python3"
cp -a "$PY_STDLIB" "$LIBDIR/"

# Nothing in the app imports these, and config-*/ carries a static libpython.
STDLIB_COPY="$LIBDIR/$(basename "$PY_STDLIB")"
rm -rf "$STDLIB_COPY/test" "$STDLIB_COPY/idlelib" "$STDLIB_COPY/tkinter" \
       "$STDLIB_COPY/turtledemo" "$STDLIB_COPY/ensurepip" "$STDLIB_COPY/lib2to3" \
       "$STDLIB_COPY"/config-*
find "$STDLIB_COPY" -name __pycache__ -type d -prune -exec rm -rf {} +

# --------------------------------------------------------------------------
# 3. Python dependencies: PyGObject/pycairo from the distro (they are C
#    extensions against the system GObject stack), the rest from PyPI.
# --------------------------------------------------------------------------
log "Bundling PyGObject and pycairo"
for pkg in gi cairo; do
    pkg_dir=$("$PYTHON_BIN" -c "import $pkg, os; print(os.path.dirname($pkg.__file__))" 2>/dev/null) \
        || die "$PYTHON_BIN cannot import $pkg (install python3-gi python3-gi-cairo)"
    cp -a "$pkg_dir" "$MODULEDIR/"
done
find "$MODULEDIR/gi" "$MODULEDIR/cairo" -name __pycache__ -type d -prune -exec rm -rf {} +

log "Installing Python dependencies"
# Straight from pyproject.toml, so this can never drift from what the app
# declares. PyGObject and pycairo are the two that must not come from PyPI.
"$PYTHON_BIN" - "$ROOT/pyproject.toml" > "$BUILD_DIR/requirements.txt" <<'PY'
import re
import sys
import tomllib

with open(sys.argv[1], "rb") as handle:
    project = tomllib.load(handle)["project"]

skip = {"pygobject", "pycairo"}
for dep in project["dependencies"]:
    name = re.match(r"[A-Za-z0-9._-]+", dep)
    if name and name.group(0).lower().replace("_", "-") not in skip:
        print(dep)
PY
"$PYTHON_BIN" -m pip install \
    --disable-pip-version-check \
    --no-compile \
    --no-warn-script-location \
    --upgrade \
    --target "$MODULEDIR" \
    -r "$BUILD_DIR/requirements.txt"
# pip --target leaves the console scripts of the dependencies behind; the app
# has no use for them and they carry absolute shebangs.
rm -rf "$MODULEDIR/bin"
find "$MODULEDIR" -name __pycache__ -type d -prune -exec rm -rf {} +

# --------------------------------------------------------------------------
# 4. Make the Meson install relocatable.
# --------------------------------------------------------------------------
log "Rewriting the launchers and build_config for a relocatable prefix"
"$PYTHON_BIN" - "$APPDIR" "$MODULEDIR" <<'PY'
"""Rewrite the absolute paths Meson baked in at install time.

Meson configures build_config.py and bin/sshpilot for --prefix=/usr, which
inside an AppImage names the *host's* install: an sshpilot installed from the
distro package would win over the bundled one, and PKGDATADIR would resolve to
its (differently versioned) gresource. Both are regenerated here to derive the
prefix from their own location instead.
"""
import os
import stat
import sys

appdir, moduledir = sys.argv[1], sys.argv[2]
prefix = os.path.join(appdir, "usr")
bindir = os.path.join(prefix, "bin")

package_dir = os.path.join(moduledir, "sshpilot")
rel_prefix_from_package = os.path.relpath(prefix, package_dir)
rel_moduledir_from_bin = os.path.relpath(moduledir, bindir)

with open(os.path.join(package_dir, "build_config.py"), "w", encoding="utf-8") as handle:
    handle.write(
        '"""Build-time paths, resolved relative to this file (AppImage build).\n\n'
        "The AppImage is mounted at a different path on every run, so the\n"
        "absolute prefix Meson writes here cannot be used.\n"
        '"""\n\n'
        "import os\n\n"
        "_PREFIX = os.path.abspath(\n"
        "    os.path.join(os.path.dirname(os.path.abspath(__file__)), %r)\n"
        ")\n\n"
        "PKGDATADIR = os.path.join(_PREFIX, 'share', 'io.github.mfat.sshpilot')\n"
        "LOCALEDIR = os.path.join(_PREFIX, 'share', 'locale')\n"
        % rel_prefix_from_package
    )

# The three entry points Meson installs, each re-pointed at the bundled
# package. sys.path[0] rather than [1]: the bundled app must win even if the
# host happens to have sshpilot installed too.
LAUNCHER = '''#!/usr/bin/env python3
"""Relocatable %(name)s launcher (AppImage build)."""
import os
import sys

_moduledir = os.path.normpath(
    os.path.join(os.path.dirname(os.path.realpath(__file__)), %(reldir)r)
)
if _moduledir not in sys.path:
    sys.path.insert(0, _moduledir)

from %(module)s import %(func)s as main

sys.exit(main())
'''

for name, module, func in (
    ("sshpilot", "sshpilot.main", "main"),
    ("sshpilot-agent", "sshpilot.sshpilot_agent", "main"),
    ("sshpilot-daemon", "sshpilot.daemon.cli", "main"),
):
    path = os.path.join(bindir, name)
    if not os.path.exists(path):
        continue
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(LAUNCHER % {
            "name": name,
            "reldir": rel_moduledir_from_bin,
            "module": module,
            "func": func,
        })
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
PY

# --------------------------------------------------------------------------
# 5. GObject introspection typelibs, and the libraries they name.
# --------------------------------------------------------------------------
log "Bundling typelibs"
# Every namespace the app calls gi.require_version() for, plus whatever those
# pull in transitively. WebKit is deliberately absent (see the header).
"$PYTHON_BIN" - > "$BUILD_DIR/typelibs.txt" <<'PY'
import sys

import gi

# Newer hosts (Ubuntu 26.04) ship only GLib's GIRepository-3.0; the Ubuntu 24.04
# floor ships gobject-introspection's 2.0. The two APIs differ slightly below.
try:
    gi.require_version("GIRepository", "3.0")
except ValueError:
    gi.require_version("GIRepository", "2.0")
from gi.repository import GIRepository

WANTED = [
    ("Gtk", "4.0"),
    ("Gdk", "4.0"),
    ("Gsk", "4.0"),
    ("Adw", "1"),
    ("Vte", "3.91"),
    ("GtkSource", "5"),
    ("Secret", "1"),
    # Pulls JavaScriptCore with it; both the PyXterm.js terminal backend and
    # the Host Info tab need them.
    ("WebKit", "6.0"),
    ("Gio", "2.0"),
    ("GLib", "2.0"),
    ("GLibUnix", "2.0"),
    ("GObject", "2.0"),
    ("GdkPixbuf", "2.0"),
    ("Pango", "1.0"),
    ("PangoCairo", "1.0"),
    ("PangoFT2", "1.0"),
    ("cairo", "1.0"),
    ("Graphene", "1.0"),
]

def lookup(namespace, version):
    """Return (typelib path, shared libraries, immediate dependencies)."""
    if gi.get_required_version("GIRepository") == "3.0":
        # A fresh repository per namespace: reusing one makes
        # get_shared_libraries() return the first namespace's libraries for
        # every later one (seen with GLib 2.88 / PyGObject 3.56).
        repo = GIRepository.Repository.new()
        repo.require(namespace, version, 0)
        libraries = repo.get_shared_libraries(namespace)
    else:
        repo = GIRepository.Repository.get_default()
        repo.require(namespace, version, 0)
        libraries = (repo.get_shared_library(namespace) or "").split(",")
    return (repo.get_typelib_path(namespace),
            [lib for lib in libraries if lib],
            repo.get_immediate_dependencies(namespace))


seen = set()
queue = list(WANTED)
while queue:
    namespace, version = queue.pop(0)
    if namespace in seen:
        continue
    seen.add(namespace)
    try:
        typelib, libraries, dependencies = lookup(namespace, version)
    except Exception as exc:  # a namespace this build's GTK does not ship
        print("SKIP\t%s-%s\t%s" % (namespace, version, exc), file=sys.stderr)
        continue
    print("%s\t%s" % (typelib, ",".join(libraries)))
    for dep in dependencies:
        dep_ns, _, dep_ver = dep.partition("-")
        queue.append((dep_ns, dep_ver))
PY

typelib_libs=()
while IFS=$'\t' read -r typelib shared; do
    [ -n "$typelib" ] || continue
    install -Dm644 "$typelib" "$TYPELIBDIR/$(basename "$typelib")"
    IFS=',' read -ra sonames <<<"$shared"
    for soname in "${sonames[@]}"; do
        if [ -n "$soname" ]; then
            typelib_libs+=("$soname")
        fi
    done
done < "$BUILD_DIR/typelibs.txt"
[ "${#typelib_libs[@]}" -gt 0 ] || die "no typelibs were resolved"

# --------------------------------------------------------------------------
# 6. Loadable modules GTK/GLib open by path rather than by DT_NEEDED.
# --------------------------------------------------------------------------
# Read once, not piped per lookup: awk exits at the first match, and a cache
# listing larger than the pipe buffer then kills ldconfig with SIGPIPE, which
# pipefail turns into a build failure.
LDCONFIG_CACHE=$(ldconfig -p)
resolve_soname() {
    awk -v name="$1" '$1 == name { print $NF; exit }' <<<"$LDCONFIG_CACHE"
}

SYS_LIBDIR=$(dirname "$(resolve_soname libglib-2.0.so.0)")
[ -d "$SYS_LIBDIR" ] || die "could not locate the system library directory"

log "Bundling gdk-pixbuf loaders and GIO modules"
cp -a "$SYS_LIBDIR/gdk-pixbuf-2.0/2.10.0/loaders/." "$PIXBUF_DIR/loaders/"
# dconf is how GSettings reaches the host's desktop settings -- without it
# there is no system dark-mode preference to follow.
for module in "$SYS_LIBDIR"/gio/modules/*.so; do
    [ -e "$module" ] && cp -a "$module" "$LIBDIR/gio/modules/"
done

log "Bundling the WebKitGTK helper processes"
# WebKitGTK renders out of process: libwebkitgtk itself is only half of it, and
# a WebView whose helpers cannot be exec'd never finishes loading. The paths
# are compiled into the library, so AppRun overrides them with WEBKIT_EXEC_PATH
# and WEBKIT_INJECTED_BUNDLE_PATH.
WEBKIT_SRC=$SYS_LIBDIR/webkitgtk-6.0
[ -d "$WEBKIT_SRC" ] || die "$WEBKIT_SRC missing (install gir1.2-webkit-6.0)"
WEBKIT_DIR=$LIBDIR/webkitgtk-6.0
mkdir -p "$WEBKIT_DIR/injected-bundle"
webkit_helpers=()
for helper in WebKitWebProcess WebKitNetworkProcess WebKitGPUProcess; do
    # GPUProcess is absent in some builds; the other two are not optional.
    if [ -x "$WEBKIT_SRC/$helper" ]; then
        install -Dm755 "$WEBKIT_SRC/$helper" "$WEBKIT_DIR/$helper"
        webkit_helpers+=("$WEBKIT_DIR/$helper")
    elif [ "$helper" != WebKitGPUProcess ]; then
        die "$WEBKIT_SRC/$helper missing"
    fi
done
# MiniBrowser is WebKit's own demo app, not something the bundle needs.
install -Dm644 "$WEBKIT_SRC/injected-bundle/libwebkitgtkinjectedbundle.so" \
    "$WEBKIT_DIR/injected-bundle/libwebkitgtkinjectedbundle.so"

# --------------------------------------------------------------------------
# 7. Shared libraries: the transitive closure of everything bundled so far,
#    minus what has to come from the host.
# --------------------------------------------------------------------------
log "Collecting shared libraries"
declare -A EXCLUDED=()
while read -r entry; do
    entry=${entry%%#*}
    entry=$(printf '%s' "$entry" | tr -d '[:space:]')
    [ -n "$entry" ] && EXCLUDED[$entry]=1
done < "$SCRIPT_DIR/excludelist"

declare -A COLLECTED=()
queue=("$APPDIR/usr/bin/python$PY_VER")
while IFS= read -r -d '' elf; do
    queue+=("$elf")
done < <(find "$MODULEDIR" "$STDLIB_COPY/lib-dynload" "$PIXBUF_DIR/loaders" \
              "$LIBDIR/gio/modules" "$WEBKIT_DIR" -name '*.so' -print0 2>/dev/null)
# The helpers are executables, not libraries, and pull in libraries of their own.
queue+=("${webkit_helpers[@]}")
for soname in "${typelib_libs[@]}"; do
    # A typelib naming a host library (HarfBuzz names libharfbuzz.so.0) is no
    # reason to bundle it: GI dlopen()s it by soname, so the host copy loads.
    [ -n "${EXCLUDED[$soname]:-}" ] && continue
    path=$(resolve_soname "$soname")
    [ -n "$path" ] || die "typelib names $soname but ldconfig cannot find it"
    [ -n "${COLLECTED[$soname]:-}" ] && continue
    COLLECTED[$soname]=1
    install -Dm644 "$(readlink -f "$path")" "$LIBDIR/$soname"
    queue+=("$LIBDIR/$soname")
done

while [ "${#queue[@]}" -gt 0 ]; do
    current=${queue[0]}
    queue=("${queue[@]:1}")
    while read -r name arrow path _; do
        [ "$arrow" = "=>" ] || continue
        case "$path" in /*) ;; *) continue ;; esac
        [ -n "${EXCLUDED[$name]:-}" ] && continue
        [ -n "${COLLECTED[$name]:-}" ] && continue
        COLLECTED[$name]=1
        # Copied under its SONAME: that is the name every DT_NEEDED uses.
        install -Dm644 "$(readlink -f "$path")" "$LIBDIR/$name"
        queue+=("$LIBDIR/$name")
    done < <(ldd "$current" 2>/dev/null || true)
done
echo "bundled ${#COLLECTED[@]} shared libraries"

# --------------------------------------------------------------------------
# 8. RPATHs. This is what lets AppRun skip LD_LIBRARY_PATH entirely -- and so
#    what keeps the terminal's shell, ssh and any host binary the app spawns
#    running against the host's libraries.
# --------------------------------------------------------------------------
log "Setting \$ORIGIN RPATHs"
set_rpath() {
    local elf=$1 rel
    rel=$(realpath --relative-to="$(dirname "$elf")" "$LIBDIR")
    if [ "$rel" = "." ]; then
        patchelf --set-rpath '$ORIGIN' "$elf" 2>/dev/null || true
    else
        patchelf --set-rpath "\$ORIGIN/$rel" "$elf" 2>/dev/null || true
    fi
}

set_rpath "$APPDIR/usr/bin/python$PY_VER"
for helper in "${webkit_helpers[@]}"; do
    set_rpath "$helper"
done
# MODULEDIR usually sits inside LIBDIR; sort -zu keeps that from patching the
# same file twice.
while IFS= read -r -d '' elf; do
    set_rpath "$elf"
done < <(find "$LIBDIR" "$MODULEDIR" \( -name '*.so' -o -name '*.so.*' \) -print0 2>/dev/null | sort -zu)

# --------------------------------------------------------------------------
# 8b. The C library, for hosts older than this build host. Done after the
#     RPATH pass on purpose: patchelf must never touch the loader or libc.
# --------------------------------------------------------------------------
log "Bundling the C library for hosts older than the build host"
# Everything above was linked against this host's glibc, so a host with an
# older one cannot run it natively. AppRun compares the two versions: on an
# older host it starts the interpreter and the WebKit helpers through the
# bundled loader instead, which then takes glibc -- and the C++ runtime, which
# is just as new -- from here. On anything as new as the build host nothing
# below is used, and the app runs on the host's C library exactly as before.
#
# The directory is reached only through the loader's --library-path, never an
# RPATH: a bundled libc.so.6 found by the *host's* loader would be a mixed
# glibc, which crashes.
LIBC_DIR=$LIBDIR/libc
mkdir -p "$LIBC_DIR"
LOADER=$(patchelf --print-interpreter "$APPDIR/usr/bin/python$PY_VER")
LOADER_NAME=$(basename "$LOADER")
install -Dm755 "$(readlink -f "$LOADER")" "$LIBC_DIR/$LOADER_NAME"
# HarfBuzz rides along: it is in the excludelist, but GTK and Pango here need
# HarfBuzz >= 7 and the hosts that get this directory can be older (Ubuntu
# 22.04 has 2.7). Newer hosts keep their own, as before.
for soname in libc.so.6 libm.so.6 libmvec.so.1 libpthread.so.0 libdl.so.2 \
              librt.so.1 libresolv.so.2 libutil.so.1 libanl.so.1 \
              libnss_files.so.2 libnss_dns.so.2 \
              libstdc++.so.6 libgcc_s.so.1 \
              libharfbuzz.so.0; do
    path=$(resolve_soname "$soname")
    if [ -z "$path" ]; then
        # Merged into libc.so.6 on some glibc versions; libc itself is not optional.
        [ "$soname" != libc.so.6 ] || die "ldconfig cannot find libc.so.6"
        continue
    fi
    install -Dm644 "$(readlink -f "$path")" "$LIBC_DIR/$soname"
done
LIBC_VERSION=$(getconf GNU_LIBC_VERSION | awk '{ print $2 }')
[ -n "$LIBC_VERSION" ] || die "getconf cannot report the glibc version"
printf '%s\n' "$LIBC_VERSION" > "$LIBC_DIR/VERSION"
echo "bundled glibc $LIBC_VERSION ($LOADER_NAME)"

# The loader cannot be named in PT_INTERP -- that has to be an absolute path,
# and the mount point changes on every run -- so each bundled executable gets a
# wrapper that runs it through the loader. --argv0 keeps the wrapper's own path
# in argv[0]: that is what Python reports as sys.executable, so every Python
# child the app starts (daemon, askpass, restarts) comes back through here.
write_libc_wrapper() {
    local wrapper=$1 target=$2 rel_libc rel_target
    rel_libc=$(realpath --relative-to="$(dirname "$wrapper")" "$LIBC_DIR")
    rel_target=$(realpath --relative-to="$(dirname "$wrapper")" "$target")
    mkdir -p "$(dirname "$wrapper")"
    cat > "$wrapper" <<EOF
#!/bin/sh
# Runs ${target#"$APPDIR"/} on the C library bundled in ${LIBC_DIR#"$APPDIR"/}.
# AppRun picks this only on hosts whose glibc is older than $LIBC_VERSION.
here=\$(dirname "\$0")
exec "\$here/$rel_libc/$LOADER_NAME" --library-path "\$here/$rel_libc" \\
    --argv0 "\$0" "\$here/$rel_target" "\$@"
EOF
    chmod 755 "$wrapper"
}

write_libc_wrapper "$APPDIR/usr/bin/python3-bundled-libc" "$APPDIR/usr/bin/python$PY_VER"
# WebKit exec's its helpers by name from WEBKIT_EXEC_PATH, so a directory of
# same-named wrappers is all it takes to move them onto the bundled libc.
for helper in "${webkit_helpers[@]}"; do
    write_libc_wrapper "$LIBDIR/webkitgtk-6.0-bundled-libc/$(basename "$helper")" "$helper"
done

# --------------------------------------------------------------------------
# 9. Data GTK looks up by path: schemas, icon themes, pixbuf loader cache.
# --------------------------------------------------------------------------
log "Bundling GSettings schemas and icon themes"
mkdir -p "$APPDIR/usr/share/glib-2.0/schemas"
cp -a /usr/share/glib-2.0/schemas/*.xml "$APPDIR/usr/share/glib-2.0/schemas/" 2>/dev/null || true
cp -a /usr/share/glib-2.0/schemas/*.gschema.override "$APPDIR/usr/share/glib-2.0/schemas/" 2>/dev/null || true
glib-compile-schemas "$APPDIR/usr/share/glib-2.0/schemas" >/dev/null

mkdir -p "$APPDIR/usr/share/icons"
[ -d /usr/share/icons/Adwaita ] || die "adwaita-icon-theme is not installed"
cp -a /usr/share/icons/Adwaita "$APPDIR/usr/share/icons/"
install -Dm644 /usr/share/icons/hicolor/index.theme \
    "$APPDIR/usr/share/icons/hicolor/index.theme"

# The cache has to name the loaders by absolute path, and the mount point is
# only known at run time -- AppRun expands @APPDIR@ into a per-user copy.
QUERY_LOADERS=$SYS_LIBDIR/gdk-pixbuf-2.0/gdk-pixbuf-query-loaders
[ -x "$QUERY_LOADERS" ] || QUERY_LOADERS=$(command -v gdk-pixbuf-query-loaders || true)
[ -x "$QUERY_LOADERS" ] || die "gdk-pixbuf-query-loaders not found"
"$QUERY_LOADERS" "$PIXBUF_DIR/loaders/"*.so > "$PIXBUF_DIR/loaders.cache.in"
sed -i "s|$APPDIR|@APPDIR@|g" "$PIXBUF_DIR/loaders.cache.in"

# --------------------------------------------------------------------------
# 10. AppDir metadata.
# --------------------------------------------------------------------------
log "Assembling AppDir metadata"
install -Dm755 "$SCRIPT_DIR/AppRun" "$APPDIR/AppRun"
install -Dm644 "$APPDIR/usr/share/applications/$APP_ID.desktop" "$APPDIR/$APP_ID.desktop"
install -Dm644 "$APPDIR/usr/share/icons/hicolor/scalable/apps/$APP_ID.svg" "$APPDIR/$APP_ID.svg"
ln -sf "$APP_ID.svg" "$APPDIR/.DirIcon"
if command -v desktop-file-validate >/dev/null; then
    desktop-file-validate "$APPDIR/$APP_ID.desktop"
fi

# --------------------------------------------------------------------------
# 11. Smoke-test the bundle before packaging it.
# --------------------------------------------------------------------------
log "Smoke-testing the AppDir"
# The environment comes from AppRun itself, with only its final exec line
# stripped, so this tests what users actually get and the two can never drift.
grep -v '^exec ' "$APPDIR/AppRun" > "$BUILD_DIR/apprun-env.sh"

# env -i: prove the bundle stands on its own, with none of the build host's GTK,
# GI or Python environment leaking in. DISPLAY, when the caller has one, turns
# on the WebKit render check below -- run the build under xvfb-run to get it in
# CI.
#
# It runs twice, once per C library AppRun can choose. The build host is never
# older than itself, so the bundled-libc run is forced -- it is the only run
# the build gets of what users on older distros start.
smoke_test() {
    log "Smoke test on the $1 C library"
    env -i \
        HOME="${HOME:-/tmp}" \
        PATH=/usr/bin:/bin \
        APPDIR="$APPDIR" \
        SSHPILOT_APPIMAGE_LIBC="$1" \
        ${DISPLAY:+DISPLAY="$DISPLAY"} \
        ${XAUTHORITY:+XAUTHORITY="$XAUTHORITY"} \
        bash -c '. "$1"; shift; exec "$SSHPILOT_PYTHON" "$@"' _ "$BUILD_DIR/apprun-env.sh" \
        - "$APPDIR" < "$BUILD_DIR/smoke-test.py"
}

cat > "$BUILD_DIR/smoke-test.py" <<'PY'
import os
import subprocess
import sys

appdir = sys.argv[1]
assert sys.executable.startswith(appdir), sys.executable

# Which libc.so.6 is really mapped, not which one AppRun meant to pick.
with open("/proc/self/maps", encoding="utf-8") as maps:
    libc_paths = {line.split()[-1] for line in maps if line.rstrip().endswith("/libc.so.6")}
bundled_libc = os.path.join(appdir, "usr", "lib", "libc", "libc.so.6")
if os.environ["SSHPILOT_APPIMAGE_LIBC"] == "bundled":
    assert libc_paths == {bundled_libc}, libc_paths
    # sys.executable is what the app hands its children (daemon, askpass), so
    # it has to be the wrapper, or they would start on the host's libc again.
    assert sys.executable.endswith("/python3-bundled-libc"), sys.executable
else:
    assert bundled_libc not in libc_paths, libc_paths
child = subprocess.run(
    [sys.executable, "-c",
     "import gi; gi.require_version('Gtk', '4.0'); from gi.repository import Gtk"],
    capture_output=True, text=True,
)
assert child.returncode == 0, child.stderr

# Everything below runs under AppRun's environment, so these are AppRun's
# paths, not ones this test invented.
for variable, expected in (
    ("GI_TYPELIB_PATH", "Gtk-4.0.typelib"),
    ("GSETTINGS_SCHEMA_DIR", "gschemas.compiled"),
    ("GIO_MODULE_DIR", None),
    ("WEBKIT_EXEC_PATH", "WebKitWebProcess"),
    ("WEBKIT_INJECTED_BUNDLE_PATH", "libwebkitgtkinjectedbundle.so"),
    ("PYXTERMJS_ASSETS_DIR", "xterm.js"),
):
    value = os.environ.get(variable)
    assert value and value.startswith(appdir), "%s=%r" % (variable, value)
    assert os.path.isdir(value), "%s=%r is not a directory" % (variable, value)
    if expected:
        assert os.path.exists(os.path.join(value, expected)), \
            "%s has no %s" % (variable, expected)

import gi

for namespace, version in (
    ("Gtk", "4.0"), ("Gdk", "4.0"), ("Adw", "1"), ("Vte", "3.91"),
    ("GtkSource", "5"), ("Secret", "1"), ("GLibUnix", "2.0"),
    ("PangoFT2", "1.0"), ("WebKit", "6.0"),
):
    gi.require_version(namespace, version)
from gi.repository import (  # noqa: F401
    Adw, Gdk, Gio, GLib, Gtk, GtkSource, Secret, Vte, WebKit,
)

# The dependencies the app imports at runtime.
import cairo, certifi, cryptography, keyring, psutil, yaml  # noqa: F401,E401

import sshpilot
from sshpilot.build_config import PKGDATADIR

assert sshpilot.__file__.startswith(appdir), sshpilot.__file__
gresource = os.path.join(PKGDATADIR, "io.github.mfat.sshpilot.gresource")
assert os.path.exists(gresource), gresource
Gio.resources_register(Gio.Resource.load(gresource))

# The app icon is an SVG, so the svg pixbuf loader has to be usable.
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf

formats = {fmt.get_name() for fmt in GdkPixbuf.Pixbuf.get_formats()}
assert "svg" in formats, sorted(formats)

# The xterm.js assets the PyXterm.js backend renders. AppRun's
# PYXTERMJS_ASSETS_DIR is what keeps this off the host's libjs-xterm.
from sshpilot.xterm_shell import asset_dir, build_shell_html

assert asset_dir().startswith(appdir), asset_dir()
assert os.path.isfile(os.path.join(asset_dir(), "xterm.js")), asset_dir()
shell_html = build_shell_html()

# Importing WebKit only proves the library loads. The renderer runs in a
# separate process that WebKit exec's from a path compiled in at build time, so
# the check that matters is whether a real WebView finishes a load -- if
# WEBKIT_EXEC_PATH is wrong, this hangs until the timeout instead. Needs a
# display, so it is skipped when there is none (the build still fails on a
# WebKit that cannot even be imported).
if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
    Gtk.init()
    window = Gtk.Window()
    webview = WebKit.WebView()
    window.set_child(webview)
    window.present()

    outcome = {}
    loop = GLib.MainLoop()

    def _on_load_changed(_view, event):
        if event == WebKit.LoadEvent.FINISHED:
            outcome["loaded"] = True
            loop.quit()

    def _on_timeout():
        outcome.setdefault("loaded", False)
        loop.quit()
        return False

    webview.connect("load-changed", _on_load_changed)
    GLib.timeout_add_seconds(90, _on_timeout)
    webview.load_html(shell_html, "file:///")
    loop.run()
    assert outcome.get("loaded"), (
        "the bundled WebKit never finished loading the xterm.js shell -- "
        "check WEBKIT_EXEC_PATH and the WebKit*Process helpers"
    )
    print("smoke test ok: WebKit rendered the xterm.js shell")
else:
    print("smoke test: no display, WebKit checked by import only")

print("smoke test ok: sshpilot %s" % sshpilot.__version__)
PY
smoke_test host
smoke_test bundled

# --------------------------------------------------------------------------
# 12. Pack it.
# --------------------------------------------------------------------------
log "Building the AppImage"
APPIMAGETOOL=${APPIMAGETOOL:-$BUILD_DIR/appimagetool}
if [ ! -x "$APPIMAGETOOL" ]; then
    # AppImage/appimagetool rather than the archived AppImageKit: the runtime
    # it embeds uses libfuse3. The old one needs libfuse2, which none of the
    # distros new enough to run this AppImage (glibc >= the build host's)
    # installs by default, so those users would get "error loading
    # libfuse.so.2" instead of an app.
    wget -q -O "$APPIMAGETOOL" \
        "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-$ARCH.AppImage"
    chmod +x "$APPIMAGETOOL"
fi

OUTPUT="$OUTDIR/sshpilot-$VERSION-linux-$ARCH.AppImage"
# Runners have no FUSE; extracting the tool is the supported way around it.
APPIMAGE_EXTRACT_AND_RUN=1 ARCH="$ARCH" "$APPIMAGETOOL" "$APPDIR" "$OUTPUT"

log "Built $OUTPUT ($(du -h "$OUTPUT" | cut -f1))"
