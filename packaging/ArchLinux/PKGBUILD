# Maintainer: mfat <https://github.com/mfat>
# Contributor: Patrick Northon <northon_patrick3@yahoo.ca>

pkgname=sshpilot
pkgver=6.4.3
pkgrel=1
pkgdesc='SSH connection manager with integrated terminal, tunneling, tabbed interface and scp upload support.'
url='https://github.com/mfat/sshpilot'
# LGPL/MIT: the bundled gtkghostty terminal widget and libghostty-vt.
license=('GPL-3.0-only' 'LGPL-2.1-or-later' 'MIT')
# Not 'any': gtk4-frdp (RDP tab) and gtkghostty (terminal widget) are compiled
# into the package, privately in /usr/lib/sshpilot.
arch=('x86_64' 'aarch64')
# Needed to build as well as to run: Meson looks up Python, and the bundled
# libraries link against GTK 4, libadwaita and FreeRDP 3.
depends=('libadwaita' 'python' 'gtk4' 'freerdp')
# zig: builds libghostty-vt for the gtkghostty widget. libghostty-vt 1.0.0
# needs Zig 0.17.x; Arch ships one Zig at a time, so the bound turns a future
# Zig update into a clear dependency error instead of a compile failure.
makedepends=('meson' 'ninja' 'blueprint-compiler' 'glib2-devel' 'gettext'
             'desktop-file-utils' 'appstream' 'gobject-introspection'
             'zig>=0.17' 'zig<0.18')
# Built-in protocol plugins shell out to these; all degrade gracefully if absent.
optdepends=('inetutils: Telnet protocol support'
            'mosh: Mosh protocol support'
            'kubectl: Kubernetes protocol support'
            'docker: Docker/Podman protocol support'
            'picocom: Serial console protocol support'
            'python-pykeepass: KeePass (.kdbx) secret backend')
_ghostty_vt=1.0.0
source=("${pkgname}-${pkgver}.tar.gz::${url}/archive/refs/tags/v${pkgver}.tar.gz"
        "https://release.files.ghostty.org/libghostty-vt/${_ghostty_vt}/libghostty-vt-${_ghostty_vt}.tar.gz"
        "https://release.files.ghostty.org/libghostty-vt/${_ghostty_vt}/libghostty-vt-${_ghostty_vt}-deps.tar.gz")
sha256sums=('163fbc820b28d06bb52b03e1b01975ba9675e10f2169444a21bf5545bec1a037'
            '4a6ef758be721923e1969e7b81b443dcc92d8dd3fb5a39600c03a8817a753676'
            '4d5ea5e448e4c7ea8fb55ccbaa8668f2a97d44f438cee831e2b7ecd162d85c29')

build() {
	# libghostty-vt, offline from its release and dependency tarballs, into a
	# prefix the gtkghostty widget builds against; meson then bundles the
	# library (and its licence) in /usr/lib/sshpilot.
	local _vt="${srcdir}/ghostty-vt"
	(cd "libghostty-vt-${_ghostty_vt}" &&
		ZIG_GLOBAL_CACHE_DIR="${srcdir}/zig-cache" zig build --prefix "${_vt}" \
			--system "${srcdir}/libghostty-vt-${_ghostty_vt}-deps" \
			-Doptimize=ReleaseFast -Dcpu=baseline -Dstrip=true)
	install -Dm644 "libghostty-vt-${_ghostty_vt}/LICENSE" "${_vt}/share/licenses/libghostty-vt/LICENSE"
	install -Dm644 "libghostty-vt-${_ghostty_vt}/deps/libghostty-vt-third-party.json" \
		"${_vt}/share/licenses/libghostty-vt/third-party.json"

	# enabled: fail here rather than quietly ship without RDP tabs or gtkghostty.
	PKG_CONFIG_PATH="${_vt}/lib/pkgconfig" arch-meson "${pkgname}-${pkgver}" build \
		-Drdp_tab=enabled -Dgtkghostty=enabled
	meson compile -C build
}

check() {
	# Validates the .desktop entry and the AppStream metainfo.
	meson test -C build --print-errorlogs
}

package() {
	# Only needed at run time.
	depends+=('python-gobject' 'python-cairo' 'python-cryptography'
	          'python-keyring' 'python-psutil' 'python-yaml' 'openssh'
	          'libsecret' 'vte4' 'gtksourceview5' 'sshpass'
	          'webkitgtk-6.0')

	meson install -C build --destdir "${pkgdir}"
	# The bundled gtk4-frdp (RDP tab) lives privately in /usr/lib/sshpilot;
	# its unversioned .so link is a development file.
	rm -f "${pkgdir}/usr/lib/sshpilot/libgtk4-frdp-4.0.so"
	install -Dm644 "${pkgname}-${pkgver}/subprojects/gtk4-frdp/NOTICE" \
		"${pkgdir}/usr/share/licenses/${pkgname}/gtk4-frdp-NOTICE"
	# The bundled gtkghostty widget, likewise private; libghostty-vt's MIT
	# licence is installed next to it (usr/lib/sshpilot/licenses).
	rm -f "${pkgdir}/usr/lib/sshpilot/libgtkghostty-1.so"
	install -Dm644 "${pkgname}-${pkgver}/subprojects/gtkghostty/NOTICE" \
		"${pkgdir}/usr/share/licenses/${pkgname}/gtkghostty-NOTICE"
}
