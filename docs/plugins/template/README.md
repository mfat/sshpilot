# Example sshPilot plugin (template)

A minimal, working sshPilot plugin you can fork to build your own. It registers
an "Example" protocol that runs a configurable command in the terminal — replace
the backend in `__init__.py` with your real protocol or UI page.

See the [plugin developer guide](../writing-plugins.md) for the full API.

## Layout

```
example-plugin/
├── plugin.json                 # manifest (id, name, api_version)
├── __init__.py                 # exposes `class Plugin(SshPilotPlugin)`
├── tests/test_plugin.py        # unit tests (no GTK needed)
└── .github/workflows/test.yml  # CI: pytest against the published sshpilot API
```

## Make it your own

1. Copy this directory out of the sshPilot repo into a new project/repo.
2. In `plugin.json`, change `id` and `name` (the `id` is your directory name and
   keyring/settings namespace).
3. Rewrite `__init__.py` — keep the `class Plugin(SshPilotPlugin)` entry point;
   register a protocol (`ctx.register_protocol(...)`) and/or a UI page
   (`ctx.ui.register_page(...)`).
4. Update `tests/`.

> Tip: on GitHub, mark your repo as a **template repository** so others can
> generate from it.

## Install it (to try it)

Copy the directory into the user plugin dir and enable it:

```sh
cp -r example-plugin ~/.local/share/sshpilot/plugins/example-plugin
# Flatpak: ~/.var/app/io.github.mfat.sshpilot/data/sshpilot/plugins/example-plugin
```

…or use **Preferences ▸ Plugins ▸ Install plugin…** (pick the folder or a zip),
enable it in **Preferences ▸ Plugins**, and restart sshPilot.

## Test it

```sh
pip install pytest
pip install "sshpilot @ git+https://github.com/mfat/sshpilot" --no-deps
pytest -ra
```

## Publish it

Push to your own repo and open a PR adding it to
[`community.md`](../community.md) so users can find it. Only plugins that meet
the bar in [CONTRIBUTING](../../../CONTRIBUTING.md#plugins) belong in sshPilot
core; everything else lives in your repo.
