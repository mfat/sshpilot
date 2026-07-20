#!/usr/bin/env python3
"""On-demand live GUI smoke test for SSH Pilot.

Launches the real app against a throwaway sandbox (its config, data, state and
``~/.ssh`` are redirected into a temp dir, so your real setup is never touched),
then drives it through the accessibility bus and its D-Bus GActions: create a
connection, connect, duplicate, create/edit/move a group, trigger the
edit-while-connected reconnect prompt, and quit. Each step prints PASS/FAIL/SKIP
and the process exits non-zero if anything failed.

Local-only. Lives under ``tests/manual/`` which pytest, ruff, and the type
checker all ignore, so it can never run in — or break — CI.

Usage:
    python3 tests/manual/live_test.py                 # connect to localhost
    python3 tests/manual/live_test.py --host myhost --user me
    python3 tests/manual/live_test.py --no-connect    # skip SSH-dependent steps
    python3 tests/manual/live_test.py --keep          # leave the app open at the end

The connect and reconnect steps need an SSH server reachable with key auth
(no password/passphrase prompt). They auto-SKIP if the target isn't reachable.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve()
REPO_ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE.parent))

try:
    import gi
    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi
    from atspi_driver import Driver, DriverError
except Exception as exc:  # noqa: BLE001
    print(f"SKIP: live test needs PyGObject + Atspi typelib ({exc})")
    sys.exit(0)


class Runner:
    def __init__(self):
        self.results: list[tuple[str, str, str]] = []

    def step(self, name: str, fn):
        try:
            detail = fn()
            self.results.append(("PASS", name, detail or ""))
            print(f"  [PASS] {name}{(' — ' + detail) if detail else ''}")
        except _Skip as s:
            self.results.append(("SKIP", name, str(s)))
            print(f"  [SKIP] {name} — {s}")
        except Exception as e:  # noqa: BLE001
            self.results.append(("FAIL", name, str(e)))
            print(f"  [FAIL] {name} — {e}")

    def summary(self) -> int:
        n_fail = sum(1 for r in self.results if r[0] == "FAIL")
        n_skip = sum(1 for r in self.results if r[0] == "SKIP")
        n_pass = sum(1 for r in self.results if r[0] == "PASS")
        print(f"\n{n_pass} passed, {n_skip} skipped, {n_fail} failed")
        return 1 if n_fail else 0


class _Skip(Exception):
    pass


# -- a11y helpers robust to tree shape ------------------------------------


def _first_label(node) -> str | None:
    try:
        if node.get_role_name() == "label" and node.get_name():
            return node.get_name()
    except Exception:
        return None
    for i in range(node.get_child_count()):
        c = node.get_child_at_index(i)
        if c is not None:
            got = _first_label(c)
            if got:
                return got
    return None


def select_connection_row(d: Driver, label: str) -> bool:
    for _path, node in d.find("list item", ""):
        if _first_label(node) == label:
            parent = node.get_parent()
            sel = parent.get_selection_iface()
            return bool(Atspi.Selection.select_child(sel, node.get_index_in_parent()))
    raise DriverError(f"no sidebar row labelled {label!r}")


def click_button(d: Driver, label: str) -> None:
    d.click(d.find_one("button", label))


def fill_field(d: Driver, name_sub: str, value: str) -> None:
    d.set_text(d.find_one("text", name_sub), value)


def sole_entry(d: Driver):
    """The single showing text entry — used for the create/edit-group dialogs
    whose plain Gtk.Entry has no accessible name (unlike the move dialog's
    named Adw.EntryRow)."""
    texts = d.find("text", "")
    if not texts:
        raise DriverError("no text entry showing")
    return texts[0][1]


def ssh_reachable(host: str, timeout: int = 4) -> bool:
    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={timeout}",
             "-o", "StrictHostKeyChecking=accept-new", host, "true"],
            capture_output=True, timeout=timeout + 3,
        )
        return r.returncode == 0
    except Exception:
        return False


# -- the scenario ----------------------------------------------------------


def run(host: str, user: str, do_connect: bool, keep: bool) -> int:
    sandbox = Path(tempfile.mkdtemp(prefix="sshpilot-livetest-"))
    ssh_dir = sandbox / "sshdir"
    ssh_dir.mkdir(mode=0o700)
    env = dict(os.environ,
               XDG_CONFIG_HOME=str(sandbox / "config"),
               XDG_DATA_HOME=str(sandbox / "data"),
               XDG_STATE_HOME=str(sandbox / "state"),
               SSHPILOT_SSH_DIR=str(ssh_dir))
    log = sandbox / "app.log"
    cfg_json = sandbox / "config" / "sshpilot" / "config.json"
    ssh_config = ssh_dir / "config"
    crash_log = sandbox / "state" / "sshpilot" / "crash.log"

    print(f"sandbox: {sandbox}")
    connect = do_connect and ssh_reachable(host)
    if do_connect and not connect:
        print(f"note: {host} not reachable with key auth — connect/reconnect will SKIP")

    proc = subprocess.Popen(
        [sys.executable, "-m", "sshpilot.main", "--verbose"],
        cwd=str(REPO_ROOT), env=env,
        stdout=log.open("w"), stderr=subprocess.STDOUT,
    )

    d = Driver()
    r = Runner()
    try:
        d.wait_for_app(30)
        d.wait_for_dbus(30)
        time.sleep(1)

        def dismiss_first_run():
            hits = d.find("button", "Confirm")
            if not hits:
                return "no first-run dialog"
            d.click(hits[0][1])
            time.sleep(1)
            return "confirmed default mode"
        r.step("first-run mode dialog", dismiss_first_run)

        def create_conn():
            d.gaction("new-connection")
            time.sleep(1.5)
            fill_field(d, "Nickname", "livetest")
            fill_field(d, "Hostname", host)
            fill_field(d, "Username", user)
            click_button(d, "Save")
            time.sleep(1.5)
            text = ssh_config.read_text() if ssh_config.exists() else ""
            if "Host livetest" not in text:
                raise AssertionError("Host livetest not written to ssh config")
            return "ssh config written"
        r.step("create connection", create_conn)

        win = 1  # first window

        def connect_conn():
            if not connect:
                raise _Skip(f"{host} not reachable")
            select_connection_row(d, "livetest")
            time.sleep(0.5)
            d.gaction("open-new-connection", win)
            time.sleep(4)
            if not d.find("terminal", ""):
                raise AssertionError("no terminal widget after connect")
            if "connected to livetest" not in log.read_text():
                raise AssertionError("no 'connected' log line")
            return "live terminal spawned"
        r.step("connect + terminal", connect_conn)

        def duplicate_conn():
            select_connection_row(d, "livetest")
            time.sleep(0.5)
            d.gaction("duplicate-connection", win)
            time.sleep(1.5)
            if "Host livetest-Copy" not in ssh_config.read_text():
                raise AssertionError("livetest-Copy not in ssh config")
            return "livetest-Copy created"
        r.step("duplicate connection", duplicate_conn)

        def groups():
            groupdata = json.loads(cfg_json.read_text())["connection_groups"]["groups"]
            return {v["name"]: v for v in groupdata.values()}

        def create_group():
            d.gaction("create-group", win)
            time.sleep(1.5)
            d.set_text(sole_entry(d), "")  # ensure empty->error path exists
            # validation: empty name should surface an error alert
            click_button(d, "Create")
            time.sleep(1)
            if not d.find("label", "Please enter a group name"):
                raise AssertionError("empty-name validation did not fire")
            d.click(d.find_one("button", "OK"))
            time.sleep(0.5)
            d.set_text(sole_entry(d), "prod")
            click_button(d, "Create")
            time.sleep(1.5)
            if "prod" not in groups():
                raise AssertionError("group 'prod' not persisted")
            return "validation + create both work"
        r.step("create group (+validation)", create_group)

        def edit_group():
            select_connection_row(d, "prod")
            time.sleep(0.5)
            d.gaction("edit-group", win)
            time.sleep(1.5)
            entry = sole_entry(d)
            if d.text(entry) != "prod":
                raise AssertionError(f"edit dialog not pre-filled (got {d.text(entry)!r})")
            d.set_text(entry, "prod-renamed")
            click_button(d, "Save")
            time.sleep(1.5)
            if "prod-renamed" not in groups():
                raise AssertionError("rename not persisted")
            return "GroupManager.rename_group verified"
        r.step("edit group (rename)", edit_group)

        def move_to_group():
            select_connection_row(d, "livetest")
            time.sleep(0.5)
            d.gaction("move-to-group", win)
            time.sleep(1.5)
            d.set_text(d.find_one("text", "Group name"), "prod-renamed")
            time.sleep(0.5)
            click_button(d, "Move")
            time.sleep(1.5)
            g = groups().get("prod-renamed", {})
            if "livetest" not in g.get("connections", []):
                raise AssertionError("livetest not moved into group")
            return "membership updated"
        r.step("move to group", move_to_group)

        def reconnect_prompt():
            if not connect:
                raise _Skip(f"{host} not reachable")
            select_connection_row(d, "livetest")
            time.sleep(0.5)
            d.gaction("edit-connection", win)
            time.sleep(1.5)
            d.set_text(d.find_one("text", "Port"), "2222")
            click_button(d, "Save")
            time.sleep(2)
            if not d.find("alert", "Settings Changed"):
                raise AssertionError("reconnect alert not shown")
            if not d.find("button", "Reconnect") or not d.find("button", "Cancel"):
                raise AssertionError("Adw.AlertDialog buttons Cancel/Reconnect missing")
            d.click(d.find_one("button", "Reconnect"))
            time.sleep(3)
            return "Adw.AlertDialog shown, reconnect fired"
        r.step("reconnect prompt (Adw.AlertDialog)", reconnect_prompt)

        def quit_clean():
            d.gaction("quit")
            time.sleep(3)
            if crash_log.exists() and crash_log.stat().st_size > 0:
                raise AssertionError(f"non-empty crash log: {crash_log.read_text()[:200]}")
            return "clean shutdown, no crash trace"
        if not keep:
            r.step("quit cleanly", quit_clean)

    finally:
        rc = r.summary()
        if keep:
            print(f"\n--keep: app left running (pid {proc.pid}); sandbox at {sandbox}")
            print("kill it with:  kill", proc.pid)
        else:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            subprocess.run(["rm", "-rf", str(sandbox)])
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description="Live GUI smoke test for SSH Pilot")
    ap.add_argument("--host", default="localhost", help="SSH target (default: localhost)")
    ap.add_argument("--user", default=getpass.getuser(), help="SSH username")
    ap.add_argument("--no-connect", action="store_true",
                    help="skip the SSH connect/reconnect steps")
    ap.add_argument("--keep", action="store_true",
                    help="leave the app running and the sandbox in place at the end")
    args = ap.parse_args()
    return run(args.host, args.user, not args.no_connect, args.keep)


if __name__ == "__main__":
    sys.exit(main())
