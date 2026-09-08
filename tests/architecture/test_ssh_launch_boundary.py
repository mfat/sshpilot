"""One choke point for daemon OpenSSH children, enforced rather than agreed.

:mod:`sshpilot.daemon.ssh_launch` is the only sanctioned way to construct or
run a daemon-owned OpenSSH child. Every previous attempt at this was a
convention, and conventions drifted: six call sites each re-decided the
interaction policy, the askpass ``REQUIRE`` level, the scope id, the ``Popen``
flags, the process-registry record and the
``mark_authenticated()``-before-``cancel_session()`` ordering. Three re-derived
the ordering in near-identical comments, one omitted it, and two never
registered their children at all.

Two rules, because the codebase has two kinds of caller:

*First-order* callers construct launches. They must go through
:class:`~sshpilot.daemon.ssh_launch.SshLauncher`. The ones that still do not
are listed in :data:`APPROVED_DIRECT_LAUNCHERS`, which may only ever shrink.

*Second-order* callers do not launch anything -- they delegate to a
first-order service and add only "which command" and "how to read the answer".
Host Info and both backup transports are already written this way, and their
own docstrings say so. They get the stricter rule: they may not touch the
launch stack *and* may not reach for ``SshLauncher`` either. Without that
second rule the new choke point becomes a tempting shortcut that flattens
layering which is currently correct -- Host Info calling ``SshLauncher``
directly would pass a one-list test while losing the cancellation, output
limits and operation lifecycle it inherits today.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path("src/sshpilot")

#: Packages that participate in the SSH launch stack.
SCOPE = ("daemon/", "plugins/builtin/")

#: The choke point itself.
OWNER = "daemon/ssh_launch.py"

#: Files that *define* the layers below the choke point. They are the stack,
#: not callers of it, so the caller rules do not apply to them.
LAYERS = frozenset(
    {
        OWNER,
        "daemon/interaction_broker.py",
        "daemon/connection_launch_provider.py",
        "daemon/process_registry.py",
    }
)

#: Starting a child process.
SPAWN_NAMES = frozenset({"Popen", "_popen"})

#: Reaching into the launch stack or the credential lifecycle.
STACK_NAMES = frozenset(
    {
        "resolve_native_auth",
        "build_ssh_connection",
        "_build_base_ssh_command",
        "prepare_operation_launch",
        "mark_authenticated",
        "cancel_session",
    }
)

#: Delegators. They must appear in neither scan, and must not import the
#: launcher: their correctness comes from the service they delegate to.
SECOND_ORDER = frozenset(
    {
        "daemon/host_info_service.py",
        "daemon/backup_transport.py",
    }
)

#: Frontend-side delegator, outside :data:`SCOPE` but held to the same rule.
SECOND_ORDER_FRONTEND = frozenset({"backup_backends.py"})


def _receiver(node: ast.Call) -> str:
    try:
        return ast.unparse(node.func.value)  # type: ignore[attr-defined]
    except Exception:
        return ""


def scan_file(rel: str) -> frozenset:
    """Names in *rel* that reach the launch stack or spawn a child."""

    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"), rel)
    hits: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", getattr(node.func, "id", ""))
            if name in SPAWN_NAMES or name in STACK_NAMES:
                hits.add("Popen" if name in SPAWN_NAMES else name)
            elif name == "prepare_launch":
                # ``SshReadinessManager.prepare_launch`` is an unrelated method
                # that happens to share the broker's name; only the broker's
                # is launch-stack access.
                if "readiness" not in _receiver(node).lower():
                    hits.add("prepare_launch")
        elif isinstance(node, ast.Attribute) and node.attr == "Popen":
            # ``popen=subprocess.Popen`` as an injected default still means
            # this file decides how a child is started.
            hits.add("Popen")
    return frozenset(hits)


def current_direct_launchers() -> dict:
    """Every in-scope file that still reaches the stack itself."""

    found = {}
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if not rel.startswith(SCOPE) or rel in LAYERS:
            continue
        hits = scan_file(rel)
        if hits:
            found[rel] = hits
    return found


#: The state of the world when the choke point landed. Every entry is a file
#: that still builds its own launch. Migrating one deletes its entry; nothing
#: may ever be added. ``daemon/launcher.py`` is the odd one out and stays: it
#: spawns the daemon itself, not an OpenSSH child.
APPROVED_DIRECT_LAUNCHERS = frozenset(
    {
        # -- Runners that own a spawn the launcher cannot express ------------
        # These start children themselves and register them themselves. A PTY
        # child needs ``pass_fds``; the broadcast runner owns a select loop
        # with bounded output retention. Their *composition* and credential
        # lifecycle already go through the launcher.
        ("daemon/broadcast_service.py", "Popen"),
        ("daemon/forward_runtime.py", "Popen"),
        ("daemon/pty_runner.py", "Popen"),
        ("daemon/session_runtime.py", "Popen"),
        ("daemon/sftp_runtime.py", "Popen"),
        # -- Injectable defaults kept as test seams ---------------------------
        # ``popen=subprocess.Popen`` on the constructor. The spawn itself goes
        # through the scope, which is what records the child.
        ("daemon/native_scp_backend.py", "Popen"),
        ("daemon/privileged_file_service.py", "Popen"),
        ("daemon/identity_service.py", "Popen"),
        # -- Not an OpenSSH child at all --------------------------------------
        # Spawns the daemon itself.
        ("daemon/launcher.py", "Popen"),
        # -- Helper commands with no connection -------------------------------
        # ``ssh-add`` and ``ssh-keygen`` are agent/key tools, not connection
        # launches: there is no connection for the launch provider to compose
        # from. ``key_service`` additionally ties a scope's lifetime to the
        # owning client rather than to one call, so it cannot use a scope
        # context manager. Giving these a home in the launcher means modelling
        # a non-connection command intent; until then they own their scopes.
        ("daemon/identity_service.py", "cancel_session"),
        ("daemon/identity_service.py", "prepare_operation_launch"),
        ("daemon/key_service.py", "cancel_session"),
        ("daemon/key_service.py", "prepare_operation_launch"),
        # -- Scope owners -----------------------------------------------------
        # Sessions are cancelled centrally on interaction-cancelling events.
        # Construction is the launcher's; ownership stays here.
        ("daemon/server.py", "cancel_session"),
        # -- Public plugin API ------------------------------------------------
        # Protocol plugins resolve their own auth through the plugin API.
        # Migrating them is a plugin-API change, not a daemon change.
        ("plugins/builtin/mosh_protocol/__init__.py", "resolve_native_auth"),
        ("plugins/builtin/ssh_protocol/__init__.py", "build_ssh_connection"),
    }
)


def current_identity() -> frozenset:
    return frozenset(
        (rel, name)
        for rel, hits in current_direct_launchers().items()
        for name in hits
    )


def test_no_new_direct_launchers():
    added = current_identity() - APPROVED_DIRECT_LAUNCHERS
    assert not added, (
        "these reach the SSH launch stack directly instead of going through "
        "sshpilot.daemon.ssh_launch.SshLauncher: "
        + ", ".join(f"{rel}:{name}" for rel, name in sorted(added))
    )


def test_approved_list_has_no_stale_entries():
    """The ratchet only tightens if migrated entries are removed."""

    stale = APPROVED_DIRECT_LAUNCHERS - current_identity()
    assert not stale, (
        "these no longer reach the launch stack and must be removed from "
        "APPROVED_DIRECT_LAUNCHERS so the ratchet cannot loosen again: "
        + ", ".join(f"{rel}:{name}" for rel, name in sorted(stale))
    )


def test_second_order_services_do_not_touch_the_launch_stack():
    for rel in sorted(SECOND_ORDER):
        assert not scan_file(rel), (
            f"{rel} delegates execution to a first-order service and must not "
            "build or spawn a launch itself"
        )
    for rel in sorted(SECOND_ORDER_FRONTEND):
        assert not scan_file(rel), (
            f"{rel} states that it never spawns ssh itself; that is now checked"
        )


def test_second_order_services_do_not_import_the_launcher():
    """Delegation is the point: the choke point is not a shortcut past it.

    Host Info and the backup transports inherit cancellation, bounded output
    and the operation lifecycle from ``BroadcastCommandService`` (and the SFTP
    and transfer runtimes). Reaching for ``SshLauncher`` directly would look
    correct and quietly drop all of it.
    """

    for rel in sorted(SECOND_ORDER | SECOND_ORDER_FRONTEND):
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"), rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert "ssh_launch" not in (node.module or ""), rel
            elif isinstance(node, ast.Import):
                assert not any("ssh_launch" in a.name for a in node.names), rel


def test_the_choke_point_owns_the_credential_ordering():
    """``mark_authenticated`` must precede ``cancel_session`` in the scope.

    This is the hazard the whole exercise exists to remove: ``cancel_session``
    destroys the askpass context and clears its pending remembered secrets, so
    a scope that tears down first silently discards a credential the user
    explicitly asked to save.
    """

    source = (ROOT / OWNER).read_text(encoding="utf-8")
    close = source.index("def close(")
    body = source[close:]
    assert body.index("mark_authenticated") < body.index("cancel_session")
