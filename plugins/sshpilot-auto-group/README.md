# Smart Auto-Grouping (sshPilot plugin)

Automatically sort new connections into sidebar groups by glob rule.

Define rules like `*.prod.*` → **Production**. Whenever a connection is created
whose **nickname or host** matches a rule, it's added to that group. Use
**Apply to existing connections** to backfill hosts you already had.

## Requirements

- sshPilot with plugin **API ≥ 1.4** (provides `ctx.list_connections()`, used by
  "Apply to existing").

## Install

Copy this directory to your user plugin dir and enable it in
**Preferences ▸ Plugins** (then restart sshPilot):

- Linux: `~/.local/share/sshpilot/plugins/auto-group/`
- Flatpak: `~/.var/app/io.github.mfat.sshpilot/data/sshpilot/plugins/auto-group/`

Or install the released `.zip` from **Preferences ▸ Plugins ▸ Install plugin…**.

## Permissions

`connections`, `ui`, `settings` — declared for transparency; sshPilot plugins
run unsandboxed with full app privileges. Only install plugins you trust.

## Develop / test

```sh
pip install pytest
pip install "sshpilot @ git+https://github.com/mfat/sshpilot" --no-deps
pytest -ra
```

The rule-matching logic (`match_group`) is pure Python and unit-tested without
GTK; `gi` is imported lazily inside the page factory.
