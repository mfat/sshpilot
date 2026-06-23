# Community plugins

Third-party sshPilot plugins, maintained outside the core repo. Install them into
the user plugin dir (`~/.local/share/sshpilot/plugins/<id>/`, Flatpak:
`~/.var/app/io.github.mfat.sshpilot/data/sshpilot/plugins/<id>/`) or via
**Preferences ▸ Plugins ▸ Install plugin…**, then enable and restart.

> These are not maintained or vetted by the sshPilot maintainers. Plugins run
> in-process with full privileges — **install only what you trust.**

To list yours, scaffold from the
[**sshpilot-plugin-template**](https://github.com/mfat/sshpilot-plugin-template)
("Use this template"), publish a release, then add an entry to the discovery
index at [**mfat/sshpilot-plugins**](https://github.com/mfat/sshpilot-plugins)
(see the [registry format](registry.md)) — and optionally add a row below
(keep it alphabetical).

| Plugin | Author | Repository | Description |
|--------|--------|------------|-------------|
| AWS SSM | mfat | [sshpilot-aws-ssm](https://github.com/mfat/sshpilot-aws-ssm) | Open AWS Session Manager shells as a protocol (no inbound SSH needed). |
| Connection Notes | mfat | [sshpilot-notes](https://github.com/mfat/sshpilot-notes) | A freeform note per connection, pruned when a connection is deleted. |
| EasyEnv Workspaces | mfat | [sshpilot-easyenv](https://github.com/mfat/sshpilot-easyenv) | Provision easyenv.io dev workspaces and open them as SSH connections. |
| Hetzner Cloud | mfat | [sshpilot-hetzner](https://github.com/mfat/sshpilot-hetzner) | Browse Hetzner Cloud servers and add them as SSH connections. |
| Host Health Dashboard | mfat | [sshpilot-health](https://github.com/mfat/sshpilot-health) | Live up/down TCP status for every saved host. |
| Inventory Import | mfat | [sshpilot-inventory-import](https://github.com/mfat/sshpilot-inventory-import) | Bulk-import hosts from Ansible, CSV, or plain host lists. |
| Runbook Snippets | mfat | [sshpilot-runbook](https://github.com/mfat/sshpilot-runbook) | Per-connection command snippets, copy-to-clipboard. |
| Session Log | mfat | [sshpilot-session-log](https://github.com/mfat/sshpilot-session-log) | Session open/close history with CSV export. |
| Smart Auto-Grouping | mfat | [sshpilot-auto-group](https://github.com/mfat/sshpilot-auto-group) | Sort new connections into sidebar groups by glob rule. |
| SSH Key Audit | mfat | [sshpilot-key-audit](https://github.com/mfat/sshpilot-key-audit) | Key age and certificate-expiry dashboard for ~/.ssh. |
| Tailscale Sync | mfat | [sshpilot-tailscale](https://github.com/mfat/sshpilot-tailscale) | Add Tailscale tailnet peers as SSH connections. |
| _Your plugin here_ | | | |
