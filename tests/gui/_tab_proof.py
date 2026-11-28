"""Prove a terminal tab in the real window runs a live shell.

Shared by the protocol, SSH and local-terminal GUI tests. The marker only
appears once a shell has evaluated ``$((6*7))``, so typed text echoed back by a
dead target cannot pass, and the screen is read through the tab's own backend,
so the proof holds for every terminal backend, not only VTE.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

COMMAND = "printf 'SHELL_%s_OK\\n' $((6*7))\n"
MARKER = "SHELL_42_OK"
UNKNOWN_TERM = "sshpilot-unknown-term"

#: Control sequences (CSI, OSC, two-byte escapes): xterm.js reports the raw
#: stream it was fed, where a prompt can end in a cursor query.
_CONTROL = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-_]")


def terminal_text(h, term) -> str:
    return h.read_visible_terminal_text(term) or ""


def child_processes() -> str:
    """This test's descendant processes, to show where a launch hangs."""
    parents: dict[int, int] = {}
    commands: dict[int, str] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
            cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue
        parents[int(entry.name)] = int(stat.rsplit(")", 1)[1].split()[1])
        commands[int(entry.name)] = cmdline.strip()
    found, todo = [], [os.getpid()]
    while todo:
        pid = todo.pop()
        children = [child for child, parent in parents.items() if parent == pid]
        found += children
        todo += children
    return "; ".join(f"{pid}: {commands[pid][:200]}" for pid in sorted(found)) or "none"


def evidence(h, term) -> str:
    backend = getattr(term, "get_backend_name", lambda: "?")()
    return (
        f"backend {backend}; terminal shows {terminal_text(h, term)[-400:]!r}; "
        f"processes: {child_processes()}"
    )


def wait_for(h, term, marker: str, *, timeout: float = 30.0) -> str:
    from tests.gui._phase14_harness import Phase14EvidenceError

    try:
        return h.wait_for_marker(marker, terminal=term, timeout=timeout)
    except Phase14EvidenceError as error:
        raise AssertionError(f"{error}; {evidence(h, term)}") from error


def type_into(h, term, text: str) -> None:
    """Type ``text`` as the keyboard would: through the tab, to its session."""
    if getattr(term, "_daemon_mode", False):
        h.emit_terminal_input(text, term)
    else:
        term.feed_child_data(text.encode("utf-8"))


def wait_for_prompt(h, term, *, timeout: float) -> None:
    """Type only once the shell shows a prompt, as a person would.

    Clients such as ``docker exec -it`` switch their terminal to raw mode
    with a flush once attached, which discards anything typed before.
    """

    def _prompt():
        text = _CONTROL.sub("", terminal_text(h, term))
        lines = [line.rstrip() for line in text.splitlines() if line.strip()]
        return bool(lines) and lines[-1][-1:] in ("$", "#", ">", "%")

    try:
        try:
            h.pump_until(_prompt, timeout=10.0, label="shell prompt")
        except AssertionError:
            # A serial console prints nothing until it is poked: its first
            # prompt went out before the client opened the line.
            type_into(h, term, "\n")
            h.pump_until(_prompt, timeout=timeout, label="shell prompt after Enter")
    except AssertionError as error:
        raise AssertionError(f"{error}; {evidence(h, term)}") from error


def assert_veil_lifted(h, term) -> None:
    """Nothing may still cover a terminal whose session answers (#1432)."""
    h.pump_until(
        lambda: getattr(term, "_overlay_mode", None) == "none",
        timeout=10.0,
        label=f"Connecting veil lifted (mode={getattr(term, '_overlay_mode', None)!r})",
    )
    assert not term.connecting_bg.get_visible()
    assert not term.connecting_box.get_visible()


def open_tab(h, connection):
    """Open ``connection`` from the window, as a double-click does."""
    h.gui.window.terminal_manager.connect_to_host(connection, force_new=True)
    h.pump_until(
        lambda: h.find_terminal_widget(connection) is not None,
        timeout=20.0,
        label="terminal widget created",
    )
    return h.find_terminal_widget(connection)


def prove_session_shell(h, term, *, label: str, timeout: float = 60.0) -> str:
    """Wait for the tab's daemon session, then run the marker command in it."""
    h.pump_until(
        lambda: bool(getattr(term, "is_connected", False)),
        timeout=timeout,
        label=f"{label} tab connected",
    )
    try:
        h.ensure_input_ready(term, timeout=20.0)
    except AssertionError as error:
        raise AssertionError(f"{error}; {evidence(h, term)}") from error
    wait_for_prompt(h, term, timeout=timeout)
    type_into(h, term, COMMAND)
    text = wait_for(h, term, MARKER, timeout=timeout)
    assert MARKER in text
    assert_veil_lifted(h, term)
    return text


def open_and_prove_shell(h, connection, *, monkeypatch, timeout: float = 60.0):
    """Open the tab from the window and prove a live shell sits behind it.

    The daemon spawns clients with its own environment, so the test starts
    with a ``TERM`` no terminfo database knows: Mosh refuses to start with
    one (#1432), and the far side must see the terminal sshPilot provides.
    """
    monkeypatch.setenv("TERM", UNKNOWN_TERM)
    term = open_tab(h, connection)
    prove_session_shell(
        h, term, label=str(getattr(connection, "protocol", "ssh")), timeout=timeout
    )
    return term
