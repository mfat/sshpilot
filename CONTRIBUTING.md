# Contributing

We welcome and appreciate contributions.

To contribute, please open an issue first, then make a pull request that links
to it.

## Running from source

sshPilot is developed in a Python **virtual environment (venv) + pip** on top of
the GTK stack. Two setups are supported — a **hybrid** one (system PyGObject +
`--system-site-packages` venv; recommended) and a **pure venv** one (pip-built
PyGObject). Both are covered step by step, with system prerequisites and
troubleshooting, in
[documentation/running-from-source.md](documentation/running-from-source.md).

In short, for the recommended hybrid setup once the system prerequisites are
installed:

```bash
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 run.py
```

Supported/tested Python versions are 3.12 and 3.13 (CI matrix).

## Running the tests

Install the development/test dependencies in the same venv, then run the suite
the way CI does:

```bash
pip install -r requirements-dev.txt
pytest -ra -m "not integration"
```

`integration`-marked tests run real tool binaries and are exercised separately
in CI. Some unit tests are marked `xfail` (see `tests/conftest.py`) — that is
expected.

## Linting

CI runs [Ruff](https://docs.astral.sh/ruff/). Match it locally before pushing:

```bash
ruff check sshpilot/ tests/
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
