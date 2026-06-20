# sshPilot plugins (source staging)

Source for the official plugins. Each is a **standalone, installable plugin**
published to the discovery registry
([`mfat/sshpilot-plugins`](https://github.com/mfat/sshpilot-plugins)); they are
**not** bundled into the sshPilot app package. They double as worked examples for
[writing plugins](../docs/plugins/writing-plugins.md).

| Directory | id | What it does |
|-----------|----|--------------|
| `sshpilot-auto-group/` | `auto-group` | Sort new connections into sidebar groups by glob rule. |
| `sshpilot-aws-ssm/` | `aws-ssm` | AWS SSM protocol (`aws ssm start-session`). |
| `sshpilot-hetzner/` | `hetzner` | Browse Hetzner Cloud servers and add them as connections. |
| `sshpilot-inventory-import/` | `inventory-import` | Bulk-import hosts from Ansible, CSV, or plain host lists. |
| `sshpilot-key-audit/` | `key-audit` | SSH key age / certificate-expiry dashboard. |
| `sshpilot-notes/` | `notes` | A freeform note per connection. |
| `sshpilot-health/` | `health` | Live up/down TCP status for every saved host. |
| `sshpilot-runbook/` | `runbook` | Per-connection command snippets, copy-to-clipboard. |
| `sshpilot-session-log/` | `session-log` | Session open/close history with CSV export. |
| `sshpilot-tailscale/` | `tailscale` | Add Tailscale peers as SSH connections. |

Most need sshPilot plugin **API ≥ 1.4** (`ctx.list_connections()`); `aws-ssm` and
`key-audit` work on any API-1 build.

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
into the registry's `plugins.json`. Ready-to-PR entries for every plugin are in
[`registry-entries.json`](registry-entries.json) — copy the objects from its
`plugins` array (adjust versions/URLs if you tag something other than `v1.0.0`).
