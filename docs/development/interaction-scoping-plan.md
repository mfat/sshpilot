# Implementation plan — interaction routing & scoping fix

Status: **agreed plan, not yet implemented** (no code changes made as of this writing).
Scope: follow-up to commit `48d14f1` ("fixed authorized keys window failing to show
password prompt") and the review of it.

The review's verdict (request changes) was confirmed against the code. This plan
adopts the review's foundation with one amendment: the daemon-side
**visibility/eligibility gate** must also learn about operation scopes, otherwise
operation-scoped interactions stay invisible to every client and no GTK fix can work.

---

## 1. Verified findings (evidence)

| # | Finding | Evidence |
|---|---------|----------|
| F1 | Unbound `DaemonInteractionDialogs` (`_session_id is None`) accepts **every** non-secret-service interaction visible to the client. | `src/sshpilot/daemon_interaction_dialogs.py` `_handle_event`: `if self._session_id is not None and summary.session_id != self._session_id: return False` |
| F2 | `set_session()` is a bare assignment; **no replay** of interactions created before binding. | `set_session(self, session_id)` |
| F3 | The broker permits re-claim by the **same client ID**; conflicts only occur across clients. | `InteractionBroker.claim()`: `if existing is not None and existing != client_id: raise` |
| F4 | Multiple presenters share one GTK client: daemon terminals (`terminal.py` `start_daemon_session`/`attach_daemon_session`), `daemon_terminal_widget.py`, file-manager SFTP (`daemon_sftp_backend.py` `connect_to_server`), Authorized Keys (`window.py` `open_authorized_keys…`). Several bind **asynchronously** (main loop keeps running while unbound). | see callers |
| F5 | In the Authorized Keys flow specifically the unbound window cannot fire: `open_sftp()` is a **synchronous blocking RPC** on the GTK thread, and events are processed only via `GLib.idle_add`, which cannot run mid-block. Severity there is low; the wildcard is a real hazard for the async SFTP/terminal flows. | `DaemonClient.open_sftp` → `self._request(...)`; `_on_transport_event` → `GLib.idle_add` |
| F6 | ssh-copy-id interactions are scoped to a **private random** `SessionId(new_operation_id())` — a separate allocator draw from the public `OperationId(new_operation_id())` in `OperationRuntime.start_operation`. `A != B`. | `src/sshpilot/daemon/identity_service.py` `_run_deploy` |
| F7 | The daemon drops interaction events for scopes the client is not eligible for: fan-out checks `_client_can_interact(session_id, client_id)`, which accepts only session-runtime ownership, `sftp-*`, and `forward-*`. An `"operation-…"` scope → **False for every client**. Events never reach the client; `list`/`claim` refuse too. Net effect today: an unstored-password deploy hangs ~120 s per prompt, then fails. | `src/sshpilot/daemon/server.py` `_client_can_interact` + event filter at ~line 2084 |
| F8 | The review's proposed scope fix (`scope_id = SessionId(str(handle.operation_id))`) is **necessary but not sufficient** without F7. | — |
| F9 | `_run_deploy` calls `cancel_session(scope_id)` in `finally` **before** `mark_authenticated(scope_id)`; `cancel_session` deletes the askpass context and wipes `pending_remember`, so "remember password" is a no-op on the deploy path. | `identity_service.py` `_run_deploy`; `InteractionBroker.cancel_session` / `_clear_context_locked` |
| F10 | Same private-scope pattern in `_run_ssh_add`, `_run_remote_text`, `native_scp_backend` (`scp-{connection_id}-{id(cancel_event)}`), and `privileged_file_service` (scope supplied by caller). | see callers |
| F11 | `SecretsInteractionPresenter` overrides `_handle_event` entirely and never consults `_session_id` → unaffected by the presenter invariant change. | `src/sshpilot/gtk/secrets_interaction_presenter.py` |
| F12 | `DaemonClient.list_interactions()` exists → replay is feasible. | `src/sshpilot/api/daemon_client.py:2038` |
| F13 | No unit coverage exists for presenter bind/replay, the eligibility gate, or the remember ordering (only a registration-level deploy test). | `tests/daemon/test_identity_service_phase.py`; `tests/gui/` harness only references the presenter |

## 2. Target architecture

```
Interactive daemon resource       Interaction scope           Eligibility (server._client_can_interact)
-------------------------------   --------------------------   -------------------------------------------------
Terminal session               →  session ID                  session_runtime ownership
SFTP service                   →  sftp service ID             sftp_runtime ownership
Port-forward                    →  forward ID                  forward_runtime ownership
Long-running Operation         →  operation ID (public)       operation_runtime ownership (NEW)
Synchronous headless command   →  public scope or operation   registered per client (NEW / decision)
```

Presenter invariants (in `DaemonInteractionDialogs`):

1. **Unbound presenter = handles nothing.** `_session_id is None` → ignore every event.
2. **`set_session(scope)` = bind + replay.** After binding, reconcile pending
   interactions for that scope via `list_interactions()` and claim/present those
   not already presented or claimed by another responder.
3. **Never double-present.** Replay and event handling must skip interactions whose
   `responder_client_id` is already set (another presenter/client owns presentation).

## 3. Work items

### Phase 1 — Presenter invariant + replay (GTK)

Files: `src/sshpilot/daemon_interaction_dialogs.py`

1. `_handle_event`: change the filter to reject when unbound:
   ```python
   if self._session_id is None:
       return False
   if summary.session_id != self._session_id:
       return False
   ```
   (Keep the `is_secret_service_session` and `_closed` checks before it; the
   `SecretsInteractionPresenter` override is unaffected — F11.)
2. `set_session(session_id)`: set `_session_id`, then reconcile:
   - call `self._client.list_interactions()` through `self._bridge.submit_interaction`;
   - for each summary with `summary.session_id == session_id`, state not final,
     id not in `_claimed`/`_dialogs`, and `responder_client_id is None`
     → claim-and-present (reuse the existing `_handle_event` claim path).
   - The replay must be **idempotent** (skip already-presented ids) so the
     daemon-terminal `_update_daemon_connection_state` re-binding path
     (`terminal.py`) cannot re-present.
3. Audit existing presenters for reliance on the wildcard:
   - `daemon_sftp_backend.connect_to_server` / `_on_service_state_changed`
     (bind on service state) — replay now covers prompts created before bind.
   - `terminal.start_daemon_session` (bind via `_update_daemon_connection_state`),
     `daemon_terminal_widget.start` — same.
   - `terminal.attach_daemon_session` binds immediately — unaffected.
4. No change to `SecretsInteractionPresenter` (F11).

### Phase 2 — Operation interaction scoping (daemon)

Files: `src/sshpilot/daemon/identity_service.py`, `src/sshpilot/daemon/operation_runtime.py`,
`src/sshpilot/daemon/server.py`

1. `_run_deploy`: replace `scope_id = SessionId(new_operation_id())` with
   `scope_id = SessionId(str(handle.operation_id))` — the **public** operation ID.
2. Add `OperationRuntime.client_can_interact(operation_id, client_id)`:
   True iff the operation record exists and `owner_client_id == client_id`.
3. `server._client_can_interact`: for scope strings with the `operation-` prefix,
   consult `self._operation_runtime.client_can_interact(...)` (same pattern as the
   `sftp-` branch). This single change opens up event fan-out **and**
   `list`/`get`/`claim` (the broker already routes eligibility through
   `client_is_eligible=self._client_can_interact`).
4. No broker changes required.

### Phase 3 — GTK ssh-copy-id presenter

Files: `src/sshpilot/sshcopyid_window.py` (`SshCopyIdRunner`)

1. In `run()`: construct `DaemonInteractionDialogs(client, bridge, parent)`
   **before** `client.deploy_key(...)` (parent = the ssh-copy-id dialog/window,
   then `set_parent(...)` once the real window exists — same pattern as
   `window.py` Authorized Keys).
2. After `summary = client.deploy_key(...)`:
   `dialogs.set_session(SessionId(str(summary.operation_id)))` — replay (Phase 1)
   picks up prompts created before the client learned the operation ID.
3. Teardown: `dialogs.close()` when the operation reaches a terminal state
   (`succeeded`/`failed`/`cancelled`) or the window is closed / cancelled.

### Phase 4 — Remember-password ordering (daemon)

File: `src/sshpilot/daemon/identity_service.py` `_run_deploy`

1. Reorder so authentication commits happen before cleanup:
   - on `returncode == 0`: `self._broker.mark_authenticated(scope_id)`;
   - keep `cancel_session(scope_id)` in the `finally`.
2. The existing `mark_authenticated` guard (`context is not None`) becomes
   effective once the context still exists.

### Phase 5 — Audit remaining `prepare_operation_launch` callers

1. `native_scp_backend.py` (`scp-…` scope): prompts are currently invisible (F7).
   Decide: (a) register the scope as owned by the initiating client in a small
   ownership map consulted by `_client_can_interact`, or (b) wrap SCP transfers in
   an operation. Recommended: (b) for transfers already exposed as operations,
   else (a).
2. `privileged_file_service.py`: verify every caller passes an eligible scope
   (`SftpServiceRuntime` should pass its **sftp service id** so the file-manager
   presenter can answer the sudo-password prompt). Fix any caller that passes a
   private/operation-style scope.
3. `identity_service._run_remote_text`: when invoked from an operation
   (`remove_authorized_key`), pass `SessionId(str(handle.operation_id))`. The
   synchronous `list_authorized_keys` path needs either an operation wrapper or a
   public per-call scope with eligibility registration.
4. `identity_service._run_ssh_add`: same decision; recommended: expose the
   passphrase-prompt flow as a short operation so `ssh-add` prompts surface.

### Phase 6 — Tests

New/updated tests (see F13 for current gaps):

- Presenter (GUI harness or fake client/bridge unit test):
  1. interaction created **before** `set_session` is presented after bind (replay);
  2. interaction created **after** bind is presented;
  3. unrelated session while unbound is **ignored**;
  4. two simultaneous SFTP sessions don't steal each other's prompt;
  5. interaction already claimed (responder set) is not re-presented;
  6. replay is idempotent across repeated `set_session` calls.
- Daemon eligibility:
  - operation-scoped interaction visible to the owning client (event delivered,
    `list`/`claim` succeed);
  - non-owner client rejected on `list`/`claim`.
- Identity service (`tests/daemon/test_identity_service_phase.py`):
  - deploy scopes interactions with `handle.operation_id`;
  - successful deploy with `STORE_AFTER_SUCCESS` actually stores (mark before cancel);
  - failed deploy does not store.
- Parallel routing: File Manager + Authorized Keys + ssh-copy-id prompts route
  exclusively to their own presenter (the test the review singled out).

### Phase 7 — Validation

1. Targeted suites:
   `pytest tests/daemon/test_identity_service_phase.py tests/daemon/test_interaction_openssh.py tests/daemon/test_privileged_file_service.py tests/daemon/test_native_scp_backend.py tests/test_window_ssh_copy_id.py tests/gui/test_phase14_auth_dialogs.py`
   plus all new tests from Phase 6.
2. Full `pytest` run when the targeted suites pass.
3. Code review of the diff (`code-reviewer-deepseek-flash`).

## 4. Risks & notes

- **Replay double-presentation**: must skip interactions with `responder_client_id`
  set; `broker.list()` returns CLAIMED interactions, so this check is required.
- **Eligibility must stay owner-only**: `operation_runtime.client_can_interact`
  must never expose an operation to a non-owner client (F7 is the guardrail).
- **Do not** change the broker's `claim`/visibility internals; keep eligibility
  policy in `server._client_can_interact`.
- The Authorized Keys flow itself (F5) does not need restructuring — its
  pre-bind window is synchronous — but it must keep working once the wildcard is
  removed (replay covers the intended handshake prompt).
- `SecretsInteractionPresenter` is out of scope (F11).
