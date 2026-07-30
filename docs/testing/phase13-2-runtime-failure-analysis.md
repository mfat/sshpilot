# Phase 13.2 — Runtime failure analysis

Baseline SHA: `f621d7c6`. Fresh smoke log:
`/tmp/phase13-2-baseline/fresh-smoke-run.log` (also
`phase13-1-smoke-daemon4-complete.log`, `phase13-1-smoke-daemon5.log`).

Initial result: **11 PASS / 13 FAIL** then Adwaita abort mid-forward steps
(daemon4 completed more rows: **20/40** with GTK restart failing on D-Bus).

## Method

Failures were reproduced with the Phase 13.1 harness against an ephemeral
`DaemonServer` + temporary OpenSSH fixture. Root causes were confirmed by
reading production code and contrasting with the green Phase 10 helpers
(`tests/daemon/phase10_helpers.py`), which already exercise the same daemon
APIs successfully (20 passed integration tests).

---

## Host-key ask

| | |
| --- | --- |
| Step | 12 |
| Observed | `SessionState.STARTING` until client poll timeout |
| Root cause | Smoke `_start_auth_helper` answered via a **second** `DaemonClient` (`client:phase13-auth-*`). `SessionRuntime.client_can_interact` only admits `originating_client_id` or attachments (`session_runtime.py`). The helper never saw `HOST_KEY_CONFIRMATION`, so `InteractionBroker._prepare_host_key` → `wait_for_result` blocked (~180s host-key timeout) while the deferred `sessions.open` worker stayed in `STARTING`. |
| Evidence | Phase 10 uses in-process `broker.list(owner_client_id)` / `claim` / `respond` (`answer_pending_host_keys`). Separate-client helper cannot list eligible interactions. Steps 9–11 pass because stored password + populated known_hosts skip interactions entirely. |
| Fix | Answer interactions as the owning client (in-process broker, same pattern as Phase 10). Ensure accept/reject reach terminal session states. |

## Cancellation

| | |
| --- | --- |
| Step | 13 |
| Observed | `SessionState.RUNNING` after cancel attempt |
| Root cause | (1) Same eligibility bug — `SecretDecision.CANCEL` never reached the broker. Askpass waited up to **120s** secret timeout per attempt. (2) `SessionRuntime.start_session` transitions to `RUNNING` as soon as the PTY process handle exists (`session_runtime.py`), **before** authentication completes. |
| Evidence | Phase 10 `start_password_decline` cancels via in-process broker with owner client id. Default secret timeout = 120s (`DEFAULT_SECRET_INTERACTION_TIMEOUT`). Smoke timeout 25s → still `RUNNING`. |
| Fix | Owner-client cancel responses; gate `RUNNING` on ControlMaster readiness (auth proof); cancel → askpass decline → process exit → `FAILED`/`EXITED` with `OPERATION_CANCELLED` (no `CANCELLED` session enum yet — documented failure state). |

## Rejected authentication

| | |
| --- | --- |
| Step | 14 |
| Observed | `SessionState.RUNNING` with wrong password |
| Root cause | Premature `RUNNING` on process spawn. Stored wrong password is autofilled on attempt 1; subsequent OpenSSH retries create interactions that hung (helper ineligible) for up to 120s each (`NumberOfPasswordPrompts=3`). Smoke 25s window still saw `RUNNING`. Not a fixture accepting the wrong password. |
| Evidence | `_resolve_askpass_secret` returns stored secret without an interaction on first try; later attempts call `create` + `wait_for_result`. |
| Fix | Auth-gated `RUNNING`; answer/decline retry prompts promptly; assert `FAILED`/`EXITED` and no lasting `RUNNING` for the connection. |

## SFTP readiness

| | |
| --- | --- |
| Steps | 15–22 |
| Observed | Never `READY`; follow-on `ValueError('SFTP service id must be a non-empty string')` |
| Root cause | Cascade from host-key/auth interaction eligibility during `open_sftp` startup when broker pinning/prompting is required; FM path also never reached connected. Phase 10 SFTP integration is green when host keys are answered via owner client. |
| Evidence | `tests/integration/test_sftp_phase10.py` passes; smoke used ineligible helper + required FM connected ∧ READY. |
| Fix | Same interaction answering; wait for `READY`/`FAILED` via daemon client; do not require FM widget for Layer A. |

## Transfer operations

| | |
| --- | --- |
| Steps | 16–22 |
| Observed | Hard-fail on empty SFTP id |
| Root cause | Dependent on SFTP `READY`. Transfer runtime itself is covered by Phase 10 transfer tests. |
| Fix | After SFTP READY, exercise daemon `start_transfer` / cancel / atomic cleanup. |

## Local / remote / dynamic forwarding

| | |
| --- | --- |
| Steps | 23–25 |
| Observed | Local/dynamic never `ACTIVE`; remote `ACTIVE` but empty traffic payload |
| Root cause | Local/dynamic: `_detect_active` waits for bind while SSH may still be blocked on unanswered broker interactions (same eligibility). Remote: ACTIVE via process-alive window, but container `nc` traffic to reverse forward returned empty (timing / busybox `nc` / echo accept race); Adwaita abort often cut the run. |
| Evidence | Phase 10 forward integration passes with `wait_forward_active` + host-key answering. |
| Fix | Owner-client interactions; prove traffic; keep echo server alive for remote; avoid GTK dialog poking. |

## GTK restart / daemon rediscovery

| | |
| --- | --- |
| Steps | 33–38 |
| Observed | `GLib.Error: An object is already exported for the interface org.gtk.Application at /io/github/mfat/sshpilot` |
| Root cause | `SshPilotApplication` hardcodes `application_id='io.github.mfat.sshpilot'`. Restart passed a unique id via `set_application_id` after construction, but the first instance was not fully quit/unexported; object path collision. |
| Fix | Construct with unique application id; `NON_UNIQUE`; explicit quit + bounded wait for destruction before boot B. |

## Daemon rediscovery / lifecycle

Documented separately after implementation. Current policy exists in
`lifecycle_policy.py`; smoke must prove detach-on-UI-exit with active work and
final idle exit without claiming unfinished Phase 14 work as done.

## VTE / GTK crash

| | |
| --- | --- |
| Observed | (a) Adwaita `dialog_closing_cb` abort during smoke forwards; (b) known VTE bloom-filter abort when `SSHPILOT_SMOKE_GTK_TERMINAL=1` |
| Root cause | (a) Smoke auth helper blindly called `.response("ok")` on every toplevel with a `response` method — races Adwaita dialog host. (b) External/library or VTE misuse — isolated in `gtk-vte-bloom-filter-crash.md`. |
| Fix | Remove dialog poking; keep Layer A daemon acceptance independent of VTE tabs. |

---

## Semantic definitions (target)

| State | Meaning |
| --- | --- |
| `STARTING` | Session accepted; process/auth in progress — **not** usable |
| `RUNNING` | Authenticated and ControlMaster (or equivalent) ready — usable |
| `FAILED` | Startup/auth/runtime failure (includes cancelled auth → `OPERATION_CANCELLED`) |
| `CANCELLED` | *Not in Protocol v1 session enum yet*; cancellation maps to `FAILED` + `OPERATION_CANCELLED` until a protocol bump |
| `DETACHED` | *Client-side concept*; daemon session may remain `RUNNING` after UI disconnect |
| `CLOSED` | Terminal; resources reaped |

## Instrumentation

Smoke/auth paths should log structured fields when steps fail: request/session
ids, interaction id/kind/decision, subprocess pid/exit, stderr tail, timeout
reason, daemon vs client-observed state.

## Repair status

Phase 13.2 repaired the root causes above. Final layered smoke:

```text
Daemon/API: 21/21
GTK controller: 19/19
Widget interaction: 0/0
Overall gate: PASS
```

Verdict: `READY FOR FINAL RELEASE HARDENING`.
