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

The enforcement backbone is `tests/architecture/test_core_boundary.py`
(frontend core-import allowlist + pending registry + daemon-import allowlist +
frontend backend-operations registry) and
`tests/core/test_dependency_boundary.py` (package-edge enforcement for
core/api/daemon, including the daemon's transitive GObject-adapter debt).

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
- `BACKEND_OPS` (frontend backend operations) contains no `M#` entries, and the
  daemon's GObject-adapter imports in `tests/core/test_dependency_boundary.py`
  (`DAEMON_DEBT` / `CORE_DEBT`) are gone.
- The daemon runtime is GI-free: `_production_core_services` composes only
  headless services, never `Config` / `ConnectionManager` / `GroupManager` /
  `platform_utils`.

## Completion matrix

| Tag | Migration | Frontend today | Daemon target | Status | Evidence |
| --- | --- | --- | --- | --- | --- |
| M1 | Key generation + directory discovery | `key_manager.py` instantiates `core.keys.KeyService`, runs `ssh-keygen`, scans `~/.ssh` | Daemon owns key files and `ssh-keygen`; API lists/generates/deletes keys | **Deferred** | See M1 deferral |
| M2 | Known-hosts file ownership | `known_hosts_editor.py` calls `core.known_hosts.load/save_known_hosts` from GTK | Daemon API list/remove with a revision token; GTK renders entries and sends batched mutations | **Complete** | API cycle landed; editor routed through `KnownHostsController` |
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
marker sets asserted by `tests/api/test_api_documentation.py`. That is exactly
the reference-migration scope M2 established (known-hosts: models → codecs →
capabilities → RPCs → daemon service → client methods → controller → editor);
M1 mirrors that pattern.

**Exit condition:** no `KeyService` instantiation or `ssh-keygen` invocation
remains in GTK; key generation/listing/deletion go through `client.*`; the three
M1 rows leave `PENDING_MIGRATIONS`.

### M2 — Known-hosts

**Status: Complete.**

**Registered in `PENDING_MIGRATIONS`:** `known_hosts_editor.py` ×
`core.known_hosts.{load_known_hosts, save_known_hosts}` — **removed**.

**What landed:** the full reference API cycle the stream's later migrations
reuse — revision/generation token + structured-conflict pattern:

- API models (`KnownHostEntrySummary`, `KnownHostsSnapshot`,
  `RemoveKnownHostEntriesRequest`, `KnownHostsMutationResult`, `KnownHostEntryId`)
  with tuple-strict validation.
- Lossless document parsing (`core.known_hosts.document`: SHA-256 revision,
  per-revision deterministic entry IDs, comment/blank preservation) and atomic
  byte storage (`core.known_hosts.file_io`: symlink refusal, mode
  preservation, temp-file + parent-dir `fsync`).
- Wire codecs, `KNOWN_HOSTS_READ`/`KNOWN_HOSTS_WRITE` capabilities,
  `known_hosts.list` / `known_hosts.remove` RPCs with capability-gated
  advertisement, and the daemon `KnownHostsService` (one `RLock`, optimistic
  revision check, `stale_editor` on conflict).
- `DaemonClient.list_known_hosts` / `remove_known_host_entries`.
- `KnownHostsController` (GTK-free) + `known_hosts_editor.py` routed through it:
  rows store entry IDs (duplicates stay distinguishable), removals are staged
  and applied in one batched call, stale edits reload without retrying, and the
  editor never calls `load_known_hosts`/`save_known_hosts`, `get_ssh_dir`, or
  performs direct `open`/`Path` I/O.

**Exit condition (met):** `known_hosts_editor.py` renders entries received
from the daemon and sends batched revision-checked removals; it never calls
`load_known_hosts`/`save_known_hosts`; the M2 rows are gone from
`PENDING_MIGRATIONS` and `BACKEND_OPS`, and `KNOWN_HOSTS_*` capabilities are
advertised only when the daemon service is installed.

### M3 — Connections

**Registered in `PENDING_MIGRATIONS`:** `connection_manager.py` ×
`core.connections.ConnectionService`.

**Deferred because:** the daemon exposes connection operations through
`ConnectionApplicationService`, but that service still **delegates authoritative
work to the legacy GObject `ConnectionManager`** (it is composed *over* the
manager in `_production_core_services` / `daemon/cli.py`). So the debt is not
"GTK could own the store" — it is that the GObject adapter remains the
underlying implementation and is additionally instantiated in-GTK (`_domain`),
plus `ConnectionManager` still writes `~/.ssh/config` and `ConnectionApplicationService`
(in `core`) still reaches frontend helpers (`config`, `ssh_connection_builder`,
`plugins.api/registry` — registered in `CORE_DEBT`). M3 must replace both the
in-GTK store and the legacy adapter the service wraps, and move the config
writer behind a daemon operation. The config writer interacts with M7 (SSH
config authority) and requires the effective-config (`ssh -G`) ownership
decision, so it is sequenced after M2.

**Exit condition:** the daemon composes `ConnectionApplicationService` over a
headless service (no `ConnectionManager`, no `Config` import in `daemon/cli.py`),
GTK no longer instantiates `ConnectionService`, the `~/.ssh/config` writer lives
behind a daemon operation, and the `CORE_DEBT` rows for
`sshpilot.ssh_connection_builder` / `sshpilot.config` / `sshpilot.plugins` are
removed. The M3 row leaves `PENDING_MIGRATIONS`.

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

The plugin *contracts* (`plugins/api.py` × `core.plugins.{API_VERSION,
Capability, FieldSpec, SpawnSpec}`; `plugins/host.py` × `core.plugins.{ALL_EVENTS,
ConnectionInfo, EventBus, Events, SessionInfo}`) are **shared language, not
authoritative I/O**, and are therefore **allowed** in `ALLOWED` (they remain
importable from both sides, exactly as the matrix's `MIXED_NEEDS_SPLIT`
classification intends for the pure-contract half).

**Registered in `BACKEND_OPS`:** `plugins/api.py` × `subprocess`.

**Deferred because:** the real M8 violation is **local execution**, not the
model imports — `plugins/api.py` contains direct `subprocess.run`/`Popen` spawn
helpers used by plugin backends, plus local ControlMaster handling and direct
secret/settings/connection access. Splitting "contracts" (stay) from "spawn
runtime" (move to a daemon plugin host) requires an SDK-level change with
`docs/PLUGIN_SDK.md` updates and plugin-compat tests; sequenced after M7
because it reuses the daemon command API.

**Exit condition:** GTK plugin host holds only UI contributions and pure
contracts; `plugins/api.py` launches no subprocess (the `BACKEND_OPS`
`subprocess` row for `plugins/api.py` is removed); plugin backend spawns run on
the daemon. The M8 tag then has no debt.

## Enforcement summary (current state)

`tests/architecture/test_core_boundary.py` + `tests/core/test_dependency_boundary.py`
are green on the baseline tree:

```text
ALLOWED (pure, stays local) ........ 38 symbols
PENDING_MIGRATIONS (frontend core imports to route to daemon)
  M1=3  M3=1  M4=3  M5=4  M6=4  M7=6           (M2=0, M8=0: contracts are allowed)
BACKEND_OPS (frontend backend operations)
  M1=1  M3=1  M5=4  M6=1  M7=19  M8=1  frontend=6
DAEMON_ALLOWLIST ................... 7 app-side daemon bootstrap/diagnostic utilities
DAEMON_DEBT ....................... 5 daemon -> GObject-adapter imports (M3/M4/M8)
CORE_DEBT ......................... 3 core -> frontend-helper imports (M4/M7/M8)
```

The suite fails when a new frontend core import is unregistered, when a
registered entry becomes stale, when frontend performs an unregistered backend
operation (subprocess / SSH binary / service instantiation / known-hosts I/O),
when frontend reaches into `sshpilot.daemon` outside the allowlist, or when
`core`/`daemon` gain a dependency edge outside their allowed set and debt
registries. It does **not** yet forbid the pending imports/operations
themselves — that is the job of each M1–M8 migration, which removes its rows as
it lands.

## The daemon is not yet GI-free

The AST tests prove only that files under `core`/`api`/`daemon` contain **no
direct** Gtk/GLib/GI imports. The daemon *runtime* still depends on GI
transitively because `daemon/cli.py::_production_core_services` composes GObject
adapters:

```python
from sshpilot.config import Config                     # M4 debt
from sshpilot.connection_manager import ConnectionManager  # M3 debt
from sshpilot.groups import GroupManager               # M3 debt
from sshpilot.plugins.loader import load_plugins       # M8 debt
```

and `daemon/cli.py` + `daemon/launcher.py` import `platform_utils.get_state_dir`
(M4 debt) for the log path. These are registered in `DAEMON_DEBT`
(`tests/core/test_dependency_boundary.py`) and must be replaced by headless
daemon-owned services (or a GI-free `platform.paths` state-dir helper) before
the claim "the daemon runtime is GI-free" can be made. The tests reject any
*new* daemon → GObject-adapter edge.
