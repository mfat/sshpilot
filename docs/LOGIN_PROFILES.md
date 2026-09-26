# Login Profiles

A **login profile** is a named, reusable bundle of authentication settings:
- username
- key-based or password authentication
- key selection, private keys and certificates
- agent and hardware options (`IdentityAgent`, `AddKeysToAgent`, PKCS#11, FIDO provider, agent forwarding)
- extra `ssh_config` directives
- a login password and a sudo password

You can assign a profile to any number of SSH connections, or to a group whose members inherit it. Changing the profile updates every connection that uses it.

In code and UI the feature is called *login profile*. "Identity" already means the key/agent providers (`identity.py`, `identity.*` API).

## Using login profiles

- **Manage profiles:** main menu → **Login Profiles**. Here you can create, edit or delete profiles. Each row shows the profile's user and keys, and how many connections and groups use it.
- **One connection:** in the connection editor, open **Authentication** and use **Login profile**:
  - Pick *Custom (no profile)*, *Inherit from group (…)*, or a profile.
  - While a profile is linked, the authentication rows, **Username** and agent forwarding are locked and show the profile's values.
  - For an existing connection, the editor lists the settings the profile will replace before you save.
  - **New…** creates a profile. **Save as Profile…** creates one from the connection's current settings. **Edit Profile…** edits the linked profile.
- **Several connections:** select them in the sidebar → right-click → **Assign Login Profile…**. A confirmation lists what changes before anything is written.
- **Groups:** right-click a group → **Set Login Profile…**.
  - Connections in the group, and in nested groups without their own profile, inherit the profile.
  - Members that already have an explicit profile are left unchecked by default.
- **Deleting a profile that is in use:** you choose, for each affected connection and group (or all at once), either another profile or **Detach**. Detached connections keep their current settings.
- **Manual edits:** if you edit a linked connection's `Host` block outside sshPilot so it no longer matches its profile, sshPilot detaches the connection and keeps your edit. A toast tells you.

## Design decisions

| Topic | Decision |
|---|---|
| Precedence | The profile decides the auth settings; the connection's own auth fields are read-only while it is linked |
| Groups | A connection's explicit profile wins; otherwise it inherits the nearest ancestor of its primary group (the first group listing it) |
| SSH config | The effective profile is written into each linked `Host` block, so plain `ssh` keeps working; the link itself is sshPilot metadata |
| Secrets | Stored once per profile in the active secret backend and shared by every linked connection |
| External edits (drift) | The connection is auto-detached and the file wins |
| Assigning to a configured connection | Preview the diff, then replace |
| Deleting a profile in use | Confirm, then reassign each item (existing or new profile) or detach it |
| Backups | Profiles and group links are included; profile secrets travel with the other backup credentials |

## Architecture

Every operation goes through the daemon; GTK never reads or writes SSH config, state files or secrets.

```text
GTK (LoginProfileController, dialogs)
        ↓  login_profiles.* typed API (capabilities login_profiles.read/write)
DaemonLoginProfileApi (daemon/login_profile_api.py)
        ↓
LoginProfileService (core/login_profiles/service.py)
        ↓                                   ↓
ConnectionRepository.update_connection   login_profiles.json + secret backend
```

### Storage

- **Profiles and group links:** `<config-dir>/login_profiles.json`, written with the same hardened, atomic-write primitives as `connections.json` (`core/login_profiles/store.py`).
  - Group links are keyed by scope (`default` / `isolated`), because each SSH root has its own groups.
  - The file holds no secrets, only the `has_password` / `has_sudo_password` display flags.
  - If the file cannot be read, profile mutations and reconciliation are disabled rather than replacing it; links are never dropped because of it.
- **Connection link:** stored under the `login_profile` key of the connection's safe metadata, so it follows renames, duplicates and backups. It records:
  - `mode`: `explicit` (with `id`) or `inherit`.
  - `applied`: a fingerprint of the profile-owned directives as the loader read them back after sshPilot wrote the block.
  - `rev`: the profile revision it was rendered from.
  - `extra_keys`: the profile-owned extra directive keywords.

### Resolution and reconciliation

- **Resolution** (`core/login_profiles/resolver.py`): an explicit link wins. An `inherit` link walks the connection's primary group, then its parents.
- **Applying a profile** (`models.apply_profile_to_data`): overlays the profile onto the connection's current data and saves through `ConnectionRepository.update_connection`, which reuses `ssh_config_formatter`.
  - The profile's extra directives replace same-keyword lines of the connection's extras.
  - Directives that have a dedicated field are rejected in extras.
- **Reconciliation** runs at daemon start, after every repository change not made by the service itself, and after an operation-mode switch. For each link:
  1. **Detach on drift:** a recorded fingerprint that no longer matches the block means it was edited outside the profile. The link is detached (`drift`) and a `login_profiles.changed` event carries the detached connection.
  2. **Detach a dead explicit link:** an explicit link to a missing profile is detached (`profile_missing`). An `inherit` link without a group profile stays dormant.
  3. **Re-apply:** a link whose effective profile revision changed (profile edited, connection moved between groups) is re-rendered.

### Secrets, sudo, backups

- **Secret storage:** profile secrets use the plugin-secret convention, `password_spec("sshpilot-login-profile/<id>", "login" | "sudo")`, behind an injected `ProfileSecretStore` (`DaemonLoginProfileSecretStore`). Core never imports `secret_storage`.
- **Transport:** `login_profiles.set_secret` carries the secret in the protected secret frame, and the handler wipes the buffer after use. `login_profiles.clear_secret` takes no secret input.
- **Login password:** `DaemonConnectionSecretProvider` tries the effective profile's password before the per-host entry.
- **Sudo password:** `PrivilegedFileService` tries the profile's sudo password before the per-host secret. A wrong profile password is never deleted.
- **Backups:** profiles are embedded as a `login_profiles` key of the `connection_store` backup section (`daemon/login_profile_backup.py`).
  - They are restored *before* the connection store, so restored links find their profiles.
  - A bad section is a warning, not a failed import.
  - Profile secrets are exported with the other credentials under the *secrets* option and restored by the generic credential path.
  - Runtime lookups do not depend on the `has_*` flags, because secrets are restored after profiles.

### API

- **Methods:** `get_login_profiles`, `create_login_profile`, `update_login_profile` (with `expected_revision`), `delete_login_profile` (with per-item reassignment), `preview_login_profile_assignment`, `assign_login_profile`, `set_group_login_profile` and `set_login_profile_secret`.
- **Event:** `login_profiles.changed`.
- **Models:** in `sshpilot.api.models.login_profiles`; codecs in `api/transport/login_profile_codec.py`. Introduced in API 0.70; see [api/methods.md](api/methods.md) and [api/CHANGELOG.md](api/CHANGELOG.md).
- **Command keys:** writes run on the configuration command key; secret writes run on the interactive secret key, so a backend unlock prompt cannot block configuration RPCs.

### GTK

- **Controller:** `gtk/login_profile_controller.py` is GTK-free. It wraps the client, caches the snapshot, and holds the pure helpers for picker choices, delete plans and preview text.
- **Dialogs:** `login_profile_dialogs.py` contains the window, editor, delete, assign and group dialogs.
- **Connection dialog:** `connection_dialog_login_profile.py` is the picker mixin. The chosen link is applied by `MainWindow._save_connection_via_client`:
  - unlinking runs before the config write, so edited auth fields are not mistaken for drift;
  - linking runs after the config write commits.

## Deviations from the original plan

- **Separate state file:** profiles live in their own state file rather than a new `connections.json` section. This leaves the v1/v2 identity-transaction machinery untouched.
- **Link state:** it is exposed through `LoginProfileSnapshot` instead of extending `ConnectionDetails`.
- **Profile editor:** it is a dedicated form rather than an extraction of the connection dialog's authentication rows, to limit regression risk in the 5,600-line dialog.
- **New connections start unlinked:** the connection dialog has no group context for new connections. Existing connections are offered their primary group's profile.
- **Backup format:** profiles are a key of the existing `connection_store` section rather than a new top-level archive section.

## Tests

| File | Covers |
|---|---|
| `tests/core/test_login_profiles.py` | Model, resolution, Host-block rendering, inheritance, drift, delete with reassignment, secrets, backup |
| `tests/api/test_login_profiles_codec.py` | Models and wire codecs |
| `tests/daemon/test_login_profiles_transport.py` | End to end over a real in-process daemon, including secret frames and the drift event |
| `tests/daemon/test_login_profile_backup.py` | Backup section and credentials |
| `tests/daemon/test_privileged_file_service.py` | Profile sudo password |
| `tests/test_login_profile_controller.py` | GTK-free controller and helpers |
| `tests/test_gtk_connection_mutations.py` | Save hook ordering |
