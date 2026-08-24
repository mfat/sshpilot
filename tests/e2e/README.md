# Dogtail / AT-SPI end-to-end suite

These tests launch the **real** SSH Pilot as a subprocess and drive it entirely
through the accessibility bus. They never import SSH Pilot's Python, never call
a GTK callback directly, and never use screen coordinates.

## Running them

```bash
SSHPILOT_E2E_TESTS=1 pytest tests/e2e -m e2e -p no:cacheprovider -o addopts=""
```

`-o addopts=""` drops the repo default `-n 12 --dist=worksteal`: these tests
each launch a window and must not run in parallel. The `e2e` marker is excluded
from the default `pytest` run, so the ordinary development loop is unaffected.

Artifacts for failing tests land in `build/e2e-artifacts/<test name>/`
(override with `SSHPILOT_E2E_ARTIFACTS`): the accessible tree at the moment of
failure, the app's stdout/stderr, its own log files, the log of harness actions
taken, and a screenshot where one can be captured. The failing test's sandbox
is also kept rather than deleted, and its path printed. Screenshots are
best-effort — modern GNOME refuses the shell's screenshot D-Bus method from
unsanctioned callers, so on a GNOME/Wayland session expect
`screenshot: unavailable` and read the tree dump instead.

## What the session needs

* a graphical session — Wayland or X11 — with an **at-spi2 accessibility bus**
  (`busctl --user list | grep org.a11y.Bus`);
* GTK 4 + libadwaita (whatever it takes to run SSH Pilot at all);
* `python3-dogtail` and `python3-pyatspi`, plus the **GTK 3** typelib that
  dogtail imports (see `requirements-gui-tests.txt`);
* the interpreter running pytest must see those system packages — use the
  system `python3`, or a venv created with `--system-site-packages`.

The GNOME `toolkit-accessibility` GSetting does **not** need to be enabled.
That gate belongs to GTK 3's atk-bridge; GTK 4 always publishes its AT-SPI tree
when the bus exists. `harness.import_dogtail()` switches dogtail's check off
rather than rewriting the developer's desktop settings.

This suite has been exercised on GNOME/Wayland only (Fedora 44, GTK 4.22,
libadwaita 1.9, at-spi 2.58). Nothing in it is Wayland-specific — it uses no
compositor-dependent mechanism — but an X11 or headless (`xvfb-run` plus a
session bus and `at-spi-bus-launcher`) run is **untested**, and the focus
requirement below makes a headless session the likelier of the two to need
work.

## Isolation

`harness.Sandbox` redirects, per test:

| Variable | Why |
| --- | --- |
| `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_STATE_HOME`, `XDG_CACHE_HOME` | app settings, groups, logs |
| `SSHPILOT_SSH_DIR` | the `~/.ssh` SSH Pilot reads and writes |
| `XDG_RUNTIME_DIR` | **the daemon socket** — see below |
| `SSHPILOT_APP_ID` | a unique GApplication id |

`XDG_RUNTIME_DIR` is the one that is easy to miss. The daemon socket lives at
`$XDG_RUNTIME_DIR/sshpilot/sshpilotd.sock`, so redirecting only the XDG *data*
directories leaves the test app talking to the developer's **running daemon**,
where it sees and can mutate their real connections. (That is exactly what
happened the first time this was measured.) The sandbox therefore gets its own
runtime dir, with the Wayland/session-bus/a11y-bus sockets symlinked in so the
app can still open a window, and the app starts its own private daemon inside
it. `AppSession.stop()` kills that daemon, which is spawned with
`start_new_session` and would otherwise outlive the GUI.

`SSHPILOT_APP_ID` matters for a similar reason: without a distinct application
id, GApplication hands the launch off to a copy the developer already has open
and no test process ever appears. `AppSession._discover()` additionally
verifies the AT-SPI application node's PID against the process it spawned, so a
test can never accidentally drive the wrong instance.

## The test session must keep keyboard focus

Leave the session alone while the suite runs (or run it in a dedicated session
or nested compositor). This is not fussiness about timing: **an SSH Pilot window
that is not active stops publishing live content over AT-SPI.** An `Adw.Dialog`
renders on screen but exposes an almost empty accessible subtree — no fields, no
buttons — and a VTE terminal's accessible text comes back empty. Both were
measured, and both affect a screen-reader user exactly as much as they affect a
test.

Nothing outside the app can correct it on Wayland: `Component.GrabFocus` is
unimplemented in GTK 4, and `org.freedesktop.Application.Activate` returns
success without raising the window (focus-stealing prevention).

The harness confines the damage rather than gating the whole suite on it.
Almost everything — discovery, names, roles, states, actions, the sidebar, the
toolbars — is readable whether or not the window has focus, and those tests
always run. Only `AppSession.wait_for_content()` (dialog fields, terminal text)
depends on it, and only that call **skips**, with the reason spelled out. So an
imperfect session loses a handful of assertions instead of the whole run, and
never reports a red that is really a focus problem.

## Wayland

Dogtail's `Node.click()`, `Node.typeText()` and `Node.keyCombo()` synthesise
X11 events through XTEST and do nothing under Wayland. The harness never uses
them. Instead:

| Intent | Mechanism | Works on Wayland |
| --- | --- | --- |
| activate a control | `Action` interface (`AppSession.activate`) | yes |
| run a menu/shortcut command | `win.*` GActions on the frame (`activate_window_action`) | yes |
| type into a field | `EditableText` (`AppSession.set_text`) | yes |
| select a row | `Selection` interface (`AppSession.select`) | yes |
| read text/value | `Text` / `Value` interfaces | yes |
| move keyboard focus | `Component.GrabFocus` | **no** — GTK 4 does not implement it |
| type into the terminal | — | **no** — VTE exposes no `EditableText` |
| drag and drop | — | **no** — AT-SPI has no drag gesture |

See `docs/accessibility-automation.md` for the details and for what would be
needed to cover the last three rows.

## Waiting

There are no `sleep(n)` calls in the tests. `AppSession.wait_until()` polls an
observable accessible condition (a node appearing, a state flipping, a text
value changing) with a timeout, and dogtail's own search back-off covers the
rest.

Two things guard against a stale accessibility cache, which was the one source
of flakiness observed across full runs. libatspi caches an object's children and
invalidates that cache only from events the client has subscribed to; dogtail
does not subscribe, so a container that gained children after the client first
touched it can keep reporting zero of them — indistinguishable from a control
that never appeared. So `AppSession.node()` clears the cached subtree between
retries, and `AppSession.find()` falls back to a direct `Atspi` walk (a live
D-Bus query, no cache involved) when dogtail's cached search comes back empty.
Neither is a longer sleep.
