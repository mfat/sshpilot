# Phase 14 packaged runtime validation

## Status

```text
Packaged runtime: NOT RUN
```

No installed Flatpak/Debian/RPM/macOS package was built and validated end-to-end in this Phase 14.1 pass.

Source-tree GTK proof (`tests/gui/`, `tests/manual/phase14_production_smoke.py`) is **not** a substitute for packaged readiness.

## Required checks (outstanding)

* application launches from installed package
* daemon executable found inside sandbox/runtime
* daemon starts; socket accessible
* terminal opens through daemon; real VTE receives output
* authentication + host-key interaction
* file manager opens through daemon; listing appears
* upload/download work
* quit policy works
* daemon cleanup works
* no host production daemon interference

## Preferred first target

Flatpak (`flathub/io.github.mfat.sshpilot.yaml` / in-tree `flatpak/`)

## Verdict

```text
NOT READY
```
