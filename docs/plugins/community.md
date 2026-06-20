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
| Connection Notes | mfat | [sshpilot-notes](https://github.com/mfat/sshpilot-notes) | A freeform note per connection, pruned when a connection is deleted. |
| EasyEnv Workspaces | mfat | [sshpilot-easyenv](https://github.com/mfat/sshpilot-easyenv) | Provision easyenv.io dev workspaces and open them as SSH connections. |
| Host Health Dashboard | mfat | [sshpilot-health](https://github.com/mfat/sshpilot-health) | Live up/down TCP status for every saved host. |
| Inventory Import | mfat | [sshpilot-inventory-import](https://github.com/mfat/sshpilot-inventory-import) | Bulk-import hosts from Ansible, CSV, or plain host lists. |
| Smart Auto-Grouping | mfat | [sshpilot-auto-group](https://github.com/mfat/sshpilot-auto-group) | Sort new connections into sidebar groups by glob rule. |
| Session Log | mfat | [sshpilot-session-log](https://github.com/mfat/sshpilot-session-log) | Session open/close history with CSV export. |
| _Your plugin here_ | | | |
