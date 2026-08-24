"""Dogtail/AT-SPI harness for driving the *real* SSH Pilot application.

The tests here launch ``run.py`` as a normal subprocess and talk to it only
through the accessibility bus. Nothing imports SSH Pilot internals, nothing
monkeypatches GTK, nothing calls a callback directly.

Two things make that safe and repeatable:

*Isolation.* :class:`Sandbox` redirects ``XDG_CONFIG_HOME`` / ``XDG_DATA_HOME``
/ ``XDG_STATE_HOME`` / ``XDG_RUNTIME_DIR`` and ``SSHPILOT_SSH_DIR`` into a
throwaway directory. ``XDG_RUNTIME_DIR`` matters as much as the rest: the
daemon socket lives at ``$XDG_RUNTIME_DIR/sshpilot/sshpilotd.sock``, so without
it the test app connects to the developer's *running* daemon and sees (and can
mutate) their real connections. The compositor and bus sockets are symlinked
into the private runtime dir so the app can still open a window.

*Wayland.* Dogtail's ``Node.click()`` and ``Node.typeText()`` synthesise X11
pointer/key events, which do nothing under Wayland. Every helper below drives
the UI through AT-SPI semantics instead — the ``Action`` interface to activate,
``EditableText`` to type, ``Component.grabFocus`` to focus, ``Selection`` to
select. See ``docs/accessibility-automation.md`` for what that cannot reach.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_ENTRYPOINT = REPO_ROOT / "run.py"

#: AT-SPI names the application node after ``g_get_prgname()``; SSH Pilot pins
#: that in ``main()`` so it is the same however the app was started.
APP_A11Y_NAME = "sshpilot"

DEFAULT_TIMEOUT = 20.0


class HarnessError(RuntimeError):
    """Something went wrong driving the app (not an assertion failure)."""


# ---------------------------------------------------------------------------
# dogtail bootstrap
# ---------------------------------------------------------------------------


def import_dogtail(log_dir: Optional[Path] = None):
    """Import dogtail with settings suited to a GTK4 app under Wayland.

    ``dogtail.tree`` calls ``sys.exit(1)`` at import time when the GNOME
    ``toolkit-accessibility`` GSetting is off. That gate predates GTK 4, whose
    AT-SPI backend is always on whenever the accessibility bus exists — and
    SSH Pilot is demonstrably reachable with the setting off. Turning the check
    off is therefore correct here, and avoids GUI tests silently rewriting a
    developer's desktop settings.
    """

    from dogtail.config import config

    config.checkForA11y = False
    config.ensureSensitivity = False
    # Searches back off and retry, which is how we avoid fixed sleeps.
    config.searchBackoffDuration = 0.25
    config.searchCutoffCount = 40
    config.defaultDelay = 0.1
    config.actionDelay = 0.1
    config.childrenLimit = 500
    config.logDebugToStdOut = False
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        config.logDir = str(log_dir)
        config.scratchDir = str(log_dir)
        config.dataDir = str(log_dir)

    from dogtail import tree  # noqa: F401  (import has side effects)

    return tree


def accessibility_bus_available() -> bool:
    """True when this session actually has an AT-SPI bus to talk to."""
    try:
        import gi

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi

        return Atspi.get_desktop(0) is not None
    except Exception:
        return False


# ---------------------------------------------------------------------------
# isolation
# ---------------------------------------------------------------------------

#: The suite matches on accessible names, and those correctly go through
#: gettext -- launched with ``LANGUAGE=de`` the sidebar exposes "Neue
#: Verbindung" and "Verbindungen", not "New Connection" and "Connections"
#: (measured; ``test_accessible_names_are_translated`` keeps it honest).
#:
#: Inheriting the developer's language would therefore decide whether this
#: suite passes. Today it does not, but only by accident: ``tests/conftest.py``
#: sets ``LANGUAGE=en`` at import for the *unit* suite -- a pin about
#: in-process imports that says nothing about subprocesses -- and the sandbox
#: inherits it. Depending on that is exactly the kind of coupling that breaks
#: silently later, so the sandbox states its own contract: the accessible names
#: this suite asserts are the untranslated msgids.
#:
#: ``LANGUAGE`` is what selects the catalogue (see ``sshpilot.i18n``), and ``C``
#: rather than ``en`` because no ``C`` catalogue can exist, so gettext is
#: guaranteed to hand back the msgid itself. ``LANG``/``LC_ALL`` keep
#: formatting deterministic without giving up UTF-8, which the shell spawned by
#: the terminal tests needs.
TEST_LOCALE = {
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "LANGUAGE": "C",
}

#: Names under the real ``XDG_RUNTIME_DIR`` the app still needs: the Wayland
#: compositor socket, the session bus, the accessibility bus, and dconf.
_RUNTIME_PASSTHROUGH_PREFIXES = ("wayland-",)
_RUNTIME_PASSTHROUGH_NAMES = ("bus", "at-spi", "dconf", "pipewire-0", "gvfsd")


@dataclass
class Sandbox:
    """A disposable home for one app run."""

    root: Path
    env: dict = field(default_factory=dict)

    @classmethod
    def create(cls, app_id: str, *, env_overrides: Optional[dict] = None) -> "Sandbox":
        """A sandbox for one app run. ``env`` overrides the pinned defaults."""
        root = Path(tempfile.mkdtemp(prefix="sshpilot-e2e-"))
        ssh_dir = root / "ssh"
        ssh_dir.mkdir(mode=0o700)
        runtime_dir = root / "run"
        runtime_dir.mkdir(mode=0o700)

        real_runtime = Path(
            os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        )
        if real_runtime.is_dir():
            for name in os.listdir(real_runtime):
                keep = name in _RUNTIME_PASSTHROUGH_NAMES or name.startswith(
                    _RUNTIME_PASSTHROUGH_PREFIXES
                )
                if not keep:
                    continue
                try:
                    os.symlink(real_runtime / name, runtime_dir / name)
                except OSError:
                    pass

        env = dict(os.environ)
        env.update(
            XDG_CONFIG_HOME=str(root / "config"),
            XDG_DATA_HOME=str(root / "data"),
            XDG_STATE_HOME=str(root / "state"),
            XDG_CACHE_HOME=str(root / "cache"),
            XDG_RUNTIME_DIR=str(runtime_dir),
            SSHPILOT_SSH_DIR=str(ssh_dir),
            # Without a distinct id GApplication hands the launch off to a copy
            # the developer already has running, and no test process appears.
            SSHPILOT_APP_ID=app_id,
            **TEST_LOCALE,
        )
        # Keep the app off the developer's keyring/agent for good measure.
        env.pop("SSH_AUTH_SOCK", None)
        if env_overrides:
            env.update(env_overrides)
        return cls(root=root, env=env)

    # -- paths the tests assert on ----------------------------------------
    @property
    def ssh_dir(self) -> Path:
        return self.root / "ssh"

    @property
    def ssh_config(self) -> Path:
        return self.ssh_dir / "config"

    @property
    def config_json(self) -> Path:
        return self.root / "config" / "sshpilot" / "config.json"

    @property
    def app_log(self) -> Path:
        return self.root / "app-stdout.log"

    @property
    def daemon_socket(self) -> Path:
        return self.root / "run" / "sshpilot" / "sshpilotd.sock"

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


# ---------------------------------------------------------------------------
# the session
# ---------------------------------------------------------------------------


class AppSession:
    """A running SSH Pilot process plus semantic access to its widget tree."""

    def __init__(self, sandbox: Sandbox, tree_module):
        self.sandbox = sandbox
        self._tree = tree_module
        self.process: Optional[subprocess.Popen] = None
        self.app = None  # dogtail Node for the application
        self._log_handle = None
        self._actions_taken: list[str] = []

    # -- lifecycle ---------------------------------------------------------
    def start(self, *, timeout: float = 60.0) -> "AppSession":
        self._log_handle = self.sandbox.app_log.open("w")
        self.process = subprocess.Popen(
            [sys.executable, str(APP_ENTRYPOINT), "--verbose"],
            cwd=str(REPO_ROOT),
            env=self.sandbox.env,
            stdin=subprocess.DEVNULL,
            stdout=self._log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.app = self._discover(timeout)
        # A freshly launched window normally gets focus. When it does not
        # (something else grabbed it), parts of the UI publish nothing over
        # AT-SPI — see `dialog`. Give it a moment rather than failing later in
        # a way that looks like a missing widget.
        # Most of the UI is readable whether or not the compositor focused the
        # window; only *live content* is not (see `wait_for_content`). So this
        # is a short opportunistic wait, not a gate.
        try:
            self.wait_until(
                lambda: has_state(self.window, "active"),
                timeout=5.0,
                description="the main window to become active",
            )
        except HarnessError:
            pass
        return self

    def _discover(self, timeout: float):
        """Find *our* process on the a11y bus, not another SSH Pilot instance."""
        deadline = time.monotonic() + timeout
        last_error = "no application node appeared"
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise HarnessError(
                    "SSH Pilot exited during startup "
                    f"(rc={self.process.returncode})\n{self.read_log()[-4000:]}"
                )
            for candidate in self._tree.root.applications():
                try:
                    if candidate.name != APP_A11Y_NAME:
                        continue
                    if candidate.get_process_id() != self.process.pid:
                        continue
                except Exception:  # node vanished mid-scan
                    continue
                return candidate
            time.sleep(0.25)
        raise HarnessError(
            f"{last_error} for pid {self.process.pid} within {timeout}s\n"
            f"{self.read_log()[-4000:]}"
        )

    def stop(self, *, timeout: float = 20.0) -> int:
        """Ask the app to quit, then make sure it and its daemon are gone."""
        if self.process is None:
            return 0
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(10)
        self._stop_sandbox_daemon()
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None
        return self.process.returncode

    def _stop_sandbox_daemon(self) -> None:
        """Kill the private daemon the app spawned inside the sandbox.

        It is started with ``start_new_session``, so it does not die with the
        GUI; leaving it running would hold the sandbox open after cleanup.
        """
        socket_path = str(self.sandbox.daemon_socket)
        try:
            out = subprocess.run(
                ["pgrep", "-f", socket_path], capture_output=True, text=True
            ).stdout
        except Exception:
            return
        for line in out.split():
            try:
                os.kill(int(line), 15)
            except (ValueError, ProcessLookupError, PermissionError):
                pass

    # -- observation -------------------------------------------------------
    def read_log(self) -> str:
        try:
            return self.sandbox.app_log.read_text(errors="replace")
        except OSError:
            return ""

    @property
    def window(self):
        """The main application window."""
        return self.app.child(roleName="frame", name="SSH Pilot", recursive=False)

    def frames(self) -> list:
        return [
            child for child in self.app.children if child.roleName in ("frame", "dialog")
        ]

    def find(
        self,
        *,
        role=None,
        name: Optional[str] = None,
        showing_only: bool = True,
        root=None,
    ) -> list:
        """Every node matching an exact role/name, most useful for assertions.

        ``role`` may be a tuple when a control is legitimately exposed under
        more than one role (a libadwaita dialog is a ``dialog`` inside its
        parent window, while a window-level one is a ``frame``).
        """
        from dogtail import predicate

        base = root if root is not None else self.app
        roles = (role,) if role is None or isinstance(role, str) else tuple(role)
        results = []
        for one_role in roles:
            pred = predicate.GenericPredicate(
                roleName=one_role or "", name=name if name is not None else ""
            )
            results.extend(base.findChildren(pred, showingOnly=showing_only))
        if not results:
            # dogtail's search runs through pyatspi's cached tree, which can
            # report a freshly populated container as empty (see `refresh`).
            # Fall back to a direct walk, which is a live D-Bus query.
            results = _walk_for(base, roles, name, showing_only)
        return results

    def dialog(self, name: str, *, timeout: float = DEFAULT_TIMEOUT):
        """A dialog by accessible name, wherever the toolkit chose to put it.

        ``Adw.Dialog`` is presented *inside* its parent window rather than as a
        separate toplevel, so it is a descendant of the frame, not a sibling.

        The node is only returned once its accessible subtree has actually been
        populated. That matters because of a real libadwaita behaviour: an
        in-window dialog whose window is **not active** shows on screen but
        publishes an almost empty accessible subtree, so its fields are
        invisible to assistive technology and to automation alike. If that is
        what we are looking at, say so instead of timing out on a field.
        """
        node = self.node(role=("dialog", "frame"), name=name, timeout=timeout)
        self.wait_for_content(
            lambda: self._subtree_size(node, limit=8) >= 8,
            description=f"dialog {name!r} to publish its contents",
            timeout=timeout,
        )
        return node

    def wait_for_content(
        self,
        condition: Callable[[], object],
        *,
        description: str,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        """``wait_until`` for live *content* (dialog fields, terminal text).

        Content is the part that stops being published when the window loses
        focus, so a timeout here gets the focus explanation attached rather
        than looking like a missing widget.
        """
        try:
            return self.wait_until(
                condition, timeout=timeout, description=description
            )
        except HarnessError:
            if not self.window_is_active():
                # An inactive SSH Pilot window stops publishing live content
                # over AT-SPI — an Adw.Dialog's fields and a VTE terminal's
                # text both come back empty — and nothing outside the app can
                # raise it on Wayland. That is an unusable session for *this*
                # assertion, not a product failure, and it says nothing about
                # the focus-independent tests, which keep running.
                import pytest

                pytest.skip(
                    f"cannot wait for {description}: the SSH Pilot window is "
                    "not active, and an inactive window publishes no live "
                    "content over AT-SPI (see tests/e2e/README.md)"
                )
            raise

    def window_is_active(self) -> bool:
        try:
            return has_state(self.window, "active")
        except Exception:
            return False

    @staticmethod
    def _subtree_size(node, *, limit: int) -> int:
        """Count descendants over live AT-SPI calls, stopping at ``limit``."""
        Atspi = _atspi()
        seen = 0

        def visit(current, depth: int) -> None:
            nonlocal seen
            if seen >= limit or depth > 30:
                return
            try:
                count = Atspi.Accessible.get_child_count(current)
            except Exception:
                return
            for index in range(count):
                if seen >= limit:
                    return
                try:
                    child = Atspi.Accessible.get_child_at_index(current, index)
                except Exception:
                    continue
                if child is None:
                    continue
                seen += 1
                visit(child, depth + 1)

        visit(node, 0)
        return seen

    def node(
        self,
        *,
        role=None,
        name: Optional[str] = None,
        root=None,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        """Wait for and return exactly one node identified semantically.

        Role + accessible name is the locator: no coordinates, no index paths,
        nothing that a layout change invalidates.
        """
        attempts = 0

        def look():
            nonlocal attempts
            attempts += 1
            if attempts > 1:
                # libatspi caches an object's children and only invalidates
                # them from events the client is listening for; dogtail does
                # not subscribe, so a container that gained children after we
                # first touched it can keep reporting zero. Drop the cached
                # subtree before retrying rather than sleeping and hoping.
                refresh(root if root is not None else self.app)
            return (self.find(role=role, name=name, root=root) or [None])[0]

        return self.wait_until(
            look,
            timeout=timeout,
            description=f"node role={role!r} name={name!r}",
        )

    def wait_until(
        self,
        condition: Callable[[], object],
        *,
        timeout: float = DEFAULT_TIMEOUT,
        description: str = "condition",
        interval: float = 0.15,
    ):
        """Poll ``condition`` until it returns something truthy.

        This is how the suite waits: on observable accessible state, never on a
        fixed sleep chosen to be "probably long enough".
        """
        deadline = time.monotonic() + timeout
        last_exc: Optional[Exception] = None
        while time.monotonic() < deadline:
            try:
                value = condition()
                if value:
                    return value
            except Exception as exc:  # a node can vanish while the UI settles
                last_exc = exc
            time.sleep(interval)
        detail = f" (last error: {last_exc!r})" if last_exc else ""
        raise HarnessError(f"timed out after {timeout}s waiting for {description}{detail}")

    # -- interaction (AT-SPI only; safe under Wayland) ---------------------
    def activate(self, node, action: str = "click") -> None:
        """Invoke a node's accessible action instead of clicking coordinates."""
        self._actions_taken.append(f"activate({node.roleName!r}, {node.name!r})")
        actions = node.actions
        if action in actions:
            actions[action].do()
            return
        if actions:
            next(iter(actions.values())).do()
            return
        raise HarnessError(f"{node.roleName} {node.name!r} exposes no accessible action")

    def request_focus(self, node) -> bool:
        """Ask AT-SPI to focus a node. Returns False when the toolkit refuses.

        GTK 4.22's AT-SPI backend does not implement the ``Component.GrabFocus``
        method: the call comes back as ``atspi_error (1)`` for every widget.
        Focus therefore has to be moved through the application's own
        affordances — activate the control that focuses a field — rather than
        demanded from the outside. Nothing else here depends on focus:
        :meth:`set_text` writes through ``EditableText``, which does not need it.
        """
        self._actions_taken.append(f"request_focus({node.roleName!r}, {node.name!r})")
        try:
            return bool(node.grabFocus())
        except Exception:
            return False

    def set_text(self, node, text: str) -> None:
        """Type via the EditableText interface (works without a pointer)."""
        self._actions_taken.append(f"set_text({node.name!r}, {text!r})")
        node.text = text

    def select(self, node) -> None:
        self._actions_taken.append(f"select({node.roleName!r}, {node.name!r})")
        node.select()

    def activate_window_action(self, action_name: str, *, window=None) -> None:
        """Invoke one of the window's exported ``GAction``s.

        GTK publishes every ``win.*`` action on the frame's AT-SPI Action
        interface, which is the accessible way to reach commands that only have
        a menu item or a keyboard shortcut.
        """
        frame = window if window is not None else self.window
        self._actions_taken.append(f"window_action({action_name!r})")
        actions = frame.actions
        if action_name not in actions:
            raise HarnessError(
                f"window exposes no action {action_name!r}; "
                f"available: {sorted(actions)[:20]}…"
            )
        actions[action_name].do()

    # -- diagnostics -------------------------------------------------------
    def describe_tree(self, root=None, max_depth: int = 40) -> str:
        """A readable dump of the accessible subtree, for failure output."""
        lines: list[str] = []

        def visit(node, path: str, depth: int) -> None:
            if depth > max_depth:
                return
            try:
                states = sorted(states_of(node))
                line = (
                    f"{'  ' * depth}[{path or '.'}] {node.roleName!r} "
                    f"name={node.name!r}"
                )
                if node.description:
                    line += f" desc={node.description!r}"
                if node.actions:
                    line += f" actions={sorted(node.actions)}"
                line += f" states={states}"
            except Exception as exc:
                lines.append(f"{'  ' * depth}[{path}] <unreadable: {exc}>")
                return
            lines.append(line)
            # Walk over live AT-SPI calls rather than dogtail's cached view, so
            # a stale cache cannot make the diagnostic itself lie about what
            # the app was showing.
            try:
                Atspi = _atspi()
                count = Atspi.Accessible.get_child_count(node)
            except Exception:
                return
            for index in range(count):
                try:
                    child = Atspi.Accessible.get_child_at_index(node, index)
                except Exception:
                    continue
                if child is not None:
                    visit(child, f"{path}.{index}".lstrip("."), depth + 1)

        visit(root if root is not None else self.app, "", 0)
        return "\n".join(lines)

    def screenshot(self, destination: Path) -> Optional[Path]:
        """Best-effort screenshot; often unavailable, which is fine.

        Wayland has no universal screenshot CLI, and modern GNOME rejects the
        ``org.gnome.Shell.Screenshot`` D-Bus method from unsanctioned callers.
        When none of these work the accessible tree dump is the diagnostic —
        and for this suite it is the more useful one anyway, since every
        assertion is about accessible state rather than pixels.
        """
        candidates = [
            ["grim", str(destination)],                      # wlroots
            ["spectacle", "-b", "-n", "-o", str(destination)],  # KDE
            ["gnome-screenshot", "-f", str(destination)],
            ["import", "-window", "root", str(destination)],  # X11 only
            [
                "gdbus", "call", "--session",
                "--dest", "org.gnome.Shell.Screenshot",
                "--object-path", "/org/gnome/Shell/Screenshot",
                "--method", "org.gnome.Shell.Screenshot.Screenshot",
                "false", "false", str(destination),
            ],
        ]
        for command in candidates:
            if shutil.which(command[0]) is None:
                continue
            try:
                subprocess.run(command, timeout=15, check=True, capture_output=True)
                if destination.exists():
                    return destination
            except Exception:
                continue
        return None

    def failure_report(self, artifacts_dir: Path) -> str:
        """Write tree/log/screenshot next to the test and summarise them."""
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        parts: list[str] = []

        actions_path = artifacts_dir / "actions.log"
        actions_path.write_text("\n".join(self._actions_taken) + "\n")
        parts.append(f"last action: {self._actions_taken[-1] if self._actions_taken else '(none)'}")
        parts.append(f"action log: {actions_path}")

        try:
            tree_path = artifacts_dir / "accessible-tree.txt"
            tree_path.write_text(self.describe_tree())
            parts.append(f"accessible tree: {tree_path}")
        except Exception as exc:
            parts.append(f"accessible tree unavailable: {exc!r}")

        log_path = artifacts_dir / "app-stdout.log"
        try:
            log_path.write_text(self.read_log())
            parts.append(f"app stdout/stderr: {log_path}")
        except Exception as exc:
            parts.append(f"app log unavailable: {exc!r}")

        for name in ("sshpilot.log", "app.log", "ssh.log", "crash.log"):
            candidate = self.sandbox.root / "state" / "sshpilot" / name
            if candidate.exists():
                shutil.copy(candidate, artifacts_dir / name)
                parts.append(f"app log: {artifacts_dir / name}")

        shot = self.screenshot(artifacts_dir / "screenshot.png")
        parts.append(f"screenshot: {shot}" if shot else "screenshot: unavailable")
        return "\n".join(parts)


def _atspi():
    import gi

    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi

    return Atspi


def _walk_for(base, roles, name: Optional[str], showing_only: bool) -> list:
    """Find matching nodes by walking the tree over live AT-SPI calls.

    Deliberately does not go through dogtail/pyatspi's cached view: this is the
    fallback for when that view is stale. Returned nodes are still ordinary
    dogtail ``Node``s, because ``Node`` *is* an ``Atspi.Accessible`` subclass.
    """
    Atspi = _atspi()
    matches: list = []

    def visit(node, depth: int) -> None:
        if depth > 60:
            return
        try:
            count = Atspi.Accessible.get_child_count(node)
        except Exception:
            return
        for index in range(count):
            try:
                child = Atspi.Accessible.get_child_at_index(node, index)
                if child is None:
                    continue
                role_ok = None in roles or Atspi.Accessible.get_role_name(child) in roles
                name_ok = name is None or Atspi.Accessible.get_name(child) == name
                if role_ok and name_ok:
                    if not showing_only or Atspi.Accessible.get_state_set(
                        child
                    ).contains(Atspi.StateType.SHOWING):
                        matches.append(child)
            except Exception:
                continue
            visit(child, depth + 1)

    visit(base, 0)
    return matches


def refresh(node) -> None:
    """Drop libatspi's cached view of ``node`` and its descendants."""
    try:
        _atspi().Accessible.clear_cache(node)
    except Exception:
        pass


def states_of(node) -> set:
    """The node's AT-SPI states as lower-case names ("showing", "expanded").

    ``StateSet.getStates()`` hands back raw enum values, which stringify as
    numbers; ``pyatspi.stateToString`` is what turns them back into names.
    """
    import pyatspi

    return {pyatspi.stateToString(state) for state in node.getState().getStates()}


def has_state(node, state_name: str) -> bool:
    import pyatspi

    constant = getattr(pyatspi, f"STATE_{state_name.upper().replace(' ', '_')}", None)
    if constant is None:
        raise HarnessError(f"unknown AT-SPI state {state_name!r}")
    return node.getState().contains(constant)


def names(nodes: Iterable) -> list:
    return [node.name for node in nodes]
