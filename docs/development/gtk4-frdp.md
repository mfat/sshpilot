# gtk4-frdp: the RDP tab's library

RDP connections open in a tab through [gtk4-frdp](https://github.com/mfat/gtk4-frdp),
our own GTK 4 widget around FreeRDP 3. No distribution packages it, so sshPilot
ships it in every build:

| Build | Where gtk4-frdp comes from |
|---|---|
| deb, rpm, Arch, Flatpak | `subprojects/gtk4-frdp`, compiled by sshPilot's Meson build (option `rdp_tab`, default `auto`) into `<libdir>/sshpilot`, private to the app |
| AppImage, macOS | `subprojects/gtk4-frdp`, compiled by `scripts/build-gtk4-frdp.sh` |
| Windows | a prebuilt zip from the gtk4-frdp release, pinned in `scripts/fetch-gtk.py` |
| Running from a checkout | `subprojects/gtk4-frdp`, compiled by `scripts/build-rdp-tab.sh` into `build/rdp-tab` |

`subprojects/gtk4-frdp` is a copy of the source, not a download at build time,
because Launchpad and COPR build without network access. Its `VENDORED` file
records the repository, commit and release tag it came from.

## Updating it

The bundled source currently carries a local Windows navigation-key fix in
`frdp_session_send_key`: Win32 virtual keys for arrows, Home/End, Page Up/Down,
Insert and Delete need WinPR's `KBDEXT` marker before scan-code lookup.
Preserve this fix when updating until it is included upstream. Windows uses
the prebuilt DLL, so source edits alone do not fix Windows installations:
release gtk4-frdp with the fix and update the Windows zip pin using the steps
below. Verify both key presses and releases against a Linux RDP host, with
Num Lock on and off, as well as ordinary typing and keypad digits.

1. In gtk4-frdp: date the release in `NEWS.md`, bump `version` in
   `meson.build`, and publish the release (e.g. `gh release create v0.2.0`).
   Its CI builds the Windows zip and attaches it to the release.
2. Wait for that CI run's `windows-gvsbuild` job to finish.
3. In sshPilot:

   ```bash
   scripts/update-gtk4-frdp.sh v0.2.0
   ```

   This replaces `subprojects/gtk4-frdp` with the tag's source and pins the
   Windows build to the same release's zip, checksum included. It refuses to
   change anything if the zip is not attached yet.
4. Commit the result. The next sshPilot release ships it in every build.
5. For your own source runs, re-run `scripts/build-rdp-tab.sh`.

`tests/test_gtk4_frdp_pin.py` fails when the bundled copy and the Windows pin
disagree, or when the bundled copy is not a release.

To try an unreleased gtk4-frdp change, `scripts/update-gtk4-frdp.sh --commit
<full-sha>` vendors a commit and leaves the Windows pin alone. The pin test
then fails until a release tag replaces it, so it cannot ship by accident.
