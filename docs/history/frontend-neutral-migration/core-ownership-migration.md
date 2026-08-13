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

Each migration row is marked independently. A completed row means its reviewed
owner, typed API boundary, frontend ownership, compatibility behavior, and
verification evidence are documented. Pending rows are not implied complete by
another row.

The shared enforcement rules remain:

- GTK does not instantiate migrated backend services, write authoritative
  config/known-hosts/key files, or spawn daemon-route SSH or secret subprocesses.
- Frontend controllers act as presenters/adapters over the daemon API; pure
  validation, classification, naming, and formatting helpers may remain local.
- Passphrases/secrets are excluded from `repr`, logs, events, and ordinary DTOs;
  private-key contents are never serialized by the API.
- Architecture tests record remaining compatibility debt and reject new
  frontend backend operations or forbidden dependency edges.
- The daemon’s remaining internal adapter debt is tracked explicitly and is not
  confused with the ownership of completed phases.

## Completion matrix

| Tag | Migration | Frontend today | Daemon target | Status | Evidence |
| --- | --- | --- | --- | --- | --- |
| M1 | Key generation + directory discovery | `key_manager.py` instantiates `core.keys.KeyService`, runs `ssh-keygen`, scans `~/.ssh` | Daemon owns key files and `ssh-keygen`; API lists/reads/generates keys (no deletion in M1) | **Complete** | API cycle landed; `KeyManager` is a client adapter; `keys.*` RPCs + capabilities live |
| M2 | Known-hosts file ownership | `known_hosts_editor.py` calls `core.known_hosts.load/save_known_hosts` from GTK | Daemon API list/remove with a revision token; GTK renders entries and sends batched mutations | **Complete** | API cycle landed; editor routed through `KnownHostsController` |
| M3 | Connection store ownership | GTK controllers retain snapshots and transient selection only | `ConnectionRepository` and `ConnectionApplicationService` own the daemon store, groups, metadata, SSH configuration, and events | **Complete** | `connections.*` API and repository contract tests |
| M4 | Settings / config JSON ownership | GTK retains visual settings and presents typed settings forms | `SshOverridesService` owns global `ssh.*` overrides; broader visual/config split remains separate debt | **Partial: global SSH overrides complete** | `ssh_overrides.*` API, revision-safe service, and contract tests |
| M5 | Secrets backend selection + vault state | GTK presents metadata and protected interactions only | `SecretBackendService` owns selection, configuration, lifecycle, and protected backend operations; `DaemonConnectionSecretProvider` supplies runtime access | **Complete** | `secrets.*` API, service tests, and boundary checks |
| M6 | Backup / import-export | GTK selects files and presents safe previews/results only | Daemon runs backup enumeration, export/import, and restore through existing backup implementations | **Complete** | `secrets.transfer` API and transfer tests |
| M7 | SSH-process / askpass broker | `ssh_connection_builder.py` builds native SSH commands + askpass env for GTK-spawned processes | Daemon one-shot/streaming/session commands via the interaction broker | **Deferred** | See M7 deferral |
| M8 | Plugin runtime | `plugins/api.py` + `plugins/host.py` re-export core plugin contracts and run a frontend host | Daemon plugin runtime for backend ops; GTK keeps UI contributions only | **Deferred** | See M8 deferral |

## Deferral records

Each entry names what is done, why it is deferred (with the specific blocker),
and the exit condition that removes the `PENDING_MIGRATIONS` rows for that tag.

### M1 — Keys

**Status: Complete.**

**Registered in `PENDING_MIGRATIONS`:** `key_manager.py` ×
`core.keys.{KeyGenerateSpec, KeyService, SSHKeyInfo}` — **removed**. The
`BACKEND_OPS` row `(key_manager.py, KeyService)` is **removed**.

Pure key sniffing (`key_utils.py` × `core.keys.{SKIPPED_FILENAMES,
is_private_key, looks_like_private_key}`) stays `ALLOWED` and local; the
matrix classifies it `MIXED_NEEDS_SPLIT` — sniffing is presentation, directory
discovery/generation is daemon.

**What landed:**

- The daemon resolves the active key directory per semantic scope
  (`KeyStoreScope.DEFAULT` → `get_ssh_dir()`, `KeyStoreScope.ISOLATED` →
  `get_config_dir()`); the frontend never sends or derives a key-directory
  path.
- The daemon creates key directories, recursively discovers private keys,
  runs `ssh-keygen`, and reads public-key files for application features
  (`DaemonKeyService` over `core.keys.KeyService`, keyed by stable opaque IDs).
- `keys.list` / `keys.get_public` / `keys.generate` /
  `keys.verify_passphrase` RPCs with
  `KEYS_READ` / `KEYS_WRITE` capabilities advertised only when the daemon key
  service is installed.
- `KeyManager` is a GObject compatibility adapter over `SshPilotClient`;
  GTK never instantiates `core.keys.KeyService`, never scans key directories,
  and never generates keys locally.
- Public-key text crosses the API only through `keys.get_public`; the
  authorized-keys local import uses `KeyManager.read_public_key()` and never
  opens a daemon-discovered `.pub` file.
- Path metadata on `KeySummary` is temporary compatibility data for the M7
  `ssh-copy-id` subprocess adapter; GTK does not derive or scan those paths,
  and user-browsed arbitrary public-key files remain explicit frontend input.
- Private-key contents never cross the API. Key-generation and verification
  passphrases use `InteractionBroker` secret frames and daemon askpass; they
  are absent from ordinary requests, native argv/environment values, logs,
  events, errors, and retained controller state.
- No deletion API was added because no existing GTK key-deletion workflow
  exists.

**Exit condition met:** no `KeyService` instantiation, `ssh-keygen`
invocation, or directory scan remains in GTK; key listing, public-key reads,
and generation go through `client.*`; the M1 rows are gone from
`PENDING_MIGRATIONS` and `BACKEND_OPS`.

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

**Status: Complete and reviewed.** `ConnectionRepository` and
`ConnectionApplicationService` own SSH configuration, included fragments,
`connections.json`, groups, ordering, safe metadata, and connection-store
events. SSH `Host` aliases remain connection identity. GTK receives immutable
snapshots and retains only visual group expansion state.

The former GObject managers remain compatibility implementations behind
specific in-process paths and API method descriptions; they are not the
production daemon store authority. The daemon composition root injects the
repository and application service directly, and the public contract maps
repository/application-service results into explicit DTOs.

**Exit condition met:** daemon-backed connection CRUD, group operations,
metadata updates, snapshot events, and SSH configuration persistence use the
repository/application-service boundary. Remaining compatibility debt is
tracked separately and does not change daemon ownership.

### M4 — Settings / config JSON ownership

**Status: Global SSH overrides complete and reviewed; broader settings split
remains pending.** `SshOverridesService` owns global `ssh.*` override
validation, normalization, revisioning, transaction locking, and persistence.
Preferences reads and writes those values through `ssh_overrides.*` typed API
methods and does not compose or persist a competing `ssh_overrides` list.

Visual preferences remain frontend-owned. Other persistent settings and legacy
`Config` compatibility rows remain tracked as separate migration debt; this
phase status does not claim the broader config JSON split complete.

**Exit condition for the completed slice met:** global SSH override reads,
updates, and resets are daemon-owned and revision-safe. The remaining settings
rows stay explicitly pending for a separate phase review.


### M5 — Secrets

**Status: Complete and reviewed.** `SecretBackendService` owns backend
selection, configuration, lifecycle, protected interactions, and secret
transfers. `DaemonConnectionSecretProvider` is the daemon boundary for runtime
connection secret access and reuses the existing `SecretManager` and backend
implementations internally. GTK presents typed state and protected
interactions only; secret values never become ordinary API data.

Explicit backend selection remains exclusive and `auto` retains its existing
compatibility behavior. Bitwarden, rbw, KDBX, remembered-password, and
authentication-challenge behavior remains native to the existing backends and
is supervised by the daemon.

**Exit condition met:** `secrets.*` capabilities and lifecycle/transfer methods
are daemon-owned, frontend backend access is removed from the production route,
and the remaining internal compatibility reuse is documented rather than
presented as GTK ownership.

### M6 — Backup / import-export

**Status: Complete and reviewed for secret-bearing transfer.** Secret-bearing
backup preview, enumeration, export, import, and restore execute inside the
daemon. The daemon reuses `BackupManager`, `CredentialManager`,
`backup_archive`, `backup_backends`, the existing `.spbk` format, and merge
behavior. GTK selects files and presents safe metadata, warnings, and
completion state; it does not enumerate decrypted credentials or construct
secret-bearing archives.

The broader shared-operation and identity migration phases remain separate
review items and are not implied by this M6 status.

**Exit condition met:** secret backup writes and credential restoration are
reachable through `secrets.transfer` daemon operations, protected passphrases
use interactions, and no secret value appears in ordinary DTOs.

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

`tests/architecture/test_core_boundary.py` and
`tests/core/test_dependency_boundary.py` enforce the frontend boundary and
record remaining compatibility debt. Shared operation infrastructure is
reviewed and complete; the registries remain intentionally allowed to contain
pending rows for identity services, explicit legacy routes, and other phases
that have not completed review.

The suite fails when a new frontend core import is unregistered, a registered
entry becomes stale, frontend performs an unregistered backend operation, GTK
reaches into `sshpilot.daemon` outside the allowlist, or `core`/`daemon` gain an
unsupported dependency edge. Pending rows are phase-gated work, not evidence
that a completed daemon owner has reverted to GTK.

## The daemon is not yet GI-free

The AST tests prove only that files under `core`/`api`/`daemon` contain **no
direct** Gtk/GLib/GI imports. The daemon *runtime* still depends on GI
transitively because `daemon/cli.py::_production_core_services` composes GObject
adapters:

```python
from sshpilot.config import Config                     # M4 debt
from sshpilot.connection_manager import ConnectionManager  # compatibility adapter
from sshpilot.groups import GroupManager               # M3 debt
from sshpilot.plugins.loader import load_plugins       # M8 debt
```

and `daemon/cli.py` + `daemon/launcher.py` import `platform_utils.get_state_dir`
(M4 debt) for the log path. These are registered in `DAEMON_DEBT`
(`tests/core/test_dependency_boundary.py`) and must be replaced by headless
daemon-owned services (or a GI-free `platform.paths` state-dir helper) before
the claim "the daemon runtime is GI-free" can be made. The tests reject any
*new* daemon → GObject-adapter edge.
