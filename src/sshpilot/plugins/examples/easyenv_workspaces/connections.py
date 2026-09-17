"""EasyEnv machines as sshPilot SSH connections.

Every machine is reached through EasyEnv's ssh gateway, the same way the
dashboard tells people to reach one from a terminal:

    ssh -J easyenv@ssh.easyenv.io easyenv@AbC123.box.easyenv.io

The gateway is a jump host. It checks the SSH key registered on the person's
EasyEnv profile, and then carries a second, complete ssh session to the
machine's own sshd, which accepts the same key. So sshPilot's terminal, file
manager, SFTP and port forwarding all work, with nothing to install and no
token handed to ssh.

Version 2.0 ran a Python websocket client as the ProxyCommand instead. It
worked in a test profile and not on a real desktop, where the connection
never reached the machine at all; the gateway is what the terminal command
people already use goes through, so the plugin now goes through it too.
"""

from __future__ import annotations

import re
import shlex
from collections import Counter

#: The jump host, as the dashboard prints it.
GATEWAY = "easyenv@ssh.easyenv.io"

#: The name every machine is written under. The gateway reads it from the
#: forwarding request; it is never looked up on this computer.
HOST_SUFFIX = ".box.easyenv.io"

#: Always sshd inside the machine. A box's ``ssh_port`` is a NAT port for the
#: old direct path and means nothing through the gateway.
SSH_PORT = 22

#: How the machine is reached. A ProxyCommand running ``ssh -W`` is what
#: ``ProxyJump`` expands to, spelled out so the options reach the hop as well:
#: with ProxyJump the inner ssh reads only the gateway's own Host block, and a
#: first connection would stop at a host-key question inside sshPilot's
#: terminal. ``%h`` is the machine's name, which ssh fills in.
PROXY_COMMAND = (f"ssh -o StrictHostKeyChecking=accept-new "
                 f"-o ExitOnForwardFailure=yes -W %h:%p {GATEWAY}")

#: accept-new and a throwaway known_hosts because machines are rebuilt under
#: the same name, and a remembered key would turn every rebuild into a "REMOTE
#: HOST IDENTIFICATION HAS CHANGED" wall. LogLevel ERROR keeps the "Permanently
#: added" line that produces out of the terminal.
SSH_OPTIONS = (
    f"ProxyCommand {PROXY_COMMAND}",
    "StrictHostKeyChecking accept-new",
    "UserKnownHostsFile /dev/null",
    "LogLevel ERROR",
)


class SaveError(RuntimeError):
    pass


def host(box_uuid):
    return f"{box_uuid}{HOST_SUFFIX}"


def ssh_command_line(box_uuid, username):
    """What a person can paste into a terminal: the dashboard's own line."""
    return f"ssh -J {GATEWAY} {shlex.quote(username)}@{host(box_uuid)}"


def alias(text):
    """``text`` as something ssh accepts as a destination.

    The nickname is the connection's ``Host`` and what ssh is run with, and
    OpenSSH refuses a destination with a space in it ("hostname contains
    invalid characters"), so a workspace called "My lab" could be saved but
    never opened.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(text)).strip("-.")
    return cleaned or "easyenv"


def nicknames(workspace_title, machines):
    """A readable, stable nickname for each machine, keyed by machine id.

    The workspace's title alone for a workspace with one machine, which is
    what someone looks for in the sidebar. ``title-machine`` otherwise, with
    a number for machines that share a name, counted in id order so the
    numbers do not move between refreshes.
    """
    machines = [m for m in machines if m.get("uuid")]
    base = alias(workspace_title)
    if len(machines) == 1:
        return {machines[0]["uuid"]: base}
    counts = Counter(alias(m["title"]) for m in machines)
    seen = Counter()
    out = {}
    for m in sorted(machines, key=lambda m: m["uuid"]):
        label = alias(m["title"])
        if counts[label] > 1:
            seen[label] += 1
            label = f"{label}-{seen[label]}"
        out[m["uuid"]] = label if label.startswith(base) else f"{base}-{label}"
    return out


def connection_data(nickname, machine):
    """One machine (a ``model.machine_view``) as sshPilot connection data.

    Key authentication, always: the gateway accepts nothing else, and the
    machine accepts the same key. The machine's password is not written. As
    a password connection, sshPilot turned public keys off, which the
    gateway needs, and fed the password to a hop that never asks for one.

    The ProxyCommand travels as an extra config line, not as the connection's
    ``proxy_command`` field, which sshPilot's daemon drops from a plugin.
    """
    box = machine["uuid"]
    return {
        "protocol": "ssh",
        "nickname": nickname,
        "hostname": host(box),
        "host": host(box),
        "username": machine.get("user") or "easyenv",
        "port": SSH_PORT,
        "password": "",
        "auth_method": 0,
        "extra_ssh_config": "\n".join(SSH_OPTIONS),
    }


def upsert(ctx, data):
    """Create the connection, or refresh the one already there.

    Refreshed, so a connection written by an earlier version (with the old
    proxy and a password) is brought up to date the next time it is opened.
    Decided by looking first rather than by catching what ``add_connection``
    raises: the SDK documents ValueError for a duplicate, but sshPilot's
    daemon raises its own SshPilotError.
    """
    nickname = data["nickname"]
    existing = {info.nickname for info in ctx.list_connections()}
    if nickname in existing:
        if not ctx.update_connection(nickname, data):
            raise SaveError(f"could not update the connection {nickname!r}")
        return
    try:
        ctx.add_connection(data)
    except Exception:
        # Made meanwhile by something the snapshot had not seen yet.
        if not ctx.update_connection(nickname, data):
            raise


def save_workspace(ctx, view, only=None):
    """Write a workspace's machines as connections; return {machine uuid: nickname}.

    Several machines go into a sidebar group named after the workspace.
    ``only`` limits the writing to one machine, the one about to be opened;
    the nicknames are still worked out from all of them so they do not change
    with which button was pressed.
    """
    names = nicknames(view["title"], view["machines"])
    group = None
    if len(names) > 1:
        try:
            group = ctx.create_group(f"EasyEnv: {view['title']}")
        except Exception:
            group = None
    for m in view["machines"]:
        nick = names.get(m["uuid"])
        if not nick or (only and m["uuid"] != only):
            continue
        upsert(ctx, connection_data(nick, m))
        if group:
            ctx.add_connection_to_group(nick, group)
    return names
