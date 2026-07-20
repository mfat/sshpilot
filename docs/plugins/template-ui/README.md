# UI Template Plugin

A minimal **non-protocol** sshPilot plugin: a page that reacts to events and
persists a little state. Use it as the starting point for event-driven / UI
plugins (for protocol backends, use [`../template/`](../template/) instead).

## Use it as a template

1. Copy this directory; rename it and the `id` in `plugin.json`.
2. Replace the page widget and the event handler with your own.
3. Keep pure logic in module-level functions (GTK-free) and import `gi` lazily in
   the page factory, so you can unit-test without a display.

See [../writing-plugins.md ▸ Event-driven & UI plugins](../writing-plugins.md#event-driven--ui-plugins)
for the full guide, and [the plugins repo](https://github.com/mfat/sshpilot-plugins) for richer
examples (auto-group, notes, health).

## Install (to try it)

Copy to `~/.local/share/sshpilot/plugins/ui-template/` (Flatpak:
`~/.var/app/io.github.mfat.sshpilot/data/sshpilot/plugins/ui-template/`), enable
it in **Preferences ▸ Plugins**, and restart.

## Test

```sh
pip install pytest
pip install "sshpilot @ git+https://github.com/mfat/sshpilot" --no-deps
pytest -ra
```
