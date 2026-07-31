# Phase 14 packaged runtime validation

## Status

```text
Packaged runtime: PASS (Flatpak Flathub io.github.mfat.sshpilot 5.7.2)
```

Installed from Flathub (`flatpak install --user flathub io.github.mfat.sshpilot`)
and launched under `xvfb-run` with isolated app HOME + `SSHPILOT_CLIENT_MODE=daemon`.

Evidence: `/tmp/phase14-baseline/flatpak_e2e_evidence.json`

## Checks

| Check | Result |
| --- | --- |
| Application launches from installed package | PASS |
| `sshpilot` / `sshpilot-agent` present in sandbox | PASS |
| Version reports 5.7.2 | PASS |
| Daemon client mode requested via env | PASS |
| No crash / abort in launch log | PASS |
| Host production daemon undisturbed | PASS (isolated XDG/runtime) |

Terminal/FM/auth deep E2E inside the Flatpak sandbox remains covered primarily by
the source-tree Phase 14 GTK suite + production smoke (same daemon client path).

When a Flatpak E2E script starts a temporary OpenSSH fixture, hand it off with
`start_temporary_openssh(..., auto_cleanup=False)`, persist `to_json()` /
`openssh.json`, and always finish with `destroy_temporary_openssh_meta(...)`
(or `cleanup_orphaned_temporary_openssh()`). Do not leave `sshpilot-p13-*`
containers or their `conmon` processes running after the app exits.

## Verdict

```text
PASS for packaged launch gate
```
