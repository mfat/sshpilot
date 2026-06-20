# Session Log (sshPilot plugin)

Track when you open and close terminal sessions for each saved connection.
View a lightweight history, see per-host totals, and export CSV for billing or
incident timelines.

## Requirements

- Any sshPilot with the plugin system — it uses only the API-1
  event/settings/UI surface (no `list_connections`/1.4 dependency).

## Install

Copy this directory to your user plugin dir and enable it in
**Preferences ▸ Plugins** (then restart sshPilot):

- Linux: `~/.local/share/sshpilot/plugins/session-log/`
- Flatpak: `~/.var/app/io.github.mfat.sshpilot/data/sshpilot/plugins/session-log/`

Or install the released `.zip` from **Preferences ▸ Plugins ▸ Install plugin…**.

## Permissions

`ui`, `settings`, `filesystem` (CSV export writes a file you choose) — declared
for transparency; sshPilot plugins run unsandboxed with full app privileges.
Only install plugins you trust.

## Develop / test

```sh
pip install pytest "sshpilot @ git+https://github.com/mfat/sshpilot" --no-deps
pytest -ra
```

The session store (`SessionLogStore`) is pure Python and unit-tested without
GTK; `gi` is imported lazily inside the page factory.
