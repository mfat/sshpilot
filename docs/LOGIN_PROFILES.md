# Login Profiles — design and implementation plan

> Status: **phase 1 (core) implemented** — `src/sshpilot/core/login_profiles/`,
> daemon composition in `daemon/cli.py`, profile-password fallback in
> `daemon/connection_secret_provider.py`, tests in
> `tests/core/test_login_profiles.py`. Phases 2–5 (API, GTK, backup wiring,
> docs) are pending.
>
> Implementation notes that refine the plan below:
> - Profiles and group links live in a separate daemon-owned
>   `<config-dir>/login_profiles.json` (same hardened atomic-write primitives as
>   `connections.json`), not inside `connections.json`: that file's v1/v2
>   identity-transaction machinery stays untouched. Group links are keyed by
>   scope (`default` / `isolated`) because each SSH root has its own groups.
> - The connection link is the `login_profile` key of per-connection safe
>   metadata, so it follows renames, duplicates, and backups with no new code.
>   It records the fingerprint of the block as written (`applied`), the
>   profile revision it came from (`rev`), and the profile-owned extra
>   directive keywords (`extra_keys`).
> - Host-block rendering reuses `ssh_config_formatter.format_ssh_config_entry`
>   through `ConnectionRepository.update_connection`, no extraction needed.
> - Profile secrets use the plugin-secret convention
>   (`password_spec("sshpilot-login-profile/<id>", "login"|"sudo")`) behind an
>   injected `ProfileSecretStore`, so core never imports `secret_storage`.
> - An unreadable `login_profiles.json` disables profile mutations and
>   reconciliation (links are never dropped because of it).

## Plan

## Context
Today every connection carries its own auth settings (username, IdentityFile/cert, key-selection mode, password, agent/PKCS#11 options) in its `Host` block plus per-host keyring entries. Users with many hosts that share credentials (e.g. `deploy` + `~/.ssh/work_ed25519`) have to set these up one connection at a time and update them one by one. **Login Profiles** are named, reusable auth bundles that can be assigned to any number of connections, or to groups that pass them down. The connection dialog lets you pick a profile or create one.

"Identity" already means key/agent providers in this codebase (`identity.py`, `identity.*` API, `IdentityStateService`), so the feature is called **Login Profile** everywhere, in the UI and in code.

## Decisions (from the interview)
| Topic | Decision |
|---|---|
| Contents | Username; auth method; key(s) + certificate + key-selection mode; login password; key passphrase; optional sudo password; extra SSH options (IdentityAgent, ForwardAgent, PKCS11Provider, SecurityKeyProvider, AddKeysToAgent, PubkeyAuthentication no, free-form extra lines) |
| Precedence | The profile decides. While a profile is linked, the connection's own auth fields are read-only in the dialog. |
| Groups | Inherited default. A connection uses its explicit profile first, then the profile of its nearest ancestor group. Nested groups inherit from their parents. |
| SSH config | Resolved into Host blocks. The daemon writes the effective `User`/`IdentityFile`/… values into each linked Host block. The link itself is sshPilot metadata. Plain `ssh` keeps working. |
| Secrets | Stored in the active secret backend under the profile's ID and shared by every linked connection |
| Drift | Auto-detach. If a linked Host block is edited outside sshPilot and no longer matches its profile, the link is dropped, the file's values win, and the user is notified. |
| Assigning to a configured connection | Show a diff preview, then replace |
| Deleting a profile that's in use | Ask for confirmation, then let the user reassign affected connections/groups to an existing or new profile, or detach them and keep their values |
| Management UI | Dedicated Login Profiles window; inline picker and "new/save as profile" in the connection dialog; group context menu; bulk assign for multiple selected connections |
| Extras (v1) | Profiles (without secrets) are included in backup and import/export |

## Data model (daemon, GTK-free)
New `src/sshpilot/core/login_profiles/`:
- `models.py`: frozen `LoginProfile` dataclass:
  - `id` (opaque, generated with `secrets.token_hex`, the same style as `runtime_identity.py`), `name` (unique, non-empty), `username`
  - `auth_method` (key/password), `key_select_mode`, `identity_files`, `certificate_files`, `identity_agent`, `add_keys_to_agent`, `pkcs11_provider`, `security_key_provider`, `pubkey_auth_no`, `forward_agent`, `extra_ssh_config`
  - `has_password`, `has_passphrase`, `has_sudo_password` (safe flags only), `revision`

  The field names match `ConnectionEditorDetails` (`api/models/connections.py:481`) so the existing Host-block rendering can be reused.
- Persistence: a new `login_profiles` section plus link fields inside the daemon-owned `connections.json` (`core/connections/state_file.py`). This reuses the hardened atomic write and the same transaction as connection/group mutations. It needs a state-version bump and migration: missing section = no profiles.
  - Group link: `GroupFileState.login_profile_id: Optional[str]` (`state_file.py:117`, `GroupRecord` in `core/connections/models.py:196`)
  - Connection link: per-connection metadata `login_profile: {"mode": "explicit", "id": …} | {"mode": "inherit"} | absent (custom)`
  - `last_applied` fingerprint per linked connection: a hash of the resolved directives sshPilot last wrote. Drift detection compares against it.
- Resolution (`core/login_profiles/resolver.py`): `effective_profile(connection) -> (profile, source: "connection"|"group:<id>"|None)`. It walks the connection's primary group (`GroupManager.get_connection_group` semantics) up through `parent_id`. A connection that sits in several groups uses its primary group.

## Daemon service + API
- `core/login_profiles/service.py` `LoginProfileService`, composed in the daemon next to `ConnectionApplicationService`:
  - CRUD with revision checks, `assign(connection_ids, mode/id)`, `set_group_profile(group_id, id|None)`, `preview_assignment(...) -> diff`, `delete(id, reassignments)`
  - After any profile edit, assignment, group-profile change, or connection move between groups, it **re-renders every affected Host block**. This goes through the existing repository update path (`ConnectionRepository.update_connection`, `core/connections/repository.py:2012`) as one transaction, then records the new `last_applied` hashes.
  - **Drift/auto-detach**: during repository load/reload (`_load_state_locked`/`reload`), recompute the hash of each linked block's auth directives. If it differs from `last_applied`, drop the link (inherited links become explicit "custom"), persist, and emit a `login_profiles.detached` event carrying the connection and profile names.
  - Host-block rendering reuses the existing directive-building logic now in `connection_dialog.py:2639-2745` (IdentitiesOnly, PreferredAuthentications, PubkeyAuthentication …). **Move it into a GTK-free core helper** so the dialog and the service share one implementation.
- Secrets (`secret_storage.py`): add `profile_password_spec(profile_id)` and `profile_sudo_spec(profile_id)` with new `type` attributes. Passphrases keep using the existing `passphrase_spec(key_path)` (`secret_storage.py:339`), which is already keyed by key path and therefore shared. Update the type maps in `credential_model.py` so the Credential Manager lists profile secrets.
- Runtime: `DaemonConnectionSecretProvider.lookup_connection_password` (`daemon/connection_secret_provider.py`) and the sudo lookup check the effective profile's spec first, then fall back to the existing per-host spec.
- API: add `api/models/login_profiles.py` (DTOs: `LoginProfileSummary`, `LoginProfileDetails`, requests, `AssignmentPreview`, events), a `login_profiles.read` / `login_profiles.write` capability, client methods in `api/client.py` and `daemon_client.py`, and dispatcher handlers. Secret values cross the API only through the existing secret-frame path used by `StoreConnectionPasswordRequest`. Extend `ConnectionDetails` with `login_profile_id`, `login_profile_name`, `login_profile_source` (explicit/group) and add `login_profile_id` to `GroupReference`/group DTOs.
- Backup/export: extend `snapshot_for_backup` / `restore_connection_store` (`repository.py:2702`) and `core/import_export` with the profiles section and links, without secrets. On merge restore, name clashes get a suffix.

## GTK
- `gtk/login_profile_controller.py`: a snapshot/controller over the client, following the `key_controller.py` / `group_store.py` pattern.
- **Login Profiles window** (`login_profiles_window.py`), opened from the main menu next to "Known Hosts Editor" (`window.py:3843`, action in `actions.py`):
  - List of profiles showing the count of linked connections and groups
  - Editor that reuses the auth widgets (below)
  - A "Used by" list
  - Delete flow
- **Shared auth editor widget**: extract `build_authentication_groups` (`connection_dialog.py:3541`) and the load/save glue into a reusable `auth_editor.py` component. The connection dialog and the profile editor both use it, so the fields and validation stay identical.
- **Connection dialog**:
  - A "Login profile" ComboRow at the top of the Authentication page with these entries: *None (custom)*, *Inherit from group (<name>)* when a group has a profile, and the list of profiles
  - Buttons: "New profile…" and "Save current settings as profile…"
  - When a profile is linked, the auth rows and the Username row on the Connection page are read-only and show the profile's values, with a subtitle naming the profile and a "Edit profile…" link
  - Changing the profile on an existing configured connection opens the diff preview before saving
  - New connections created inside a group that has a profile default to Inherit
- **Group dialog** (`window_dialogs.py` `on_edit_group_action` :3155 / `_group_form_dialog` :2938): add a Login profile ComboRow. There is also a group context-menu item "Set login profile…". Setting a group profile opens a preview that lists the members that will inherit it: members with no explicit profile are checked by default, and members with custom auth are shown with their diff.
- **Bulk assign**: with several connections selected (`sidebar.py` MULTIPLE selection), add a context-menu action "Assign login profile…" that shows the picker and then the diff preview.
- **Delete confirmation**: lists the affected connections and groups. For each item, or for all at once, choose *Reassign to existing…*, *Create new profile…*, or *Detach (keep current values)*.
- **Detach notification**: a toast plus a log entry when the daemon reports auto-detach from drift.

## Implementation phases
1. Core model, state-file section and migration, resolver, secret specs, service CRUD and resolution into Host blocks, drift detection. Unit tests.
2. API DTOs, capabilities, client methods, and dispatcher. API tests; regenerate `docs/api/generated` using the repo's generator.
3. Extract the shared auth editor and Host-block renderer from `connection_dialog.py` with no behavior change. Existing dialog tests must still pass.
4. Login Profiles window, dialog integration, group dialog and menu, bulk assign, delete flow.
5. Backup/import-export and Credential Manager listing. Docs: a new `docs/LOGIN_PROFILES.md` and a row in the `architecture.md` ownership table.

## Verification
- `pytest` for the new tests under `tests/`: resolver inheritance (nested groups, explicit beats group, multiple groups), rendered Host-block output, drift → auto-detach, delete with reassignment, secret lookup fallback order, backup round-trip, and state-file migration of an existing `connections.json`
- Run the existing dialog/repository/API test suites to catch regressions from the extractions in phase 3
- Manual check with the `run` skill against an isolated SSH config dir:
  1. Create a profile, assign it to 2 connections and a group, and confirm the Host blocks in the config file.
  2. Change the profile's username and confirm every linked block updates.
  3. Edit one block by hand and restart, and confirm it's detached and a toast appears.
  4. Connect with a profile password to confirm keyring resolution.
  5. Delete the profile with reassignment.
