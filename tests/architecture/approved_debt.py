"""Immutable identity baseline for currently approved architecture debt.

This file is intentionally test-only and manually reviewed.  It must not be
generated from the live registries: adding a new exception to a registry must
therefore be accompanied by an explicit architecture decision.
"""

from __future__ import annotations


# Entries are (registry identity, migration tag).  Explanatory text is not
# part of the identity because wording may improve while the approved edge is
# unchanged.
APPROVED_PENDING = frozenset(
    {
        (("backup_manager.py", "import_export", "MergeStrategy"), "M6"),
        (("backup_manager.py", "import_export", "atomic_write_json"), "M6"),
        (("backup_manager.py", "import_export", "migrate_payload"), "M6"),
        (("backup_manager.py", "import_export", "plan_import"), "M6"),
        (("config.py", "settings", "CONFIG_VERSION"), "M4"),
        (("config.py", "settings", "ensure_config_defaults"), "M4"),
        (("config.py", "settings", "get_default_config"), "M4"),
        (("secret_storage.py", "secrets", "SecretDecisionKind"), "M5"),
        (("secret_storage.py", "secrets", "decide_unlock"), "M5"),
        (("secret_storage.py", "secrets", "normalize_backend_name"), "M5"),
        (("secret_storage.py", "secrets", "platform_default_order"), "M5"),
        (("ssh_connection_builder.py", "ssh", "AuthMethod"), "M7"),
        (("ssh_connection_builder.py", "ssh", "HostKeyMode"), "M7"),
        (("ssh_connection_builder.py", "ssh", "LaunchMode"), "M7"),
        (("ssh_connection_builder.py", "ssh", "ProcessSpec"), "M7"),
        (("ssh_connection_builder.py", "ssh", "SSHLaunchRequest"), "M7"),
        (("ssh_connection_builder.py", "ssh", "build_ssh_process_spec"), "M7"),
    }
)

APPROVED_BACKEND_OPS = frozenset(
    {
        (("agent_client.py", "subprocess"), "frontend"),
        (("askpass_utils.py", "ssh_binary"), "M7"),
        (("askpass_utils.py", "subprocess"), "M7"),
        (("bitwarden_setup.py", "subprocess"), "frontend"),
        (("plugins/api.py", "subprocess"), "frontend"),
        (("providers/system_agent.py", "ssh_binary"), "M7"),
        (("providers/system_agent.py", "subprocess"), "M7"),
        (("secret_storage.py", "SecretManager"), "M5"),
        (("secret_storage.py", "subprocess"), "M5"),
        (("sftp_utils.py", "subprocess"), "frontend"),
        (("ssh_config_utils.py", "ssh_binary"), "M7"),
        (("ssh_config_utils.py", "subprocess"), "M7"),
        (("ssh_multiplex.py", "ssh_binary"), "M7"),
        (("ssh_multiplex.py", "subprocess"), "M7"),
        (("terminal.py", "subprocess"), "frontend"),
    }
)

APPROVED_DAEMON_DEBT = frozenset(
    {
        (("askpass_utils.py", "sshpilot.secret_storage"), "M5"),
        (("backup_manager.py", "sshpilot.config"), "M6"),
        (("credential_model.py", "sshpilot.secret_storage"), "M5"),
        (("daemon/cli.py", "sshpilot.platform_utils"), "M4"),
        (("daemon/connection_launch_provider.py", "sshpilot.plugins"), "M8"),
        (("daemon/connection_launch_provider.py", "sshpilot.ssh_connection_builder"), "M7"),
        (("daemon/connection_secret_provider.py", "sshpilot.askpass_utils"), "M5"),
        (("daemon/connection_secret_provider.py", "sshpilot.credential_model"), "M5"),
        (("daemon/connection_secret_provider.py", "sshpilot.secret_storage"), "M5"),
        (("daemon/launcher.py", "sshpilot.platform_utils"), "M4"),
        (("daemon/secret_backend_service.py", "sshpilot.secret_storage"), "M5"),
        (("daemon/secret_transfer.py", "sshpilot.backup_manager"), "M6"),
        (("daemon/secret_transfer.py", "sshpilot.credential_model"), "M5"),
        (("daemon/secret_transfer.py", "sshpilot.secret_storage"), "M5"),
        (("daemon/secret_transfer.py", "sshpilot.ssh_connection_builder"), "M7"),
        (("plugins/api.py", "sshpilot.plugins.host"), "M8"),
    }
)

APPROVED_CORE_DEBT = frozenset()


def debt_identity(registry: dict, *, include_frontend: bool = True) -> frozenset:
    """Convert a keyed ``entry -> tag`` registry to its ratchet identity."""

    return frozenset(
        (entry, value[0] if isinstance(value, tuple) else value)
        for entry, value in registry.items()
        if include_frontend
        or (value[0] if isinstance(value, tuple) else value) != "frontend"
    )


def assert_debt_does_not_grow(current, approved, name: str) -> None:
    """Raise with the exact newly introduced identities, if any."""

    added = set(current) - set(approved)
    assert not added, (
        f"{name} contains unapproved architecture debt entries: "
        + ", ".join(repr(item) for item in sorted(added))
    )
