# Inventory Import (sshPilot plugin)

Bulk-import SSH connections from plain host lists, CSV spreadsheets, or Ansible
inventory files. Preview which hosts are new vs already saved, then import
selected rows — optionally into a sidebar group.

## Requirements

- sshPilot with plugin **API ≥ 1.4** (provides `ctx.list_connections()` for
  diff preview).

## Install

Copy this directory to your user plugin dir and enable it in
**Preferences ▸ Plugins** (then restart sshPilot):

- Linux: `~/.local/share/sshpilot/plugins/inventory-import/`
- Flatpak: `~/.var/app/io.github.mfat.sshpilot/data/sshpilot/plugins/inventory-import/`

Or install the released `.zip` from **Preferences ▸ Plugins ▸ Install plugin…**.

## Supported formats

| Format | Example |
|--------|---------|
| Plain hosts | `web.example.com`, `10.0.0.1:2222`, `10.0.0.1 db.internal` |
| CSV | Header row with `nickname`, `host`, `user`, `port` (flexible column names) |
| Ansible INI | Standard `[group]` / `host ansible_host=…` inventory |
| Ansible YAML | Simple 2-space-indent `group: hosts: name: {ansible_host: …}` subset |

## Permissions

`connections`, `ui`, `settings`, `filesystem` — declared for transparency;
sshPilot plugins run unsandboxed with full app privileges. Only install
plugins you trust.

## Develop / test

```sh
pip install pytest
pip install "sshpilot @ git+https://github.com/mfat/sshpilot" --no-deps
pytest -ra
```

Parsing and diff logic is pure Python and unit-tested without GTK; `gi` is
imported lazily inside the page factory.
