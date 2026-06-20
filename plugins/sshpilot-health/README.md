# Host Health Dashboard (sshPilot plugin)

A page that lists every saved connection and shows whether its `host:port`
accepts a TCP connection — refreshed automatically in the background.

A worked example of doing network work off the UI thread and shutting worker
threads down cleanly on quit.

## Requirements

- sshPilot with plugin **API ≥ 1.4** (provides `ctx.list_connections()`). On an
  older app the page shows an "update required" notice instead of failing.

## Install

Copy this directory to your user plugin dir and enable it in
**Preferences ▸ Plugins** (then restart sshPilot):

- Linux: `~/.local/share/sshpilot/plugins/health/`
- Flatpak: `~/.var/app/io.github.mfat.sshpilot/data/sshpilot/plugins/health/`

Or install the released `.zip` from **Preferences ▸ Plugins ▸ Install plugin…**.

## Notes

- A TCP connect only tells you the port is *reachable and accepting* — it does
  not authenticate. Default timeout is 3s; auto-refresh every 30s.
- All probes run on a bounded background thread pool; results are marshalled back
  to the UI with `ctx.run_on_ui_thread`. The pool is stopped on `deactivate()`
  and on the `app_shutdown` event, so nothing outlives the app.

## Permissions

`connections`, `network`, `ui` — declared for transparency; sshPilot plugins run
unsandboxed with full app privileges. Only install plugins you trust.

## Develop / test

```sh
pip install pytest "sshpilot @ git+https://github.com/mfat/sshpilot" --no-deps
pytest -ra
```

`tcp_check` is unit-tested by monkeypatching `socket`; `gi` is imported lazily
inside the page factory.
