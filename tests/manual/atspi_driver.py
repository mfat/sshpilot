"""AT-SPI + D-Bus driver for live-testing the running SSH Pilot app.

Local-only tooling: lives under ``tests/manual/`` which every CI gate ignores
(pytest ``norecursedirs``, ruff ``extend-exclude``, typecheck scans only
``sshpilot/``). Nothing here is collected as a test.

Two ways to use it:

* As a library — ``from atspi_driver import Driver`` — see ``live_test.py``.
* As a CLI for poking at an already-running app:

    python3 tests/manual/atspi_driver.py tree [max_depth]
    python3 tests/manual/atspi_driver.py find <role-substr> <name-substr>
    python3 tests/manual/atspi_driver.py click <index-path>
    python3 tests/manual/atspi_driver.py settext <index-path> <text>
    python3 tests/manual/atspi_driver.py text <index-path>
    python3 tests/manual/atspi_driver.py gaction <action> [window]

Requires PyGObject with the Atspi typelib and a running desktop session with
the accessibility bus enabled (``gsettings set org.gnome.desktop.interface
toolkit-accessibility true`` — usually already on under GNOME).
"""

from __future__ import annotations

import subprocess
import sys
import time

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi  # noqa: E402

APP_DBUS_NAME = "io.github.mfat.sshpilot"
APP_DBUS_PATH = "/io/github/mfat/sshpilot"
FRAME_TITLE_PREFIX = "SSH Pilot"


class DriverError(RuntimeError):
    pass


class Driver:
    """Thin wrapper over Atspi rooted at the SSH Pilot application node."""

    def __init__(self, frame_prefix: str = FRAME_TITLE_PREFIX):
        self.frame_prefix = frame_prefix

    # -- application / navigation ------------------------------------------

    def app(self):
        desktop = Atspi.get_desktop(0)
        for i in range(desktop.get_child_count()):
            a = desktop.get_child_at_index(i)
            if a is None:
                continue
            for j in range(a.get_child_count()):
                fr = a.get_child_at_index(j)
                if fr and (fr.get_name() or "").startswith(self.frame_prefix):
                    return a
        raise DriverError("SSH Pilot application not found on the a11y bus")

    def wait_for_app(self, timeout: float = 30.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                return self.app()
            except DriverError:
                time.sleep(0.5)
        raise DriverError(f"app did not appear on a11y bus within {timeout}s")

    def node_at(self, path: str):
        node = self.app()
        if path in ("", "."):
            return node
        for part in path.split("."):
            node = node.get_child_at_index(int(part))
            if node is None:
                raise DriverError(f"no child at {part!r} in path {path!r}")
        return node

    # -- queries -----------------------------------------------------------

    def _text_value(self, node) -> str:
        try:
            ti = node.get_text_iface()
            return Atspi.Text.get_text(ti, 0, Atspi.Text.get_character_count(ti))
        except Exception:
            return ""

    def find(self, role_sub: str = "", name_sub: str = "", showing_only: bool = True):
        """Return ``[(path, node), ...]`` matching role and name substrings."""
        results: list[tuple[str, object]] = []

        def visit(n, path):
            try:
                role = n.get_role_name() or ""
                name = n.get_name() or ""
                showing = n.get_state_set().contains(Atspi.StateType.SHOWING)
                if (role_sub in role and name_sub.lower() in name.lower()
                        and (showing or not showing_only)):
                    results.append((path or ".", n))
            except Exception:
                pass
            for i in range(n.get_child_count()):
                c = n.get_child_at_index(i)
                if c is not None:
                    visit(c, f"{path}.{i}".lstrip("."))

        visit(self.app(), "")
        return results

    def find_one(self, role_sub: str = "", name_sub: str = ""):
        hits = self.find(role_sub, name_sub)
        if not hits:
            raise DriverError(f"no node matching role~{role_sub!r} name~{name_sub!r}")
        return hits[0][1]

    def describe(self, n, path: str) -> str:
        role = n.get_role_name()
        name = (n.get_name() or "").replace("\n", "\\n")[:60]
        st = n.get_state_set()
        flags = [lab for s, lab in (
            (Atspi.StateType.SHOWING, "showing"),
            (Atspi.StateType.SENSITIVE, "sens"),
            (Atspi.StateType.FOCUSED, "focused"),
            (Atspi.StateType.SELECTED, "sel"),
        ) if st.contains(s)]
        return f"{path} [{role}] '{name}' ({','.join(flags)})"

    def dump(self, max_depth: int = 99):
        def walk(n, path, depth):
            print(("  " * depth) + self.describe(n, path or "."))
            if depth >= max_depth:
                return
            for i in range(n.get_child_count()):
                c = n.get_child_at_index(i)
                if c is not None:
                    walk(c, f"{path}.{i}".lstrip("."), depth + 1)
        walk(self.app(), "", 0)

    def text(self, node_or_path) -> str:
        n = node_or_path if hasattr(node_or_path, "get_role_name") else self.node_at(node_or_path)
        return self._text_value(n)

    def is_sensitive(self, node_or_path) -> bool:
        n = node_or_path if hasattr(node_or_path, "get_role_name") else self.node_at(node_or_path)
        return n.get_state_set().contains(Atspi.StateType.SENSITIVE)

    # -- actions -----------------------------------------------------------

    def click(self, node_or_path, action_index: int = 0) -> bool:
        n = node_or_path if hasattr(node_or_path, "get_role_name") else self.node_at(node_or_path)
        return bool(n.do_action(action_index))

    def set_text(self, node_or_path, value: str) -> bool:
        n = node_or_path if hasattr(node_or_path, "get_role_name") else self.node_at(node_or_path)
        return bool(n.set_text_contents(value))

    def select_child(self, container_path: str, index: int) -> bool:
        n = self.node_at(container_path)
        sel = n.get_selection_iface()
        return bool(Atspi.Selection.select_child(sel, index))

    def gaction(self, action: str, window: int | None = None) -> None:
        """Invoke an app or window GAction over D-Bus (via gdbus)."""
        path = f"{APP_DBUS_PATH}/window/{window}" if window else APP_DBUS_PATH
        subprocess.run(
            ["gdbus", "call", "--session", "--dest", APP_DBUS_NAME,
             "--object-path", path, "--method", "org.gtk.Actions.Activate",
             action, "[]", "{}"],
            check=True, capture_output=True, text=True,
        )

    def list_gactions(self, window: int | None = None) -> list[str]:
        path = f"{APP_DBUS_PATH}/window/{window}" if window else APP_DBUS_PATH
        out = subprocess.run(
            ["gdbus", "call", "--session", "--dest", APP_DBUS_NAME,
             "--object-path", path, "--method", "org.gtk.Actions.List"],
            check=True, capture_output=True, text=True,
        ).stdout
        return [tok.strip(" '") for tok in out.strip(" ()[],\n").split(",")]

    def wait_for_dbus(self, timeout: float = 30.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                self.list_gactions()
                return
            except subprocess.CalledProcessError:
                time.sleep(0.5)
        raise DriverError(f"app D-Bus name not available within {timeout}s")


def _main(argv: list[str]) -> int:
    d = Driver()
    cmd = argv[0] if argv else "tree"
    if cmd == "tree":
        d.dump(int(argv[1]) if len(argv) > 1 else 99)
    elif cmd == "find":
        for path, node in d.find(argv[1] if len(argv) > 1 else "",
                                 argv[2] if len(argv) > 2 else ""):
            print(d.describe(node, path))
    elif cmd == "click":
        print("click ->", d.click(argv[1]))
    elif cmd == "settext":
        print("settext ->", d.set_text(argv[1], argv[2]))
    elif cmd == "text":
        print(repr(d.text(argv[1])))
    elif cmd == "gaction":
        d.gaction(argv[1], int(argv[2]) if len(argv) > 2 else None)
        print("ok")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
