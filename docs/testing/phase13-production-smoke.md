# Phase 13.2 production path smoke

Isolated HOME: `/tmp/sshpilot-phase13-smoke-xzuya9fb`
Evidence directory: `/tmp/sshpilot-phase13-smoke-xzuya9fb/evidence`

## Layered results

```text
Daemon/API: 21/21
GTK controller: 19/19
Widget interaction: 0/0
Overall gate: PASS
```

Layer A = ephemeral daemon + DaemonClient production APIs (no VTE required).
Layer B = GTK controllers / ConnectionManager / BackupManager / restart rediscovery.
Layer C = visible widget clicks (not required for this gate; VTE opt-in via
`SSHPILOT_SMOKE_GTK_TERMINAL=1` — see `gtk-vte-bloom-filter-crash.md`).

| step | layer | action | expected result | actual result | pass/fail | evidence |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | B | Start SSH Pilot (real GTK, isolated HOME) | MainWindow presented | window=MainWindow pages=1 | PASS | HOME=/tmp/sshpilot-phase13-smoke-xzuya9fb |
| 2 | B | Existing connections load (ConnectionManager) | Connection list loads without error | count=0 | PASS | count=0 |
| 3 | B | Connection create via ConnectionManager.add_connection_from_data | Connection P13Create exists | P13Create | PASS | P13Create |
| 4 | B | Connection edit via ConnectionManager.update_connection | Username/port persisted | user=phase13 port=53491 | PASS | user=phase13 port=53491 |
| 5 | B | Group move via GroupManager.move_connection | Connection primary group is P13Group | a47ba1dd-fd49-419b-b199-031355f7b6ac | PASS | a47ba1dd-fd49-419b-b199-031355f7b6ac |
| 6 | B | Reorder via GroupManager.reorder_connection_in_group | Order changes | ['1334f32c-38b6-4b54-862c-8ccfd0be7afc', '912cc793-bfda-4f11-bcab-e7840261e795'] -> ['912cc793-bfda-4f11-bcab-e7840261e795', '1334f32c-38b6-4b54-862c-8ccfd0be7afc'] | PASS | ['1334f32c-38b6-4b54-862c-8ccfd0be7afc', '912cc793-bfda-4f11-bcab-e7840261e795'] -> ['912cc793-bfda-4f11-bcab-e7840261e795', '1334f32c-38b6-4b54-862c-8ccfd0be7afc'] |
| 7 | B | Duplicate via ConnectionManager.duplicate_connection | Duplicate created with new nickname | P13Create-Copy | PASS | P13Create-Copy |
| 8 | B | Delete via ConnectionManager.remove_connection | P13DeleteMe removed | still_present=False | PASS | still_present=False |
| 9 | A | Password login via daemon client.open_session (SessionRuntime) | SSH session reaches RUNNING on daemon path | session_state=SessionState.RUNNING gtk_connected=False session_ok=True | PASS | session_state=SessionState.RUNNING gtk_connected=False session_ok=True |
| 10 | A | Public-key login via daemon client.open_session | SSH session reaches RUNNING on daemon path | session_state=SessionState.RUNNING gtk_connected=False session_ok=True | PASS | session_state=SessionState.RUNNING gtk_connected=False session_ok=True |
| 11 | A | Encrypted-key passphrase login via daemon client.open_session | SSH session reaches RUNNING on daemon path | session_state=SessionState.RUNNING gtk_connected=False session_ok=True | PASS | session_state=SessionState.RUNNING gtk_connected=False session_ok=True |
| 12 | A | Host-key confirmation via daemon InteractionType.HOST_KEY_CONFIRMATION | First-use host key accepted; session RUNNING | session_state=SessionState.RUNNING gtk_connected=False session_ok=True | PASS | session_state=SessionState.RUNNING gtk_connected=False session_ok=True |
| 13 | A | Prompt cancellation via daemon SecretDecision.CANCEL on password interaction | Password prompt cancelled; session does not stay connected | session_state=SessionState.CLOSED gtk_connected=False session_ok=True | PASS | session_state=SessionState.CLOSED gtk_connected=False session_ok=True |
| 14 | A | Rejected authentication via daemon-backed connect_to_host | Bad password does not yield a lasting connected session | session_state=SessionState.CLOSED gtk_connected=False session_ok=True | PASS | session_state=SessionState.CLOSED gtk_connected=False session_ok=True |
| 15 | A | SFTP listing via builtin FM open + daemon client.sftp_list_directory | Daemon SFTP READY and remote listing succeeds | fm_connected=False pages=1 sftp_state=SftpServiceState.READY names=['.ssh'] | PASS | pages_before=1 pages_after=1 |
| 16 | A | Remote directory creation via daemon client.sftp_mkdir | mkdir visible in listing | has_dir=True | PASS | has_dir=True |
| 17 | A | Upload via daemon client.start_transfer (UPLOAD) | Transfer completes | state=TransferState.COMPLETED | PASS | state=TransferState.COMPLETED |
| 18 | A | Download via daemon client.start_transfer (DOWNLOAD) | Local file matches uploaded content | exists=True match=True | PASS | exists=True match=True |
| 19 | A | Rename via daemon client.sftp_rename | renamed.txt present | names=['renamed.txt'] | PASS | names=['renamed.txt'] |
| 20 | A | Delete via daemon client.sftp_remove / sftp_rmdir | remote_dir removed | has_dir=False | PASS | has_dir=False |
| 21 | A | Large-transfer cancellation via daemon client.cancel_transfer | Transfer CANCELLED and remote payload absent | state=TransferState.CANCELLED remote_present=False tmps=[] | PASS | state=TransferState.CANCELLED remote_present=False tmps=[] |
| 22 | A | Temporary-file cleanup after cancelled transfer | No leftover .sshpilot-tmp-* files | local_leftovers=0 remote_tmps=0 | PASS | [] |
| 23 | A | Local forwarding via daemon client.open_forward(LOCAL) | ACTIVE and HTTP OK | active=True ok=True port=40435 | PASS | active=True ok=True port=40435 |
| 24 | A | Remote forwarding via daemon client.open_forward(REMOTE) | ACTIVE and payload OK | active=True payload='ping' port=19302 | PASS | active=True payload='ping' port=19302 |
| 25 | A | Dynamic SOCKS forwarding via daemon client.open_forward(DYNAMIC) | Forward ACTIVE and port listening | active=True port=43781 | PASS | active=True port=43781 |
| 26 | A | Forward shutdown via daemon client.close_forward | Closed remote/dynamic forwards | remote_closed=True dynamic_closed=True keep_local=forward:40fb32e8-5fd6-417e-9cf0-f6f9de82ef0b | PASS | remote_closed=True dynamic_closed=True keep_local=forward:40fb32e8-5fd6-417e-9cf0-f6f9de82ef0b |
| 27 | B | Export via BackupManager.export_configuration (not file chooser) | Export file written | ok=True path=/tmp/sshpilot-phase13-smoke-xzuya9fb/evidence/export.json msg=None | PASS | /tmp/sshpilot-phase13-smoke-xzuya9fb/evidence/export.json |
| 28 | B | Import validation via BackupManager.plan_configuration_import | Plan succeeds | ImportPlan(ok=True, schema_version=1, strategy=<MergeStrategy.MERGE: 'merge'>, connections_to_add=[], connections_to_update=[], connections_to_skip=[], groups_t | PASS | ImportPlan(ok=True, schema_version=1, strategy=<MergeStrategy.MERGE: 'merge'>, connections_to_add=[], connections_to_update=[], connections_to_skip=[], groups_t |
| 29 | B | Merge import via BackupManager.import_configuration | Merge returns success | None | PASS | None |
| 30 | B | Secrets excluded from export by default | credentials absent/empty | credentials=None | PASS | /tmp/sshpilot-phase13-smoke-xzuya9fb/evidence/export.json |
| 31 | B | Skip-conflict import (re-merge) | Second merge handled | None | PASS | None |
| 32 | B | Replace import via BackupManager.import_configuration | Replace returns success | None | PASS | None |
| 33 | B | GTK close with active session (GuiApp.shutdown, detach policy) | GTK torn down while daemon kept sessions | sessions_before=7 active_before=5 | PASS | active_session_ids=['session:3e0d1d82-aecd-4e15-8042-c13eff0027a8', 'session:bb340d92-a15e-4cb2-a3f9-226bc0d8fbdd', 'session:4249232c-0501-4bce-9199-3ba7d3ffe194', 'session:50445e66-95f1-4b1a-a5fb-61d11e8201ef', 'session:8110f147-dd26-4843-9ccb-51569cc38a76'] |
| 34 | B | Session rediscovery after GTK restart + DaemonClient reinject | Daemon still lists sessions | sessions_after=7 sample=['session:3e0d1d82-aecd-4e15-8042-c13eff0027a8', 'session:bb340d92-a15e-4cb2-a3f9-226bc0d8fbdd', 'session:4249232c-0501-4bce-9199-3ba7d3ffe194'] | PASS | sessions_after=7 sample=['session:3e0d1d82-aecd-4e15-8042-c13eff0027a8', 'session:bb340d92-a15e-4cb2-a3f9-226bc0d8fbdd', 'session:4249232c-0501-4bce-9199-3ba7d3ffe194'] |
| 35 | B | GTK close with active forward (detach; daemon forward kept) | Forward was ACTIVE before GTK shutdown | active_forwards_before=1 ids=['forward:40fb32e8-5fd6-417e-9cf0-f6f9de82ef0b'] | PASS | active_forwards_before=1 ids=['forward:40fb32e8-5fd6-417e-9cf0-f6f9de82ef0b'] |
| 36 | B | Forward rediscovery after GTK restart | Daemon still lists ACTIVE forward or none were required | forwards_after=[('forward:40fb32e8-5fd6-417e-9cf0-f6f9de82ef0b', 'ForwardState.ACTIVE'), ('forward:3f04b89d-f4a8-4d1a-9d76-9b05256a72c8', 'ForwardState.CLOSED'), ('forward:aa4f33f3-a006-4bc9-9eb5-0bf4cf22de7c', 'ForwardState.CLOSED')] | PASS | forwards_after=[('forward:40fb32e8-5fd6-417e-9cf0-f6f9de82ef0b', 'ForwardState.ACTIVE'), ('forward:3f04b89d-f4a8-4d1a-9d76-9b05256a72c8', 'ForwardState.CLOSED'), ('forward:aa4f33f3-a006-4bc9-9eb5-0bf4cf22de7c', 'ForwardState.CLOSED')] |
| 37 | B | Transfer behavior around GTK restart | No RUNNING transfers left from cancelled large upload | running=0 total=3 | PASS | running=0 total=3 |
| 38 | A | Final daemon state (ephemeral smoke daemon) | Smoke daemon socket still present; no smoke-owned leak beyond it | sock_exists=True sessions=7 forwards=3 | PASS | /tmp/sshpilot-p13d-97d81953-vvm9mlv6/sshpilotd.sock |
| 39 | A | sshpilot-core without display | validate-connection exits 0 | ok nickname: Valid connection name (info) ok hostname: Valid hostname (info) ok port: Standard SSH port (info) ok username: Valid username (info)  | PASS | ok nickname: Valid connection name (info) ok hostname: Valid hostname (info) ok port: Standard SSH port (info) ok username: Valid username (info)  |
| 40 | A | Daemon isolation tests with environment active | isolation tests pass | rning: GLib.unix_signal_add_full is deprecated; use GLibUnix.signal_add_full instead     value = getattr(proxy, attr)  -- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html 6 passed, 1 warning in 1.30s  | PASS | rning: GLib.unix_signal_add_full is deprecated; use GLibUnix.signal_add_full instead     value = getattr(proxy, attr)  -- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html 6 passed, 1 warning in 1.30s  |

## Verdict

```text
READY FOR FINAL RELEASE HARDENING
```

Generated at 2026-07-30T04:49:53Z
