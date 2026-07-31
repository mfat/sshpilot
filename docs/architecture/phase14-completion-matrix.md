# Phase 14 completion matrix

Distinguishes daemon API proof from production GTK widgets, manual smoke, and packaged runtime.

| Capability | Daemon API | Production GTK controller | Actual widget | Manual smoke | Packaged runtime |
| --- | --- | --- | --- | --- | --- |
| Terminal open (daemon route) | PASS (`test_terminal_api_integration`) | PASS (`TerminalManager`) | PASS (VTE `TerminalWidget`) | PASS when connect stable | NOT RUN |
| Terminal output in VTE | N/A | PASS (`_on_daemon_output`) | PASS | PASS | NOT RUN |
| Terminal input | PASS (API) | PASS (`feed_child_data`) | PASS | PASS | NOT RUN |
| Terminal resize | PASS (API) | PASS | PASS | PASS | NOT RUN |
| Session restore + replay | PASS (metadata) | PASS (`DaemonSessionRestoreManager`) | PASS | PASS | NOT RUN |
| Host-key / password dialogs | PASS (broker) | PASS (`DaemonInteractionDialogs`) | PARTIAL (smoke uses auth helper) | PARTIAL | NOT RUN |
| File manager listing | PASS (SFTP API) | PASS (`DaemonSftpManager`) | PASS (FilePane model) | FLAKY after restore | NOT RUN |
| Transfer progress UI | PASS (transfer API) | PARTIAL | PARTIAL | PARTIAL | NOT RUN |
| Quit keep/terminate/cancel | PASS (policy) | PASS (`daemon_quit_policy`) | PASS (Adw dialog) | PARTIAL | NOT RUN |
| VTE stability | N/A | PASS under xvfb/cairo | PASS (no abort in suite) | PARTIAL | NOT RUN |

## Overall

```text
NOT READY
```

Blockers: packaged runtime not proven; interaction dialogs not fully exercised without auth helper; transfer UI progress row not fully asserted; Phase 14 smoke still flaky on combined restore→FM sequencing.
