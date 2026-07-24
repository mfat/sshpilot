# Known inconsistency: `Connection.host` / `Connection.hostname` divergence

Status: **open, characterized, not yet unified**.
Proof: `tests/test_host_hostname_divergence.py` (5 passing characterization tests).
Line numbers below are as of branch `fix/connection-manager-cleanup` (2026-07); if they
drift, search for the function names — both code blocks are small and distinctive.

## The problem in one table

The same input data produces different attribute values depending on which code
path last touched the object.

Input: `{"nickname": "myserver", "host": "myserver", "hostname": "203.0.113.7"}`

| | fresh `Connection(data)` | after `update_data(data)` |
|---|---|---|
| `conn.host` | `"myserver"` (alias) | `"203.0.113.7"` (**alias lost**) |
| `conn.hostname` | `"203.0.113.7"` | `"203.0.113.7"` |

Input: `{"nickname": "jumpbox", "host": "jumpbox"}` (no `hostname` key)

| | fresh `Connection(data)` | after `update_data(data)` |
|---|---|---|
| `conn.host` | `"jumpbox"` | `"jumpbox"` |
| `conn.hostname` | `""` (correct: no HostName) | `"jumpbox"` (**invented value**) |

"Fresh" happens at app startup (every parsed host is a new object). "Updated"
happens on every dialog edit/save and on every config reload that reuses an
existing object (the loader reuses objects by nickname to preserve identity).
So a connection's attributes silently change meaning after its first edit.

## Where the two rules live

- **Construction rule** — `Connection.__init__`,
  `src/sshpilot/connection_manager.py:295-299`:
  `hostname = data['hostname'] or ''`; `host = data['host']` (falling back to
  nickname). Alias and address stay separate.
- **Update rule** — `Connection._update_properties_from_data`,
  `src/sshpilot/connection_manager.py:762-773`:
  if `hostname` is non-empty, `host` is **overwritten with it** (:764-766);
  if the `hostname` key is absent, `hostname` is **mirrored from host** (:768-769).
- The rest of the field set was consolidated into `_apply_common_fields`
  (`connection_manager.py:777`); these three fields were deliberately left split
  because unifying them is a behavior change (see the docstring there).

## Why the app mostly works anyway

- The ssh target never uses these attributes. `resolve_host_identifier()`
  (`connection_manager.py:326`) prefers `data['__host_tokens']` / `data['host']`,
  which keep the alias on both paths. Verified stable by
  `test_safe_accessor_is_stable_across_both_paths`.
- `get_effective_host()` (`connection_manager.py:317`) reads
  `hostname → host → nickname`; in both divergence cases the chain happens to
  land on the same string either way.
- Keyring keys: `canonical_password_host` (`src/sshpilot/credential_model.py:48`)
  uses the same chain, so the *storage* key is stable. The *legacy-probe list*
  is not (see below).
- Config loads always emit a `hostname` key (possibly `""`), so the
  "invented hostname" case needs a caller-supplied dict that omits the key —
  rarer, but the dialog and plugins construct such dicts.

## Affected readers (from a full-codebase sweep)

High risk — a pre/post-edit value swap plausibly changes behavior:

- **Effective-config block matching** (which `Host` stanza `ssh -G` resolves):
  `src/sshpilot/ssh_connection_builder.py:783-784`,
  `src/sshpilot/scp_utils.py:220-221`,
  `src/sshpilot/plugins/api.py:1199,1217` — all use
  `nickname or host or hostname`; an overwritten `host` makes the lookup key
  the IP instead of the alias when `nickname` is empty.
- **Keyring legacy-probe candidate lists** (a `host`↔`hostname` collapse can
  drop the candidate a legacy password was stored under → lookup miss):
  `src/sshpilot/credential_model.py:77`,
  `src/sshpilot/sftp_utils.py:283-288,471-477`,
  `src/sshpilot/file_manager_window.py:397-402`,
  `src/sshpilot/window_dialogs.py:220-226`,
  `src/sshpilot/ssh_connection_builder.py:37-39,477`.
- **Round-trip back into persistence**: `src/sshpilot/connection_dialog.py:2068-2069`
  seeds the edit form's HostName field from `conn.hostname` — a mirrored
  hostname shows the alias in the HostName box and re-saving persists it;
  `:3194-3195` writes current attrs into the save payload's
  `__previous_secret_identity`.
- **Command targets outside ssh**: `src/sshpilot/window_dialogs.py:1094`
  (backup runner), `src/sshpilot/actions.py:323` (Wake-on-LAN DNS target),
  `plugins/builtin/telnet_protocol/__init__.py:67-68`,
  `plugins/builtin/mosh_protocol/__init__.py:88-89`,
  `plugins/builtin/docker_manager/page.py:494`.
- **Local-terminal detection** gates on `conn.hostname == 'localhost'`:
  `src/sshpilot/terminal.py:1293,1319,1544,3946,4336-4337` — load-bearing on
  the `hostname` attribute specifically.
- **Reload identity matching**: `src/sshpilot/window.py:1062` compares
  `new_conn.hostname == old_conn.hostname` to carry group membership across
  reloads; a mirrored-vs-empty hostname can mis-match.
- **Plugin API surface**: `src/sshpilot/plugins/host.py:74` exposes
  `hostname or host` as `ConnectionInfo.host`, exporting the ambiguity to
  every plugin.

Low risk (cosmetic — labels, subtitles, search, sort, logs): sidebar/picker
rows (`host_picker.py:83,112`, `welcome_page.py:259`,
`connection_dialog_field_helpers.py:129,152`, `window_dialogs.py:807,1159,1554`),
search (`search_utils.py:25-26`), sort (`connection_sort.py:28-29`), clipboard
copy (`window.py:2223`), various log strings.

## Recommended unification (future task)

Adopt the construction rule everywhere: **`host` = alias, always;
`hostname` = the HostName value or `""`**. Concretely:

1. Replace `connection_manager.py:762-773` with the `__init__` logic
   (`:295-299`), then fold all three fields into `_apply_common_fields`.
2. Flip the two `DIVERGENCE` tests in `tests/test_host_hostname_divergence.py`
   and the `host mirrors hostname` assertion in
   `tests/test_connection_manager_edit_duplicate.py::test_apply_update_syncs_attributes_on_success`.
3. Audit the high-risk readers above; most become *more* correct
   (alias-based config matching, stable keyring probes). The one to watch is
   local-terminal detection, which must keep matching whatever the synthetic
   local connection sets (`terminal_manager.py:696-697` writes
   `hostname='localhost'` explicitly, so it is unaffected).
4. Run the full suite; the round-trip tests
   (`tests/test_connection_field_roundtrip.py`) already pin `host`/`hostname`
   for the alias-only and explicit-HostName cases.
