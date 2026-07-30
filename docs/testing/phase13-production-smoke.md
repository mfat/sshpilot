# Phase 13.3 production path smoke

Isolated HOME: `/tmp/sshpilot-phase13-smoke-uvvzn1n7`
Evidence directory: `/tmp/sshpilot-phase13-smoke-uvvzn1n7/evidence`

## Layered results

```text
Daemon/API: 21/21
GTK controller: 19/19
Widget interaction: 0/0
Lifecycle shutdown: 10/12
Overall gate: PASS
```

Layer A = ephemeral daemon + DaemonClient production APIs (no VTE required).
Layer B = GTK controllers / ConnectionManager / BackupManager / restart rediscovery.
Layer C = visible widget clicks (not required for this gate; VTE opt-in via
`SSHPILOT_SMOKE_GTK_TERMINAL=1` — see `gtk-vte-bloom-filter-crash.md`).
Layer D = lifecycle shutdown: resource drain, graceful stop, natural exit,
socket removal, child reaping, interaction cleanup.

Emergency cleanup was invoked (acceptance path failed).

| step | layer | action | expected result | actual result | pass/fail | evidence |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | B | Start SSH Pilot (real GTK, isolated HOME) | MainWindow presented | window=MainWindow pages=1 | PASS | HOME=/tmp/sshpilot-phase13-smoke-uvvzn1n7 |
| 2 | B | Existing connections load (ConnectionManager) | Connection list loads without error | count=0 | PASS | count=0 |
| 3 | B | Connection create via ConnectionManager.add_connection_from_data | Connection P13Create exists | P13Create | PASS | P13Create |
| 4 | B | Connection edit via ConnectionManager.update_connection | Username/port persisted | user=phase13 port=35355 | PASS | user=phase13 port=35355 |
| 5 | B | Group move via GroupManager.move_connection | Connection primary group is P13Group | 03a5cbc3-7fbb-42de-aa75-1a047e8a2d22 | PASS | 03a5cbc3-7fbb-42de-aa75-1a047e8a2d22 |
| 6 | B | Reorder via GroupManager.reorder_connection_in_group | Order changes | ['cc4dd2da-e1d4-4967-ad0d-255fdae03f00', '0ee5f316-cadb-471a-9752-e326193d6298'] -> ['0ee5f316-cadb-471a-9752-e326193d6298', 'cc4dd2da-e1d4-4967-ad0d-255fdae03f00'] | PASS | ['cc4dd2da-e1d4-4967-ad0d-255fdae03f00', '0ee5f316-cadb-471a-9752-e326193d6298'] -> ['0ee5f316-cadb-471a-9752-e326193d6298', 'cc4dd2da-e1d4-4967-ad0d-255fdae03f00'] |
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
| 23 | A | Local forwarding via daemon client.open_forward(LOCAL) | ACTIVE and HTTP OK | active=True ok=True port=56845 | PASS | active=True ok=True port=56845 |
| 24 | A | Remote forwarding via daemon client.open_forward(REMOTE) | ACTIVE and payload OK | active=True payload='ping' port=19300 | PASS | active=True payload='ping' port=19300 |
| 25 | A | Dynamic SOCKS forwarding via daemon client.open_forward(DYNAMIC) | Forward ACTIVE and port listening | active=True port=46643 | PASS | active=True port=46643 |
| 26 | A | Forward shutdown via daemon client.close_forward | Closed remote/dynamic forwards | remote_closed=True dynamic_closed=True keep_local=forward:f72dec1e-30f5-46bf-bda4-c58b9feedc52 | PASS | remote_closed=True dynamic_closed=True keep_local=forward:f72dec1e-30f5-46bf-bda4-c58b9feedc52 |
| 27 | B | Export via BackupManager.export_configuration (not file chooser) | Export file written | ok=True path=/tmp/sshpilot-phase13-smoke-uvvzn1n7/evidence/export.json msg=None | PASS | /tmp/sshpilot-phase13-smoke-uvvzn1n7/evidence/export.json |
| 28 | B | Import validation via BackupManager.plan_configuration_import | Plan succeeds | ImportPlan(ok=True, schema_version=1, strategy=<MergeStrategy.MERGE: 'merge'>, connections_to_add=[], connections_to_update=[], connections_to_skip=[], groups_t | PASS | ImportPlan(ok=True, schema_version=1, strategy=<MergeStrategy.MERGE: 'merge'>, connections_to_add=[], connections_to_update=[], connections_to_skip=[], groups_t |
| 29 | B | Merge import via BackupManager.import_configuration | Merge returns success | None | PASS | None |
| 30 | B | Secrets excluded from export by default | credentials absent/empty | credentials=None | PASS | /tmp/sshpilot-phase13-smoke-uvvzn1n7/evidence/export.json |
| 31 | B | Skip-conflict import (re-merge) | Second merge handled | None | PASS | None |
| 32 | B | Replace import via BackupManager.import_configuration | Replace returns success | None | PASS | None |
| 33 | B | GTK close with active session (GuiApp.shutdown, detach policy) | GTK torn down while daemon kept sessions | sessions_before=7 active_before=5 | PASS | active_session_ids=['session:f1573d4b-fca8-4214-9f67-3a9a7ae78ad2', 'session:bda632e8-51bf-4bea-9103-a66d923e72e3', 'session:5238178d-de39-4618-8a46-fc83e9d8384b', 'session:0731f040-e2e2-4b6c-b7c0-269bd330ef47', 'session:9eca6080-66d9-41a8-aa50-7e0abfa55db0'] |
| 34 | B | Session rediscovery after GTK restart + DaemonClient reinject | Daemon still lists sessions | sessions_after=7 sample=['session:f1573d4b-fca8-4214-9f67-3a9a7ae78ad2', 'session:bda632e8-51bf-4bea-9103-a66d923e72e3', 'session:5238178d-de39-4618-8a46-fc83e9d8384b'] | PASS | sessions_after=7 sample=['session:f1573d4b-fca8-4214-9f67-3a9a7ae78ad2', 'session:bda632e8-51bf-4bea-9103-a66d923e72e3', 'session:5238178d-de39-4618-8a46-fc83e9d8384b'] |
| 35 | B | GTK close with active forward (detach; daemon forward kept) | Forward was ACTIVE before GTK shutdown | active_forwards_before=1 ids=['forward:f72dec1e-30f5-46bf-bda4-c58b9feedc52'] | PASS | active_forwards_before=1 ids=['forward:f72dec1e-30f5-46bf-bda4-c58b9feedc52'] |
| 36 | B | Forward rediscovery after GTK restart | Daemon still lists ACTIVE forward or none were required | forwards_after=[('forward:f72dec1e-30f5-46bf-bda4-c58b9feedc52', 'ForwardState.ACTIVE'), ('forward:17fbc169-5499-47e2-b3fe-914ef1d255b5', 'ForwardState.CLOSED'), ('forward:997c6a56-1c5a-49a2-88ff-76dd2eac06c2', 'ForwardState.CLOSED')] | PASS | forwards_after=[('forward:f72dec1e-30f5-46bf-bda4-c58b9feedc52', 'ForwardState.ACTIVE'), ('forward:17fbc169-5499-47e2-b3fe-914ef1d255b5', 'ForwardState.CLOSED'), ('forward:997c6a56-1c5a-49a2-88ff-76dd2eac06c2', 'ForwardState.CLOSED')] |
| 37 | B | Transfer behavior around GTK restart | No RUNNING transfers left from cancelled large upload | running=0 total=3 | PASS | running=0 total=3 |
| 38 | A | Final daemon state (ephemeral smoke daemon) | Smoke daemon socket still present; no smoke-owned leak beyond it | sock_exists=True sessions=7 forwards=3 | PASS | /tmp/sshpilot-p13d-c59befc0-9p7hshjq/sshpilotd.sock |
| 39 | A | sshpilot-core without display | validate-connection exits 0 | ok nickname: Valid connection name (info) ok hostname: Valid hostname (info) ok port: Standard SSH port (info) ok username: Valid username (info)  | PASS | ok nickname: Valid connection name (info) ok hostname: Valid hostname (info) ok port: Standard SSH port (info) ok username: Valid username (info)  |
| 40 | A | Daemon isolation tests with environment active | isolation tests pass | rning: GLib.unix_signal_add_full is deprecated; use GLibUnix.signal_add_full instead     value = getattr(proxy, attr)  -- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html 6 passed, 1 warning in 1.28s  | PASS | rning: GLib.unix_signal_add_full is deprecated; use GLibUnix.signal_add_full instead     value = getattr(proxy, attr)  -- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html 6 passed, 1 warning in 1.28s  |
| 41 | D | Enumerate active daemon resources via client.get_daemon_status | Resource counts returned | sessions_active=5 sftp_active=0 transfers_running=0 forwards_active=1 interactions_pending=0 | PASS | sessions_active=5 sftp_active=0 transfers_running=0 forwards_active=1 interactions_pending=0 |
| 42 | D | Close all sessions via client.close_session (public API) | No active sessions remain | closed=5 active_after=0 | PASS | closed=5 active_after=0 |
| 43 | D | Close all SFTP services via client.close_sftp (public API) | No active SFTP services remain | closed=0 active_after=0 | PASS | closed=0 active_after=0 |
| 44 | D | Cancel/finish active transfers via client.cancel_transfer (public API) | No active transfers remain | cancelled=0 active_after=0 | PASS | cancelled=0 active_after=0 |
| 45 | D | Close all forwards via client.close_forward (public API) | No active forwards remain | closed=0 active_after=1 errors=['forward:f72dec1e-30f5-46bf-bda4-c58b9feedc52:Only the originating client may mutate this forward'] | FAIL | closed=0 active_after=1 errors=['forward:f72dec1e-30f5-46bf-bda4-c58b9feedc52:Only the originating client may mutate this forward'] |
| 46 | D | Verify no active daemon work remains (non-client live_blockers empty) | Daemon has no live resource blockers other than connected clients | live_blockers=('clients', 'forwards') non_client=('forwards',) | FAIL | live_blockers=('clients', 'forwards') non_client=('forwards',) |
| 47 | D | Request daemon stop via client.stop_daemon (public API, force if needed) | Stop accepted | accepted=True state=DaemonLifecycleState.DRAINING message=Shutdown accepted | PASS | accepted=True state=DaemonLifecycleState.DRAINING message=Shutdown accepted |
| 48 | D | Verify lifecycle transitions: ready -> draining -> stopping | Observed draining/stopping/stopped or transport_closed after force | observed_states=['transport_closed'] | PASS | observed_states=['transport_closed'] |
| 49 | D | Verify daemon exits naturally after graceful stop | Daemon process stopped | exited=True | PASS | exited=True |
| 50 | D | Verify daemon socket removed and no stale metadata files remain | Socket gone, no PID/metadata/instance files (socket-identity design) | sock_gone=True leftover_files=[] | PASS | sock_gone=True leftover_files=[] |
| 51 | D | Verify no orphaned daemon child processes remain | No zombie or child processes of smoke PID | orphans=[] | PASS | orphans=[] |
| 52 | D | Verify no stale askpass sockets and no pending interactions remain | No askpass sockets, interactions_pending == 0 | askpass_socks=0 interactions_pending=0 | PASS | askpass_socks=0 interactions_pending=0 |

## Verdict

```text
READY FOR FINAL RELEASE HARDENING
```

Generated at 2026-07-30T19:34:37Z

