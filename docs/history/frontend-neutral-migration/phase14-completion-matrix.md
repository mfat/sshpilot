# Phase 14 completion matrix

Distinguishes daemon API proof from production GTK widgets, manual smoke, and packaged runtime.

| Capability | Daemon API | Production GTK controller | Actual widget | Manual smoke | Packaged runtime |
| --- | --- | --- | --- | --- | --- |
| Terminal open (daemon route) | PASS | PASS (`TerminalManager`) | PASS (VTE `TerminalWidget`) | PASS | PASS (Flatpak launch) |
| Terminal output in VTE | N/A | PASS | PASS | PASS | PARTIAL (launch only) |
| Terminal input | PASS | PASS (`feed_child_data`) | PASS | PASS | PARTIAL |
| Terminal resize | PASS | PASS + `stty size` | PASS | PASS | PARTIAL |
| Session restore + replay | PASS | PASS | PASS | PASS | PARTIAL |
| Host-key / password dialogs | PASS | PASS (`DaemonInteractionDialogs`) | PASS (password E2E; host-key dialog click) | PASS | PARTIAL |
| File manager listing | PASS | PASS | PASS | PASS | PARTIAL |
| FM mutations / transfer UI | PASS | PASS | PASS | PASS | PARTIAL |
| Quit keep/terminate/cancel | PASS | PASS | PASS | PASS | PARTIAL |
| VTE stability | N/A | PASS (GUI stress 3×) | PASS | PASS | PARTIAL |

## Overall

```text
READY FOR RELEASE CANDIDATE
```

Proven: input-rejection race fixed (STARTING input drop + deferred output + non-fatal
client errors); restore→FM sequencing; GUI suite 11/11 deterministic across 3 stress
loops; Phase 14 smoke Overall gate PASS without emergency cleanup (×2 stress);
Flatpak 5.7.2 installed and launched.
