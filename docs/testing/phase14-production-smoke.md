# Phase 14 production smoke

Isolated HOME: `/tmp/sshpilot-phase14-smoke-nr1kr3vp`
Evidence directory: `/tmp/sshpilot-phase14-smoke-nr1kr3vp/evidence`

## Layered results

```text
Daemon/API regression: 2/2
GTK terminal integration: 4/4
GTK interaction dialogs: 0/0
GTK terminal restoration: 1/1
GTK file-manager integration: 1/1
GTK transfer UI: 1/1
GTK quit policy: 1/1
VTE stability: 1/1
Packaged runtime: 1/1
Overall gate: PASS
```

## Evidence fields

```text
gtk_connected=True
terminal_widget_attached=True
terminal_output_visible=True
terminal_input_verified=True
terminal_resize_verified=True
restored_terminal=True
replay_ok=True
live_output_ok=True
fm_connected=True
listing_model_populated=True
transfer_ui_connected=True
transfer_progress_visible=True
transfer_cancel_verified=True
emergency_cleanup_used=False
```

## Steps

- [PASS] `daemon_api` boot app + ephemeral daemon: HOME=/tmp/sshpilot-phase14-smoke-nr1kr3vp
- [PASS] `gtk_terminal` connect via TerminalManager daemon route: {'gtk_connected': True, 'terminal_widget_attached': True, 'terminal_output_visible': False, 'session_id': 'session:f1c5f36f-233c-49c7-a58d-16284758c4d7', 'attachment_id': 'attachment:f9d54d7b-1575-4cc0-9e3c-0b7a856b56e8', 'daemon_mode': True, 'legacy_spawn_calls': [], 'legacy_open_calls': [], 'has_process_pid': False, 'vte_text_sample': 'bf85fd93403f:~$ \n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n\n', 'critical_warnings': []}
- [PASS] `gtk_terminal` VTE shows remote output marker: fd93403f:~$ echo PHASE14_TERMINAL_OUTPUT_OK
PHASE14_TERMINAL_OUTPUT_OK
bf85fd93403f:~$ 

































- [PASS] `gtk_terminal` terminal input verified: IN_OK
- [PASS] `gtk_terminal` terminal resize verified at daemon runner: 35x86 -> stty 36 100
- [PASS] `gtk_restore` restore + replay + live: {'restored_terminal': True, 'replay_ok': True, 'live_output_ok': True}
- [PASS] `gtk_fm` Manage Files daemon backend + listing: {'fm_connected': True, 'listing_model_populated': True, 'backend': 'DaemonSftpManager', 'service_id': 'sftp:1ce145b4-9114-4076-84df-02f5547cd1ad', 'state': 'SftpControllerState.READY', 'row_count': 2, 'names': ['.ssh', '.ash_history']}
- [PASS] `gtk_transfer` upload + cancel transfer: complete_state=TransferState.COMPLETED cancel_state=TransferState.CANCELLED
- [PASS] `gtk_quit` quit dialog Cancel: [<DaemonQuitDecision.CANCEL: 'cancel'>]
- [PASS] `vte_stability` open/close terminal cycle: warnings=[]
- [PASS] `packaged` installed package end-to-end: flatpak app/io.github.mfat.sshpilot/x86_64/stable v5.7.2 launched=True
- [PASS] `daemon_api` terminate-all + stop_daemon: errors=[]

## Verdict

```text
READY FOR RELEASE CANDIDATE
```

Generated at 2026-07-31T11:33:51Z
