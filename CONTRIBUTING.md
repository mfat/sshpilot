# Contributing

We welcome and appreciate contributions.

To contribute, please open an issue first, then make a pull request that links
to it.

## Architecture

Before changing how the app connects, authenticates or transfers files, read
[docs/architecture.md](docs/architecture.md). There is exactly **one** connection
path and **one** auth resolver, and PRs that add a second of either will be sent
back.

## Running from source

sshPilot is developed in a Python **virtual environment (venv) + pip** on top of
the GTK stack. Two setups are supported — a **hybrid** one (system PyGObject +
`--system-site-packages` venv; recommended) and a **pure venv** one (pip-built
PyGObject). Both are covered step by step, with system prerequisites and
troubleshooting, in
[docs/running-from-source.md](docs/running-from-source.md).

In short, for the recommended hybrid setup once the system prerequisites are
installed:

```bash
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 run.py
```

Supported/tested Python versions are 3.12 and 3.13 (CI matrix).

## Building and installing with Meson

Meson is the build system for every distro package — the Flatpak, the `.deb`
(`debian/rules` runs `dh --buildsystem=meson`) and the `.rpm`
(`packaging/fedora/rpm.spec` uses the `%meson` macros). The setuptools build is
kept in parallel for the wheel-based paths (PyPI, PyInstaller). Meson is what
compiles the Blueprint `.blp` sources into
the bundled GResource, merges the translations, and installs the app the way a
distro would:

```bash
meson setup builddir --prefix=/usr
meson compile -C builddir
meson test -C builddir      # validates the .desktop and AppStream metainfo
sudo meson install -C builddir
```

Build dependencies beyond the runtime GTK stack: `meson`, `ninja`,
`blueprint-compiler`, the glib tools (`glib-compile-resources`), `gettext`, and —
for the validation tests — `desktop-file-utils` and `appstream`.

Installing this way generates `sshpilot/build_config.py`, which points the app at
the GResource and translations under the install prefix. Running from source has
no such file and falls back to the in-tree paths, so both work side by side.

Use `--prefix` pointed at a scratch directory to inspect an install without
touching the system. `meson dist -C builddir` produces the release tarball.
The `Meson build` CI workflow runs all of the above on every PR.

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

### Running GUI tests (real GTK)


The default `pytest` suite stubs `gi` and never opens a window, so it stays
headless/CI-safe. Real-GTK GUI tests (marker `gui`) boot the actual
`SshPilotApplication` on a display and drive its `Gio` actions/widgets — useful
for action/dialog/state/preference flows. They are **opt-in** and excluded from
the default run (`addopts = -m "not gui"` in `pytest.ini`):

```bash
SSHPILOT_GUI_TESTS=1 pytest -m gui            # on a display
SSHPILOT_GUI_TESTS=1 xvfb-run -a pytest -m gui  # headless machine
```

Without `SSHPILOT_GUI_TESTS=1` + real PyGObject + a display they **skip**
(never error), so they can never turn CI red. Write them with the harness in
`tests/_gui_harness.py`: **name the file `test_gui_*.py`** (in GUI mode the
conftest collects only `test_gui_*` modules — importing the stub-assuming
modules under real GTK can segfault during collection), call `requires_gui()` at
module top, mark the module `pytest.mark.gui`, and use the `gui` fixture
(`open_local_tabs`, `user_pages`, `message_dialogs`, `activate_action`,
`respond`). See `tests/test_gui_tab_close.py` for examples. They are NOT for pixel-gesture,
drag-and-drop, VTE-scraping, or live-SSH bugs — use unit tests there.

## Code style

- Follow [PEP 8](https://peps.python.org/pep-0008/); use type hints where appropriate.
- Prefer GTK4/libadwaita widgets over custom ones, and follow the GNOME HIG.
  Prefer modern Adwaita elements; avoid deprecated GTK3 APIs.
- Widget layout lives in Blueprint (`.blp`) templates compiled into the
  GResource; behaviour lives in Python.
- Add or update tests when you change behaviour. Prefer unit and controller
  tests — add a GUI test only when the bug is genuinely about widget
  interaction, focus, drag-and-drop, rendering or event delivery.

## Linting

CI runs [Ruff](https://docs.astral.sh/ruff/). Match it locally before pushing:

```bash
ruff check src/ tests/
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

Built-ins live under `src/sshpilot/plugins/builtin/<id>/`; remember to add their
`plugin.json` to `[tool.setuptools.package-data]` in `pyproject.toml` (a test
enforces this).

## Packaging

**Meson is the build system for every Linux package.** It compiles the Blueprint
`.blp` sources into the bundled GResource and installs the launcher, the Python
package, the desktop entry, the AppStream metainfo, the icon and
`sshpilot-agent`. Never re-implement any of that by hand in a packaging file —
that is exactly how the packaging silently desynced from the tree when the
sources moved under `src/`.

| Target | Entry point | Builds with |
| --- | --- | --- |
| Flatpak | `flathub/io.github.mfat.sshpilot.yaml` (in-tree copy: `flatpak/`) | `buildsystem: meson` |
| Debian / PPA | `debian/rules` | `dh --buildsystem=meson` |
| Fedora / COPR | `packaging/fedora/rpm.spec` | `%meson` macros |
| Arch | `packaging/ArchLinux/PKGBUILD` | `arch-meson` |
| macOS DMG | `packaging/pyinstaller/` | PyInstaller (not Meson); `packaging/macos/` only holds the `.icns` |

- The setuptools build (`pyproject.toml`) is kept in parallel for the
  wheel-based paths only: PyPI, Homebrew and PyInstaller.
- `meson test` runs the desktop-entry and AppStream validators; the packaging
  wires it into `%check` / `dh_auto_test`.
- macOS DMG naming takes its version from `__init__.py`.
- `scripts/bump-version.sh` is the single writer of the version and changelog
  across `__init__.py`, `meson.build`, the RPM spec, the metainfo and
  `debian/changelog`. Both `scripts/release.sh` and the Release workflow call
  it; never bump a version by hand.

## Releases

- Tags follow `vX.Y.Z` (e.g. `v2.7.1`).
- `scripts/release.sh` (interactive) and the **Release** workflow (manual
  dispatch) both drive `scripts/bump-version.sh`; never bump a version by hand.

## Generated artifacts

The GResource bundle (`src/sshpilot/resources/sshpilot.gresource`) and the `.ui`
files inside it are **committed**, because a source-tree run loads them directly.
Rebuild with `scripts/build_gresource.sh` after changing anything under
`src/sshpilot/resources/` — including the Blueprint `.blp` sources. `lint.yml`
fails the PR if the committed artifacts drift from their sources. The Meson build
compiles its own copy and never reads the committed one.
