# sshPilot plugins (source staging)

Source for the official non-protocol plugins. Each is a **standalone, installable
plugin** published to the discovery registry
([`mfat/sshpilot-plugins`](https://github.com/mfat/sshpilot-plugins)); they are
**not** bundled into the sshPilot app package. They double as worked examples for
[writing non-protocol plugins](../docs/plugins/writing-plugins.md).

| Directory | id | What it does |
|-----------|----|--------------|
| `sshpilot-auto-group/` | `auto-group` | Sort new connections into sidebar groups by glob rule. |
| `sshpilot-inventory-import/` | `inventory-import` | Bulk-import hosts from Ansible, CSV, or plain host lists. |
| `sshpilot-notes/` | `notes` | A freeform note per connection. |
| `sshpilot-health/` | `health` | Live up/down TCP status for every saved host. |
| `sshpilot-session-log/` | `session-log` | Session open/close history with CSV export. |

All three need sshPilot plugin **API ≥ 1.4** (`ctx.list_connections()`).

## Test

```sh
PYTHONPATH=$(git rev-parse --show-toplevel) python3 -m pytest plugins/*/tests -q
```

(In each plugin's own CI, `sshpilot` is installed from git with `--no-deps`.)

## Package for release

```sh
plugins/build.sh        # writes plugins/dist/<id>.zip + .zip.sha256
```

Then, per [docs/plugins/registry.md](../docs/plugins/registry.md): create a
GitHub release per plugin (attach the `.zip` and `.zip.sha256`), and PR an entry
into the registry's `plugins.json`. Ready-to-PR entries for all three are in
[`registry-entries.json`](registry-entries.json) — copy the objects from its
`plugins` array (adjust versions/URLs if you tag something other than `v1.0.0`).
