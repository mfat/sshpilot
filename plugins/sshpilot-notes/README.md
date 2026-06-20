# Connection Notes (sshPilot plugin)

Keep a freeform note per saved connection — credentials reminders, runbook
steps, maintenance windows. Open the **Notes** page, pick a connection, type.
Notes are saved per host and pruned automatically when a connection is deleted.

## Requirements

- sshPilot with plugin **API ≥ 1.4** (provides `ctx.list_connections()`, used to
  populate the connection picker).

## Install

Copy this directory to your user plugin dir and enable it in
**Preferences ▸ Plugins** (then restart sshPilot):

- Linux: `~/.local/share/sshpilot/plugins/notes/`
- Flatpak: `~/.var/app/io.github.mfat.sshpilot/data/sshpilot/plugins/notes/`

Or install the released `.zip` from **Preferences ▸ Plugins ▸ Install plugin…**.

## Known limitation: renames

Notes are keyed by connection nickname. The `connection_updated` event only
reports the connection's *current* nickname, so when you rename a connection the
plugin can't tell which old note to migrate — the note stays under the old
nickname and is cleaned up the next time the Notes page opens (or when the old
connection is deleted). Re-enter the note after a rename if needed.

## Permissions

`connections`, `ui`, `settings` — declared for transparency; sshPilot plugins
run unsandboxed with full app privileges. Only install plugins you trust.

## Develop / test

```sh
pip install pytest
pip install "sshpilot @ git+https://github.com/mfat/sshpilot" --no-deps
pytest -ra
```

The `NoteStore` logic is pure Python and unit-tested without GTK; `gi` is
imported lazily inside the page factory.
