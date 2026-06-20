# Runbook Snippets (sshPilot plugin)

Keep the commands you run on each host as labelled snippets and copy the
ready-to-paste command with one click. Tokens `{host}`, `{user}`, `{port}`, and
`{nickname}` are filled in from the selected connection.

> **Copy only.** sshPilot's plugin API can't type into a terminal or run remote
> commands, so this plugin copies to the clipboard — it never auto-runs anything.
> Paste it into your session.

## Requirements

- sshPilot with plugin **API ≥ 1.4** (provides `ctx.list_connections()` for the
  connection picker).

## Install

Copy this directory to your user plugin dir and enable it in
**Preferences ▸ Plugins** (then restart sshPilot):

- Linux: `~/.local/share/sshpilot/plugins/runbook/`
- Flatpak: `~/.var/app/io.github.mfat.sshpilot/data/sshpilot/plugins/runbook/`

Or install the released `.zip` from **Preferences ▸ Plugins ▸ Install plugin…**.

## Notes

Snippets are keyed by connection nickname and pruned when a connection is
deleted. As with the Notes plugin, a rename can't be followed automatically (the
`connection_updated` event only reports the current nickname); stale entries are
cleaned up the next time the page opens.

## Permissions

`connections`, `ui`, `settings` — declared for transparency; sshPilot plugins run
unsandboxed with full app privileges. Only install plugins you trust.

## Develop / test

```sh
pip install pytest
pip install "sshpilot @ git+https://github.com/mfat/sshpilot" --no-deps
pytest -ra
```

`substitute` and `SnippetStore` are pure Python and unit-tested without GTK; `gi`
is imported lazily inside the page factory.
