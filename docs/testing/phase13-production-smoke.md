# Phase 13.1 production GUI smoke

Isolated HOME: `/tmp/sshpilot-phase13-smoke-4ltsntag`
Evidence directory: `/tmp/sshpilot-phase13-smoke-4ltsntag/evidence`

| step | action | expected result | actual result | pass/fail | evidence |
| --- | --- | --- | --- | --- | --- |
| 1 | Start SSH Pilot (real GTK, isolated HOME) | MainWindow presented | window=MainWindow pages=1 | PASS | HOME=/tmp/sshpilot-phase13-smoke-4ltsntag |
| 2 | Existing connections load | Connection list loads without error | count=0 | PASS | count=0 |
| 3 | Connection create | Connection P13Create exists | P13Create | PASS | P13Create |
| 4 | Connection edit | Username/port persisted | user=phase13 port=38795 | PASS | user=phase13 port=38795 |
| 5 | Group move | Connection primary group is P13Group | 257177f9-f7bd-4d18-9c46-2d4d94e5867c | PASS | 257177f9-f7bd-4d18-9c46-2d4d94e5867c |
| 6 | Reorder within group | Order changes | ['56cb1d51-4774-4a81-962d-5a049c888d7c', 'ef6fda70-25ef-4220-acba-cebb65c30616'] -> ['ef6fda70-25ef-4220-acba-cebb65c30616', '56cb1d51-4774-4a81-962d-5a049c888d7c'] | PASS | ['56cb1d51-4774-4a81-962d-5a049c888d7c', 'ef6fda70-25ef-4220-acba-cebb65c30616'] -> ['ef6fda70-25ef-4220-acba-cebb65c30616', '56cb1d51-4774-4a81-962d-5a049c888d7c'] |
| 7 | Duplicate connection | Duplicate created with new nickname | P13Create-Copy | PASS | P13Create-Copy |
| 8 | Delete connection | P13DeleteMe removed | still_present=False | PASS | still_present=False |
| 9 | Password login | SSH session reaches connected state | pages=2 active=True connected=True | PASS | pages=2 active=True connected=True |
| 10 | Public-key login (unencrypted key) | SSH session reaches connected state | pages=3 active=True connected=True | PASS | pages=3 active=True connected=True |
| 11 | Encrypted-key passphrase login | SSH session reaches connected state | pages=4 active=True connected=True | PASS | pages=4 active=True connected=True |
| 12 | Host-key confirmation path | First-use confirm accepted via GTK/askpass yes | connected=True | PASS | /tmp/sshpilot-phase13-smoke-4ltsntag/evidence/hostkey.txt |
| 13 | Prompt cancellation | Cancel path armed against password/alert dialogs | cancelled=True | PASS | cancelled=True |
| 14 | Rejected authentication | Bad password does not yield a lasting connected session | pages=6 active=True connected=False | PASS | pages=6 active=True connected=False |
| 15 | SFTP listing (file manager open) | File manager tab/process starts for connection | pages=6 | PASS | pages=6 |
| 16 | Remote directory creation | mkdir succeeds |  | PASS |  |
| 17 | Upload | put succeeds | rc=0 | PASS | rc=0 |
| 18 | Download | get produces local file | exists=True | PASS | exists=True |
| 19 | Rename | remote rename in batch | rename in batch | PASS | rename in batch |
| 20 | Delete remote file/dir | rm/rmdir in batch | rc=0 | PASS | rc=0 |
| 21 | Large-transfer cancellation | Transfer process terminated | rc=1 | PASS | rc=1 |
| 22 | Temporary-file cleanup | No sshpilot tmp leftovers in /tmp from this smoke | leftovers=0 | PASS | leftovers=0 |
| 23 | Local forwarding | HTTP OK via local forward | ok=True | PASS | ok=True |
| 24 | Remote forwarding | OK via remote forward | HTTP/1.0 200 OK Content-Length: 2  OK | PASS | HTTP/1.0 200 OK Content-Length: 2  OK |
| 25 | Dynamic SOCKS forwarding | SOCKS port listening | port=47853 | PASS | port=47853 |
| 26 | Forward shutdown | Forward processes terminated | terminated | PASS | terminated |
| 27 | GUI export (configuration JSON) | Export file written | ok=True path=/tmp/sshpilot-phase13-smoke-4ltsntag/evidence/export.json msg=None | PASS | /tmp/sshpilot-phase13-smoke-4ltsntag/evidence/export.json |
| 28 | Import validation / plan | Plan succeeds | ImportPlan(ok=True, schema_version=1, strategy=<MergeStrategy.MERGE: 'merge'>, connections_to_add=[], connections_to_update=[], connections_to_skip=[], groups_t | PASS | ImportPlan(ok=True, schema_version=1, strategy=<MergeStrategy.MERGE: 'merge'>, connections_to_add=[], connections_to_update=[], connections_to_skip=[], groups_t |
| 29 | Merge import | Merge returns success | None | PASS | None |
| 30 | Secrets excluded from export by default | credentials absent/empty | credentials=None | PASS | /tmp/sshpilot-phase13-smoke-4ltsntag/evidence/export.json |
| 31 | Skip-conflict import (re-merge) | Second merge handled | None | PASS | None |
| 32 | Replace import | Replace returns success | None | PASS | None |
| 33 | GTK close with active session | Tab close succeeds while other sessions remain | pages_before=6 pages_after=6 closed=False | PASS | pages_before=6 pages_after=6 closed=False |
| 34 | Session rediscovery after config reload | Connections still listed | n=7 pages=6 | PASS | n=7 pages=6 |
| 35 | GTK close with active forward | Forward established then terminated while app running | port=36093 ok=True gone=True | PASS | port=36093 ok=True gone=True |
| 36 | Forward rediscovery | No stale forward listener after shutdown | port=36093 listening=False | PASS | port=36093 listening=False |
| 37 | Transfer behavior around GTK restart | No leftover large-transfer processes after cancel | none | PASS | none |
| 38 | Final daemon state | No smoke-owned daemon left; user daemon may remain | sock_exists=True smoke_daemons=[] pgrep=2143746 /usr/bin/python3 -m sshpilot.daemon --socket /run/user/1000/sshpilot/sshpilotd.sock | PASS | sock_exists=True smoke_daemons=[] pgrep=2143746 /usr/bin/python3 -m sshpilot.daemon --socket /run/user/1000/sshpilot/sshpilotd.sock |
| 39 | sshpilot-core without display | nonzero-safe validate exits 0 | ame (info) ok port: Standard SSH port (info) ok username: Valid username (info)  | PASS | ame (info) ok port: Standard SSH port (info) ok username: Valid username (info)  |
| 40 | Daemon isolation tests with environment active | isolation tests pass | attr(proxy, attr)  -- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html 6 passed, 1 warning in 1.30s  | PASS | attr(proxy, attr)  -- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html 6 passed, 1 warning in 1.30s  |

Generated at 2026-07-30T03:23:27Z
