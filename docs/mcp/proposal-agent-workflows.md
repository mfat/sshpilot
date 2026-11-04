# Proposal: SSH Pilot–specific agent workflows over runtime MCP

Status: **Proposed — not implemented.** Written 2026-11-04 against `dev`
(a3bb1320). Nothing here is accepted until it is moved into
[decisions.md](decisions.md).

## Why this proposal exists

The runtime MCP server today (`src/sshpilot/mcp/runtime/server.py`) exposes
sessions, SFTP metadata/file operations, transfers, forwards, operations and
interactions. Almost all of that is something an AI agent can already do with
plain `ssh`/`sftp` from its own shell — with the user's full SSH authority, no
host scoping and no visibility. A runtime MCP that only re-exposes those
primitives adds attack surface without adding value.

This proposal covers three areas where SSH Pilot owns something plain `ssh`
does not, and where routing an agent through SSH Pilot can be both **more
useful and safer** than the agent's own shell:

1. **Human-in-the-loop authentication** — the agent can reach hosts it could
   not log into itself, while every secret and trust decision stays with the
   human in the SSH Pilot window.
2. **Connection inventory management** — creating, organizing and fixing the
   saved connections, groups and tags that exist only in SSH Pilot.
3. **Fleet operations** — key rotation, `authorized_keys` audits and
   multi-host commands using SSH Pilot's knowledge of which hosts and keys go
   together.

Each section states what the code does **today** (with references), the gap,
the proposed design, the safety rules, and the tests required before any UI
exposure.

Out of scope: the in-app HTTP listener and activity panel proposed in PR #1315,
and the shared live terminal (`terminal.replay` / `claim_input`), which deserves
its own proposal.

---

## Cross-cutting prerequisites

These apply to all three areas and should land first.

### P1. The daemon should know a client is an agent

`DaemonClient` already sends a `frontend_type` in the handshake
(`api/daemon_client.py:630`, wire field in `api/transport/codec.py:1666`). The
GTK frontend sends `"gtk"` (`daemon/launcher.py:627`), the CLI sends `"cli"`
(`daemon/cli.py:120`). The runtime MCP server constructs
`DaemonClient(socket_path=...)` with **no** `frontend_type`
(`mcp/runtime/server.py:638`), and the daemon does not read `frontend_type`
anywhere.

Proposal: the runtime MCP client identifies as `frontend_type="mcp"`, and the
daemon records it on the client state so it can (a) attribute sessions,
operations and interactions to an agent, and (b) route agent-originated prompts
to a human frontend (see area 1).

This is a **label, not a security boundary.** Any same-uid process can open the
daemon socket and claim any `frontend_type`. The threat model for runtime MCP
is a mistaken or prompt-injected *model*, not a malicious local process — a
local process with the user's uid already has the user's SSH keys. The label
lets the daemon present agent activity honestly; enforcement of what the model
may do stays in the MCP server's single choke point (P2).

### P2. One authorization choke point in the MCP server

Every tool today calls `RuntimeHandle._require(level)` (`server.py:52`) and
MUTATE tools call `_mutate(...)` (`server.py:67`). Per-connection scoping
(an allowlist of connections the agent may touch) must be added **in one
place** that resolves every id a tool receives — `connection_id`,
`session_id`, `service_id`, `transfer_id`, `forward_id`, `operation_id`,
`interaction_id` — back to its owning connection before the call is made.
Checking only the `connection_id` argument tool-by-tool is how PR #1315 ended
up with tools that bypass its allowlist and a transfer tool that could pair an
allowed connection with another host's SFTP service.

A table-driven test must assert that every registered tool goes through the
choke point (the existing D004 drift tests in `tests/architecture` are the
model for this).

### P3. Fail closed

Any error while loading policy or allowlist state denies the call. No
"legacy/unrestricted" fallback.

---

## Area 1 — Human-in-the-loop authentication

### User story

> "Check disk usage on `db-legacy`." The host uses a password plus a TOTP code.
> The agent asks SSH Pilot to connect; SSH Pilot pops its normal password/2FA
> dialog in front of the user, labelled as an agent request; the user types the
> code; the agent gets a working session. The agent never sees the password or
> the code, and the user can refuse.

Plain `ssh` from an agent's shell cannot do this: it hangs on the TTY prompt
or fails under `BatchMode`.

### What the code does today

**Prompts are typed and secret-free on the wire.**
`api/models/interactions.py` defines `InteractionType` (`HOST_KEY_CONFIRMATION`,
`PASSWORD`, `PRIVATE_KEY_PASSPHRASE`, `KEYBOARD_INTERACTIVE`,
`SECURITY_KEY_PRESENCE`, `CONFIRMATION`), a claim protocol
(`InteractionClaim` with a nonce, line 301) and typed decisions
(`InteractionDecisionRequest`, line 321, with `HostKeyDecision`,
`SecretDecision` and a `RememberPolicy`). Secrets travel on a separate
protected path, not in the decision. This is exactly the shape needed.

**Stored secrets are autofilled before any prompt is raised.**
`InteractionBroker._resolve_askpass_secret` (`daemon/interaction_broker.py:1692`)
tries the stored password/passphrase first (`try_stored`, ~line 1763) and only
creates an interaction if there is none. The switch is
`allow_stored_secrets` on the askpass context (line 157), settable per launch
via `SshLauncher` (`daemon/ssh_launch.py:510`).

> **Finding:** today an MCP client with OPERATE can call `open_session` on a
> host whose password is in the keyring and be logged in silently with the
> user's stored password — no human sees anything. That is a policy gap the
> proposal closes, not a feature to keep by accident.

**Prompts are only visible to the session's originator or attached clients.**
`InteractionBroker._visible_locked` (line 2258) defers to
`DaemonServer._client_can_interact` (`daemon/server.py:1114`), which for a
session asks `SessionRuntime.client_can_interact`
(`daemon/session_runtime.py:1301`): true only for the
`originating_client_id` or a client with an attachment.

**The GTK app never presents prompts for sessions it did not start.**
`DaemonInteractionDialogs._is_ours` (`daemon_interaction_dialogs.py:102`) only
claims interactions whose `session_id` matches the scope the presenter was bound
to by the GTK code that launched it. `main.py` receives `SESSION_CREATED`
events (~line 719) but only records them for diagnostics.

**Net effect today:** an agent opens a session to a password/2FA host → the
interaction is visible only to the MCP client → MCP exposes `claim`/`release`/
`cancel` but (correctly) no `respond` → nobody can answer → it expires after
`DEFAULT_SECRET_INTERACTION_TIMEOUT` (120 s) or
`DEFAULT_HOST_KEY_INTERACTION_TIMEOUT` (180 s)
(`interaction_broker.py:69-70`). The existing host-key test
(`tests/mcp/test_runtime_hostkey_stdio.py`) only succeeds because the test's
"trusted frontend" explicitly attaches to the session first.

### Gap

There is no daemon concept of "this prompt came from an agent's session; show
it to the human". The building blocks (typed prompts, claim nonces, protected
secret path, per-launch `allow_stored_secrets`, per-launch
`ExecutionInteractionMode`) all exist.

### Proposed design

1. **Delegated approval scope.** When a client whose `frontend_type` is
   `"mcp"` (P1) originates a session, SFTP service, forward or operation, the
   daemon also makes its interactions visible to a designated *approver*: any
   connected `gtk` client. Implemented as one more branch in
   `_client_can_interact`, not a parallel mechanism.
2. **The agent cannot answer, only wait.** The MCP client keeps read-only
   visibility (`list_interactions`/`get_interaction`, redacted per D010) and may
   `cancel` its own prompt. `claim`/`release` are removed from the agent
   surface: claiming would let the agent block the human's dialog.
3. **GTK presenter for agent prompts.** A new presenter, modelled on the
   connection-scoped `BackupServerInteractionPresenter` and registered in the
   `_INTERACTION_OWNERS` registry (`daemon_interaction_dialogs.py:48`), claims
   delegated interactions and shows the existing password / passphrase /
   host-key / keyboard-interactive / security-key dialogs with an
   **"Requested by an AI assistant"** header naming the connection and the
   tool call. No new dialog types.
4. **Stored-secret autofill for agent sessions is a user setting, default
   off.** Agent-originated launches pass `allow_stored_secrets=False` unless
   the user enables "Let AI assistants use saved passwords" (per connection or
   global). With it off, a saved password still produces a prompt — but the
   dialog can offer "Use saved password" as a one-click approval.
5. **No human frontend → fail fast.** If no `gtk` client is connected, the
   agent's launch uses `ExecutionInteractionMode.AUTOFILL_ONLY` semantics with
   `allow_stored_secrets=False`, i.e. key/agent auth only, and returns a
   stable error code ("approval needed; open SSH Pilot") instead of hanging for
   120 s.
6. **`RememberPolicy` is always the human's choice.** The agent can never set
   it; the dialog defaults to `DO_NOT_STORE` for agent prompts.

Everything goes through the existing `SshLauncher` path and interaction
broker — no new askpass, `Popen` or secret handling (per CLAUDE.md and
`AGENTS.md`).

### Safety rules

- No MCP tool ever accepts or returns a secret, a claim nonce or a remember
  policy (`reveal_password`, `reveal_passphrase`, `get_plugin_secret`,
  `secrets.*`, `set_session_password`, `interactions.respond` stay unexposed;
  D003).
- Host-key `CHANGED`/`REVOKED` prompts from agent sessions are shown with the
  existing warning styling and default to Reject.
- Every delegated prompt and its outcome is logged with the agent label.

### Tests required before UI exposure

- Daemon unit: delegated visibility — `gtk` client sees an `mcp`-originated
  interaction; a second `mcp` client does not; the originating `mcp` client
  can list but cannot claim or respond.
- Daemon unit: `allow_stored_secrets=False` for agent launches by default;
  `True` only when the setting is on.
- Integration (existing Alpine sshd fixture used by
  `tests/mcp/test_runtime_operate_stdio.py`): agent opens a password session;
  a headless approver client answers; session reaches RUNNING; the MCP stdio
  transcript contains no secret bytes (grep the captured stream).
- Integration: unknown host key from an agent session → approver rejects →
  session fails with the stable code, `known_hosts` unchanged.
- Integration: no approver connected → fast stable error, not a 120 s
  timeout.
- GTK: presenter claims only delegated interactions, never a terminal's own
  (registry exclusivity, same pattern as the backup presenter's tests).

---

## Area 2 — Connection inventory management

### User stories

> "Add these 30 hosts from `inventory.yaml` into a *Production* group, tag the
> web servers `web`, colour the group red."

> "Find connections whose hostnames no longer resolve and put them in
> *Stale*."

> "Why does `staging` connect to the wrong IP?" → the agent compares SSH
> Pilot's view with OpenSSH's effective config and proposes the fix.

Groups, tags, colours, layouts and plugin protocols exist only in SSH Pilot;
an agent editing `~/.ssh/config` cannot reach them.

### What the code does today

The daemon API already has a complete, typed, generation-checked inventory
surface (none of it is exposed over MCP):

| Need | Client method | Notes |
| --- | --- | --- |
| Create | `create_connection` (`daemon_client.py:990`) | `CreateConnectionRequest` (`models/connections.py:712`): nickname validated as an SSH host alias, `config_patch`, `plugin_data` |
| Edit | `update_connection` (`:1047`) | `UpdateConnectionRequest` (`:752`) is presence-aware and carries `expected_generation` to reject stale edits; `config_patch` additionally requires `CONNECTIONS_CONFIG_WRITE` |
| Duplicate / delete | `duplicate_connection` (`:1071`), `delete_connection` (`:1086`), `delete_connections` (`:1099`) | |
| Groups | `create_group` (`:1384`), `assign_connection_to_group` (`:1369`), `place_group` (`:1273`), `set_group_color` (`:1261`) | |
| Tags / metadata | `update_connection_metadata` (`:1246`) | |
| Import | `preview_asbru_import` (`:1023`), `import_asbru` (`:1034`) | preview-then-apply already exists |
| Diagnose | `get_effective_config` (`:862`) → `EffectiveConfigComparison` (`models/connections.py:441`), `check_unsaved_host` (`:872`) | |

**Risk found in the editable surface.** `ConnectionEditorDetails`
(`models/connections.py:534`) includes `proxy_command` (line 554),
`pre_command` (558), `local_command` (559), `remote_command` (560),
`forwarding_rules` (556) and `extra_ssh_config` (563). `ProxyCommand`,
`LocalCommand` and the pre-connection command **run on the user's machine**
when the connection is next opened — by the human. An agent that can write
those fields can plant a command that the user later executes unknowingly.
`forward_agent` (551) can expose the user's agent to a remote host.

### Proposed design

1. **Two tiers of edits.**
   - *Organizational* (low risk): nickname/display name, groups, group
     colour/placement, tags/metadata, duplicate. OPERATE level.
   - *Connection-affecting* (MUTATE + human approval): hostname, user, port,
     identity files, `proxy_jump`, and anything in `config_patch`.
   - *Never via agent*: `proxy_command`, `pre_command`, `local_command`,
     `remote_command`, `extra_ssh_config`, `forward_agent`, forwarding rules,
     `plugin_data` script-like fields. The MCP tool schema simply does not
     include them, and a test pins that list.
2. **Plan, then apply.** Bulk changes are a two-step tool pair:
   `plan_inventory_change(...)` returns a typed diff (reusing the
   preview/apply pattern of `preview_asbru_import`/`import_asbru`);
   `apply_inventory_change(plan_id)` applies it with `expected_generation` for
   every touched connection, so a concurrent human edit makes the apply fail
   rather than overwrite.
3. **Human approval in the app for MUTATE-tier plans.** The plan is shown as a
   diff in SSH Pilot (connections added / changed / removed); the user
   approves or rejects there. This replaces D009's model-filled
   `confirm=true` for this area, as D009 anticipated ("a future human-review
   layer").
4. **Deletion is soft.** Agent-initiated deletes move connections to a
   recoverable state (or require approval per batch); no silent
   `delete_connections` of 50 hosts.
5. **Read-only diagnosis first.** `get_effective_config` and
   `check_unsaved_host` are READ tools and can ship before any write tool.
   `get_ssh_config_text` stays redacted (D010).

### Tests required before UI exposure

- Schema test: agent tools cannot carry any field in the "never via agent"
  list (fails if a new `ConnectionEditorDetails` field is added without
  classification — forces a decision per field).
- Generation test: apply with a stale generation fails and changes nothing
  (all-or-nothing per plan).
- Integration: plan → approve → apply round-trip against a sandbox profile
  (`SSHPILOT_SSH_DIR` + `XDG_*`, per the sandbox notes), then verify the
  written `ssh_config` and the sidebar state.
- Rejection path: rejected plan leaves files byte-identical.

---

## Area 3 — Fleet operations

### User stories

> "Which of my Production servers still have the old `laptop-2019` key in
> `authorized_keys`?"

> "Generate a new ed25519 key, deploy it to the *Production* group, verify I
> can log in with it, then remove the old key everywhere."

> "Run `df -h /` on every web host and summarise."

### What the code does today

| Need | Client method | Notes |
| --- | --- | --- |
| Audit `authorized_keys` | `list_authorized_keys` (`daemon_client.py:3044`) | `ListAuthorizedKeysRequest` (`models/identity.py:366`) |
| Remove one key line | `remove_authorized_key` (`:3064`) | `RemoveAuthorizedKeyRequest` (`identity.py:376`) requires the `file_revision` from the listing — the remote file must be unchanged since it was read |
| Deploy a key | `deploy_key` (`:3022`) | `DeployKeyRequest` (`identity.py:268`): native `ssh-copy-id`, key by daemon key id or a pasted public key |
| Generate a key | `generate_key` (`:1981`) | `GenerateKeyRequest` (`models/keys.py:172`): daemon-owned `ssh-keygen`, optional passphrase via interaction scope |
| List keys / public key | `list_keys` (`:1949`) | |
| Known hosts | `list_known_hosts` (`:1924`) | |
| Multi-host command | `start_broadcast_command` (`:3124`) | `BroadcastCommandRequest` (`models/broadcast.py:80`): ≤ 256 targets, ≤ 64 KiB command; `BroadcastExecutionPolicy` (line 34): `concurrency_limit` 4, `timeout_seconds`, output cap 1 MiB, `failure_policy`, `interaction_mode` |

These are already the right primitives: revision-checked removal, daemon-owned
`ssh-keygen`/`ssh-copy-id`, bounded broadcast with per-host results and an
interaction mode. Operations surface prompts on `operation-…` scopes
(`daemon/server.py:1135`), which Area 1's delegated approval would cover.

### Proposed design

1. **Read tools first (READ):** `list_keys`, `list_known_hosts`,
   `list_authorized_keys` (per allowed connection). Key *material* other than
   public keys is never returned.
2. **Guided multi-step workflows instead of raw primitives.** A key rotation
   is exposed as one plan the human approves as a whole:
   `plan_key_rotation(group, old_key_fingerprint, new_key)` → shows: hosts,
   which have the old key, deploy targets, removal targets →
   human approves in SSH Pilot → the daemon executes per host:
   deploy → **verify login with the new key** → remove old line using the
   listing's `file_revision`. A host that fails verification keeps its old key.
   This ordering prevents the classic lock-out.
3. **Broadcast as the only "run a command" path for fleets**, always with:
   explicit target list (resolved through P2's allowlist), a timeout, the output
   cap, `failure_policy` chosen by the agent but shown to the human, and human
   approval of the exact command text and target list before start. Output is
   subject to D010-style redaction unless content is enabled.
4. **`generate_key`** is allowed (it creates a new local file, nothing is
   destroyed) but the passphrase prompt, if any, is a delegated interaction
   (Area 1). `delete` of keys is never exposed.

### Tests required before UI exposure

- Integration against the Alpine sshd fixture with two containers: rotation
  where host B rejects the new key → B keeps the old key, A is rotated; the
  run report says so.
- Revision race: modify `authorized_keys` between listing and removal →
  removal refused, nothing deleted.
- Broadcast: target outside the allowlist is rejected before any SSH process
  starts; timeout and output cap are honoured; unapproved broadcast never
  launches.

---

## Proposed decision record changes

If accepted, add to [decisions.md](decisions.md):

- **D011 — Agent clients are labelled** (`frontend_type="mcp"`); the label
  drives attribution and prompt delegation, never authorization.
- **D012 — Agent-originated prompts are answered by a human frontend**; agents
  may observe and cancel, never claim or respond; stored-secret autofill for
  agent launches is off by default.
- **D013 — Human approval for MUTATE-tier plans happens in SSH Pilot**,
  refining D009: `confirm=true` remains for the CLI-only stdio deployment, the
  in-app approval replaces it when a GTK frontend is connected.
- **D014 — Fields that execute locally are never agent-writable**
  (`proxy_command`, `pre_command`, `local_command`, `remote_command`,
  `extra_ssh_config`, `forward_agent`, forwarding rules).

(PR #1315 also claims a D012; numbering to be reconciled when either lands.)

## Suggested order

1. P1–P3 and the read-only tools (effective config, known hosts, keys,
   `authorized_keys` listing). Lowest risk, immediately useful, and they
   exercise the allowlist choke point.
2. Area 1 (delegated approval). It is the foundation the other write paths
   rely on for approvals and prompts.
3. Area 2 organizational edits, then connection-affecting plans.
4. Area 3 rotation and broadcast workflows.

UI exposure (Preferences, activity view) comes after step 2, and only for the
tools whose tests above are green.
