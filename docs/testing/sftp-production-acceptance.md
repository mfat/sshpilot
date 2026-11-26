# SFTP file manager production acceptance

Production readiness is a release decision for a specific commit, package, OS,
and server configuration. A green unit suite alone is insufficient. Every gate
below must have recorded evidence; skipped, unavailable, and unexecuted tests
mean **unproven**, not passed. Any data loss, authorization bypass, secret leak,
or false success blocks release. Other failed gates require fixes and reruns.

## Acceptance gates and evidence

| Gate | Observable acceptance criterion | Evidence to run / collect |
| --- | --- | --- |
| Connect and authenticate | Correct credentials reach READY; rejected credentials fail with a typed error; cancellation never leaves a stuck tab. Native SSH configuration and host-key decisions are honored. | `tests/integration/test_sftp_phase10.py`; additionally exercise unknown and changed host keys, key/passphrase auth, agent and ProxyJump on the release package. |
| Browse and mutate | Listing, mkdir, rename, remove, stat, chmod and links affect the intended path only. Unicode, spaces, quotes and shell metacharacters remain literal. Truncated listings are explicitly indicated. | `tests/integration/test_sftp_phase10.py`; `tests/daemon/test_sftp_non_utf8_names.py`; GUI mutation suite. Verify destructive confirmation and refresh in the packaged UI. |
| Byte integrity | Zero-byte, binary, multiblock and recursive uploads/downloads reproduce every byte and the expected tree. Completion is emitted exactly once, only after success. | `tests/integration/test_transfer_phase10.py`; `tests/daemon/test_transfer_batch_real_sftp.py`. Compare contents, not merely sizes or progress counters. |
| Metadata | New transfers retain documented mode/mtime semantics; overwriting a private file does not broaden its permissions. | `tests/daemon/test_transfer_metadata_real_sftp.py`. Run on each supported filesystem/platform; document server umask effects. |
| Overwrite safety | Rejecting overwrite preserves old bytes. Cancelling mid-file, in either direction, preserves an existing destination or leaves a new destination absent, and removes staging files. | Integration overwrite/cancellation tests; real-server `test_cancel_mid_file_leaves_nothing_under_the_real_name`, both directions and both destination states. Cancellation must occur after bytes move and before EOF. |
| Partial failure | A failed batch identifies the failed item; successful siblings contain correct bytes; aggregate state cannot report success. Server death terminates work without a hang. | Real-server batch tests for missing items, blocked destinations and server loss. Also inject disk-full, permission denial and network loss mid-file against real SSH; inspect old destination and staging files. These additional scenarios remain required evidence. |
| Lifecycle and ownership | Closing a session or daemon stops owned children and operations; another client cannot access services without attachment/authorization; reconnect works after failure. | SFTP integration ownership/shutdown tests, transfer close test, `tests/daemon/test_sftp_session_death_stuck_tab.py`. Repeat open/transfer/cancel/close 100 times and verify no accumulating children, handles or temporary files. |
| UI correctness | Real GTK actions use daemon-backed paths; progress and cancel operate on the intended transfer; errors are actionable; completion refreshes listings; closing/reopening does not show stale state. | `tests/gui/test_phase14_file_manager_integration.py` and `tests/gui/test_phase14_file_manager_mutations.py`; packaged UI checks for keyboard navigation, multi-selection, retry, confirmation and disconnected states. |
| Security and architecture | Core/API/daemon stay GI-free; credentials remain on protected interactions; no credentials appear in logs, ordinary RPC, argv or environment. | Architecture, API and secret-transfer tests, plus inspect captured diagnostics from real authentication failures with synthetic credentials. |
| Capacity | On declared minimum hardware: a 10,000-entry directory gives feedback within 1 second and completes or explicitly truncates within 10 seconds; cancellation acknowledges within 2 seconds and settles within 10 seconds on a responsive server; a 1 GiB transfer uses less than 256 MiB additional daemon RSS. | Record timings and peak RSS on the release package, including a delayed network and simultaneous browsing/transfers. These are proposed release budgets; approve any changed budgets before measuring. |

## Automated evidence commands

Run from the repository's configured test environment:

```sh
pytest -q tests/daemon/test_transfer_batch_real_sftp.py tests/daemon/test_transfer_metadata_real_sftp.py
pytest -q tests/integration/test_sftp_phase10.py tests/integration/test_transfer_phase10.py
pytest -q tests/gui/test_phase14_file_manager_integration.py tests/gui/test_phase14_file_manager_mutations.py
pytest -q tests/architecture tests/core tests/api
python scripts/generate_api_artifacts.py --check
ruff check src/ tests/ scripts/generate_api_artifacts.py
pytest
```

Use the existing integration/GUI fixture setup instructions and inspect skip
reasons. Local `sftp-server` tests exercise the real binary and transfer runtime,
but bypass SSH authentication, transport, public RPC and GTK. Container SSH
integration tests cover the daemon/client route; GUI tests cover presentation.
All three levels are needed. Mock tests support deterministic fault coverage,
but do not substitute for real transport or filesystem evidence.

## Release record

Record commit, clean/dirty worktree, package hash, OS/filesystem, OpenSSH server
version/configuration, hardware/network profile, exact commands, pass/fail/skip
counts, logs, timings, peak RSS and remaining issues. Keep a row for every gate
with evidence links and reviewer sign-off. Run the same matrix on each supported
packaging/platform target. Until required evidence is present, the verdict is
**readiness not established**; this document does not certify a release.
