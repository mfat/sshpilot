# gtkghostty: the opt-in terminal widget

The "gtkghostty (experimental)" terminal backend uses
[gtkghostty](https://github.com/mfat/gtkghostty), our own GTK 4 widget on
Ghostty's libghostty-vt. No distribution packages either, so sshPilot ships them
where it can build them:

| Build | Where gtkghostty comes from |
|---|---|
| deb and rpm built in GitHub CI, Arch | `subprojects/gtkghostty`, compiled by sshPilot's Meson build (option `gtkghostty`) into `<libdir>/sshpilot` with a copy of libghostty-vt, private to the app |
| Flatpak | `subprojects/gtkghostty`, compiled by the Meson build against a `libghostty-vt` module the manifest builds with the Zig SDK extension (`org.freedesktop.Sdk.Extension.ziglang`) |
| AppImage | `subprojects/gtkghostty`, compiled with libghostty-vt by `scripts/build-gtkghostty.sh` |
| macOS (DMG) | `subprojects/gtkghostty`, compiled by `scripts/build-gtkghostty.sh` against the static libghostty-vt in Ghostty's official XCFramework (no Zig) |
| Windows | a prebuilt zip from the gtkghostty release, pinned in `scripts/fetch-gtk.py` |
| Launchpad PPA, COPR | not included: the backend is not offered |

libghostty-vt 1.0 needs Zig 0.17 to build.
`packaging/windows/libghostty-vt/build.py --target native --prefix DIR` downloads
it (pinned, checksummed) and installs libghostty-vt into DIR; the CI builds then
set `PKG_CONFIG_PATH=DIR/lib/pkgconfig`. Arch builds it with its own Zig from the
release tarballs (`packaging/ArchLinux/PKGBUILD`), and so does the Flatpak with
Flathub's Zig extension. Launchpad and COPR build
without network access, and Ubuntu and Fedora have no Zig 0.17, so those builds
leave the widget out; with option `gtkghostty=auto` that is not an error.

`subprojects/gtkghostty` is a copy of the source for the same reason as
gtk4-frdp's. Its `VENDORED` file records the repository, commit and release tag.

## Updating it

1. In gtkghostty: bump `version` in `meson.build` and publish the release
   (`gh release create v0.2.0`). Its CI builds the Windows zip and attaches it.
2. Wait for that CI run's `windows-gvsbuild` job to finish.
3. In sshPilot: `scripts/update-gtkghostty.sh v0.2.0`. This replaces
   `subprojects/gtkghostty` with the tag's source and pins the Windows build to
   the same release's zip, checksum included.
4. Commit the result.

`tests/test_gtkghostty_pin.py` fails when the bundled copy and the Windows pin
disagree, or when the bundled copy is not a release. `scripts/update-gtkghostty.sh
--commit <full-sha>` vendors an unreleased commit for testing; the pin test then
fails until a release replaces it.

## Running it from a checkout

Build libghostty-vt and the widget, then point GI and the loader at them:

```bash
python3 packaging/windows/libghostty-vt/build.py --target native --prefix ~/.local/opt/ghostty-vt
PKG_CONFIG_PATH=~/.local/opt/ghostty-vt/lib/pkgconfig meson setup build/gtkghostty subprojects/gtkghostty
ninja -C build/gtkghostty
GI_TYPELIB_PATH=build/gtkghostty/src \
LD_LIBRARY_PATH=build/gtkghostty/src:$HOME/.local/opt/ghostty-vt/lib python3 run.py
```
