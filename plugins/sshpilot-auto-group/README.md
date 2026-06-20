# Smart Auto-Grouping (sshPilot plugin)

Automatically sort new connections into sidebar groups by glob rule.

Define rules like `*.prod.*` → **Production**. Whenever a connection is created
whose **nickname or host** matches a rule, it's added to that group. Use
**Apply to existing connections** to backfill hosts you already had.

## How matching works

- Each rule has a **pattern**, a **group name**, and an optional **colour**
  (picked with a colour chooser; stored as `#rrggbb`).
- Patterns are shell-style globs (Python `fnmatch`): `*` matches anything, `?`
  one character. Matching is case-insensitive against both the nickname and host.
- Rules are checked **top to bottom; the first match wins** — order matters, so a
  broad `*` catch-all belongs last. Reorder rules with the ▲/▼ buttons.

| Pattern | Matches |
|---------|---------|
| `*.prod.*` | hosts on a prod subdomain (`web.prod.example.com`) |
| `prod-*` | nicknames starting with `prod-` |
| `10.0.*` | hosts in the `10.0.x.x` range |
| `*` | everything (catch-all — keep last) |

Add, edit (click a rule), reorder, and delete rules from the **Auto-Grouping**
page; the same help is available there.

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
