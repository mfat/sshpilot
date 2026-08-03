# Core ownership migration (M1–M8)

Goal of this workstream: **`sshpilot.core` is no longer treated as equivalent
to daemon ownership.** The daemon owns all authoritative state and I/O; the GTK
frontend renders and validates pure logic only.

Two consequences of that rule are audited and enforced here:

1. A module being GTK-free does **not** mean GTK should own an instance of it.
   The authoritative copy of every connection, key, known-hosts entry, secret,
   config key, backup, SSH subprocess and plugin runtime belongs to the daemon.
2. Pure helpers (validation, classification, naming, formatting) stay in
   `sshpilot.core` and may be called directly from the frontend — they are the
   exception, not the default.

The enforcement backbone is the AST test `tests/architecture/test_core_boundary.py`.
It admits frontend core imports only through the explicit `ALLOWED` allowlist or
the `PENDING_MIGRATIONS` registry below, forbids frontend → `sshpilot.daemon`
imports outside a tiny utility allowlist, and keeps `core`/`api`/`daemon`
GI-free.

## Definition of done

The workstream is complete when every row below is `Complete`:

- GTK does not instantiate `KeyService`, `ConnectionService`, or `SecretManager`,
  does not write config / known-hosts / key files, and does not spawn SSH or
  secret subprocesses.
- `key_manager.py`, `known_hosts_editor.py`, `connection_manager.py`,
  `secret_storage.py`, `backup_manager.py`, `plugins/api.py`, `plugins/host.py`,
  `ssh_connection_builder.py` and the headless SCP/SFTP/identity/multiplex
  helpers act as presenters/adapters over the daemon API.
- Passphrases/secrets are excluded from `repr`, logs and events; private-key
  contents are never serialized by the API.
- `tests/architecture/test_core_boundary.py::ALLOWED` grows only for genuinely
  pure helpers, and `PENDING_MIGRATIONS` is empty (every pending import routed
  through the daemon).

## Completion matrix

| Tag | Migration | Frontend today | Daemon target | Status | Evidence |
| --- | --- | --- | --- | --- | --- |
| M1 | Key generation + directory discovery | `key_manager.py` instantiates `core.keys.KeyService`, runs `ssh-keygen`, scans `~/.ssh` | Daemon owns key files and `ssh-keygen`; API lists/generates/deletes keys | **Deferred** | See M1 deferral |
| M2 | Known-hosts file ownership | `known_hosts_editor.py` calls `core.known_hosts.load/save_known_hosts` from GTK | Daemon API list/remove/apply with a revision token; GTK renders entries and sends mutations | **Deferred** | See M2 deferral |
| M3 | Connection store ownership | `connection_manager.py` instantiates `core.connections.ConnectionService` (`_domain`) and writes `~/.ssh/config` | Daemon is the authoritative store; GTK uses `ConnectionApplicationService` through the client | **Deferred** | See M3 deferral |
| M4 | Settings / config JSON ownership | `config.py` (GTK `Config`) loads/saves the config JSON via `core.settings` | Daemon owns persistent `ssh.*`/preferences keys; GTK keeps visual keys | **Deferred** | See M4 deferral |
| M5 | Secrets backend selection + vault state | `secret_storage.py` owns `SecretManager` + backend selection via `core.secrets` | Daemon owns backend/lookup/store; GTK is an interaction presenter | **Deferred** | See M5 deferral |
| M6 | Backup / import-export | `backup_manager.py` plans restores and writes files via `core.import_export` | Daemon backup/restore operations | **Deferred** | See M6 deferral |
| M7 | SSH-process / askpass broker | `ssh_connection_builder.py` builds native SSH commands + askpass env for GTK-spawned processes | Daemon one-shot/streaming/session commands via the interaction broker | **Deferred** | See M7 deferral |
| M8 | Plugin runtime | `plugins/api.py` + `plugins/host.py` re-export core plugin contracts and run a frontend host | Daemon plugin runtime for backend ops; GTK keeps UI contributions only | **Deferred** | See M8 deferral |

## Deferral records

Each entry names what is done, why it is deferred (with the specific blocker),
and the exit condition that removes the `PENDING_MIGRATIONS` rows for that tag.

### M1 — Keys

**Registered in `PENDING_MIGRATIONS`:** `key_manager.py` ×
`core.keys.{KeyGenerateSpec, KeyService, SSHKeyInfo}`.

Pure key sniffing (`key_utils.py` × `core.keys.{SKIPPED_FILENAMES,
is_private_key, looks_like_private_key}`) is `ALLOWED` and stays local; the
matrix classifies it `MIXED_NEEDS_SPLIT` — sniffing is presentation, directory
discovery/generation is daemon.

**Done now:** classification captured; the GTK adapter and its `KeyService`
instantiation are identified as the single migration point.

**Deferred because:** a real daemon key API needs typed request/result models
(`ListKeysResult`, `GenerateKeyRequest/Result`, `DeleteKeyRequest/Result`),
codec functions, capability entries, client + `DaemonClient` methods, dispatch
handlers, and a daemon-side `KeyService` instance — plus regeneration of the
strict `tests/api/snapshots/public_api.json` and the exact capability/error
marker sets asserted by `tests/api/test_api_documentation.py`. That is the M2
reference-migration scope, done once, then mirrored. It is deliberately the
*next* increment, not this one.

**Exit condition:** no `KeyService` instantiation or `ssh-keygen` invocation
remains in GTK; key generation/listing/deletion go through `client.*`; the four
M1 rows leave `PENDING_MIGRATIONS`.

### M2 — Known-hosts

**Registered in `PENDING_MIGRATIONS`:** `known_hosts_editor.py` ×
`core.known_hosts.{load_known_hosts, save_known_hosts}`.

`KnownHostEntry.parse` and `filter_entries` are `ALLOWED` (pure parse/filter for
rendering), matching the matrix row `PURE_FRONTEND_SAFE`.

**Done now:** classification captured; the editor's read (list) and write (save)
touch points are identified.

**Deferred because:** this is the *reference migration* for the whole stream —
it establishes the revision/generation token + structured-conflict pattern that
M3–M8 reuse, so it must be done carefully with a full API cycle and docs
updates in the same change. The same generated-artifact regeneration applies as
for M1.

**Exit condition:** `known_hosts_editor.py` renders entries received from the
daemon and sends remove/apply mutations with a revision token; it never calls
`load_known_hosts`/`save_known_hosts`; the two M2 rows leave `PENDING_MIGRATIONS`.

### M3 — Connections

**Registered in `PENDING_MIGRATIONS`:** `connection_manager.py` ×
`core.connections.ConnectionService`.

**Deferred because:** `ConnectionManager` already delegates mutations to
`ConnectionApplicationService` over the API for the daemon runtime
(`_production_core_services` in `daemon/cli.py`), so this row is about
shrinking the in-GTK `_domain` instance and the `~/.ssh/config` writer in
`connection_manager.py` — not about proving the daemon can own connections.
The config writer interacts with M7 (SSH config authority) and requires the
effective-config (`ssh -G`) ownership decision, so it is sequenced after M2.

**Exit condition:** GTK no longer instantiates `ConnectionService`; the
`~/.ssh/config` writer lives behind a daemon operation; the M3 row leaves
`PENDING_MIGRATIONS`.

### M4 — Settings / config JSON

**Registered in `PENDING_MIGRATIONS`:** `config.py` ×
`core.settings.{CONFIG_VERSION, ensure_config_defaults, get_default_config}`.

`compose_ssh_overrides` (Preferences ▸ SSH Settings) is `ALLOWED` — pure
composition. The pending rows are the defaults/store entry points used by the
GTK `Config` object that reads/writes the config JSON.

**Deferred because:** persistent settings ownership should land with M3's
daemon store so the "which keys does GTK keep" split is decided once against a
single daemon config authority; also several frontend features read `Config`
directly today, so this migration is wider than a single widget.

**Exit condition:** daemon owns persistent `ssh.*`/preferences keys; GTK keeps
visual-only keys; the M4 rows leave `PENDING_MIGRATIONS`.

### M5 — Secrets

**Registered in `PENDING_MIGRATIONS`:** `secret_storage.py` ×
`core.secrets.{normalize_backend_name, platform_default_order, decide_unlock,
SecretDecisionKind}`.

**Deferred because:** the secret subsystem already routes *runtime* lookups
through the daemon/askpass broker, but `SecretManager`'s backend selection and
unlock state live in the GTK process and are called during connection flows.
Moving selection/state to the daemon must not disturb the passphrase/password
askpass path, the keepassxc/bitwarden session-backed backends, or the
`use-askpass` gate — a large behavioral surface that needs its own test cycle.

**Exit condition:** daemon owns backend selection/lookup/store; GTK presents
unlock interactions only; the M5 rows leave `PENDING_MIGRATIONS`.

### M6 — Backup / import-export

**Registered in `PENDING_MIGRATIONS`:** `backup_manager.py` ×
`core.import_export.{MergeStrategy, plan_import, atomic_write_json,
migrate_payload}`.

**Deferred because:** restore planning and atomic file writes are authoritative
I/O, but the backup format/planning code is shared with the CLI and the daemon
already consumes `core.import_export`; this migration is mostly routing
`backup_manager`'s *apply* step through a daemon operation, which depends on M3
(connection store ownership) to know where restored data is written.

**Exit condition:** restore/backup writes run on the daemon; GTK only previews
and confirms; the M6 rows leave `PENDING_MIGRATIONS`.

### M7 — SSH-process / askpass broker

**Registered in `PENDING_MIGRATIONS`:** `ssh_connection_builder.py` ×
`core.ssh.{ProcessSpec, AuthMethod, HostKeyMode, LaunchMode, SSHLaunchRequest,
build_ssh_process_spec}`.

**Deferred because:** production *sessions* already run through the daemon
interaction broker; what remains is routing the **explicit command builders**
(SCP, ssh-copy-id, plugin `run_command`, SFTP listing, external-terminal
commands) through daemon operations so GTK stops assembling argv/env itself.
This is the widest migration and intentionally last in the sequencing.

**Exit condition:** every command-based caller gets its argv/env from a daemon
operation; `build_native_command` / `_build_base_ssh_command` live behind the
API; the M7 rows leave `PENDING_MIGRATIONS`.

### M8 — Plugins

**Registered in `PENDING_MIGRATIONS`:** `plugins/api.py` ×
`core.plugins.{API_VERSION, Capability, FieldSpec, SpawnSpec}`; `plugins/host.py`
× `core.plugins.{ALL_EVENTS, ConnectionInfo, EventBus, Events, SessionInfo}`.

**Deferred because:** plugin *contracts* (models, capability flags) are legitimately
imported by both sides — that is the shared language, not authoritative I/O —
but `plugins/api.py` also contains direct `subprocess.run`/`Popen` spawn helpers
used by plugin backends. Splitting "contracts" (stay) from "spawn runtime"
(move to a daemon plugin host) requires an SDK-level change with `docs/PLUGIN_SDK.md`
updates and plugin-compat tests; sequenced after M7 because it reuses the daemon
command API.

**Exit condition:** GTK plugin host holds only UI contributions and pure
contracts; plugin backend spawns run on the daemon; the M8 rows leave
`PENDING_MIGRATIONS`.

## Enforcement summary (current state)

`tests/architecture/test_core_boundary.py` is green on the baseline tree:

```text
ALLOWED (pure, stays local) ........ 31 symbols
PENDING_MIGRATIONS (must route to daemon)
  M1=3  M2=2  M3=1  M4=3  M5=4  M6=4  M7=6  M8=9
DAEMON_ALLOWLIST ................... 2 diagnostic utilities
```

The suite fails when a new frontend core import is unregistered, when a
registered entry becomes stale, or when frontend reaches into
`sshpilot.daemon` outside the allowlist. It does **not** yet forbid the pending
imports themselves — that is the job of each M1–M8 migration, which removes its
rows as it lands.
