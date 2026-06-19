# Contributing

We welcome and appreciate contributions.

To contribute, please open an issue first, then make a pull request that links
to it.

## Running from source

```
pip install -r requirements.txt
python3 run.py
```

GTK4, libadwaita, and VTE must be installed from your distribution (see the
README for the full list of system dependencies).

## Running the tests

```
python3 -m pytest tests/
```

## Plugins

sshPilot is extensible via plugins (new protocols and UI pages). Start with the
[plugin developer guide](docs/plugins/writing-plugins.md) and the
[template](docs/plugins/template/).

**Where should a plugin live?**

- **In your own repo (default).** Provider/vendor integrations and anything
  niche, opinionated, or with third-party dependencies belong in an external
  repo, installed into the user plugin dir (or via Preferences ▸ Plugins ▸
  Install plugin…). Open a PR to add it to
  [docs/plugins/community.md](docs/plugins/community.md) so users can find it.
- **In sshPilot core (a built-in PR).** Reserved for plugins that are broadly
  useful, have minimal/no third-party dependencies, come with tests, and that
  the maintainers are willing to support and version with the app (e.g. the
  protocol backends). Open an issue first to discuss; expect a security review,
  since plugins run in-process with full privileges.

Built-ins live under `sshpilot/plugins/builtin/<id>/`; remember to add their
`plugin.json` to `[tool.setuptools.package-data]` in `pyproject.toml` (a test
enforces this).
