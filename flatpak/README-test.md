Test Flatpak

This folder contains a test manifest and helper script to build a local Flatpak variant of sshPilot under the App ID `io.github.mfat.sshpilot.test`.

Build & install (local):

```bash
# From repo root
flatpak/build-and-install-test.sh
```

Notes:
- The script requires `flatpak` and `flatpak-builder` to be installed on your system.
- The test Flatpak is installed for the current user under the remote name `sshpilot-test`.
- To uninstall: `flatpak uninstall --user io.github.mfat.sshpilot.test && flatpak remote-delete --user sshpilot-test`.
