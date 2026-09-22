"""Frontend-owned presentation of Ásbrú import notices and summaries."""

from __future__ import annotations

from gettext import gettext as _, ngettext

from ..api.errors import ErrorCode, SshPilotError
from ..api.models.connections import (
    AsbruImportMessage,
    AsbruImportMessageCode as Code,
    AsbruImportResult,
)
from ..i18n import N_


_TEMPLATES = {
    Code.EXPORT_NOT_FOUND: N_("Ásbrú export not found: {path}"),
    Code.EXPORT_UNREADABLE: N_("Could not read Ásbrú export: {path}"),
    Code.PYYAML_MISSING: N_("PyYAML is required to import Ásbrú exports (pip install PyYAML)."),
    Code.YAML_INVALID: N_("The Ásbrú export contains invalid YAML."),
    Code.EXPORT_EMPTY: N_("Ásbrú export is empty."),
    Code.EXPORT_NOT_MAPPING: N_("Ásbrú export must be a YAML mapping."),
    Code.NO_ENTRIES: N_("No connections or groups found. Use Ásbrú's 'Export selected connections' (not the live asbru.yml)."),
    Code.LIVE_CONFIG: N_("This looks like a live asbru.yml, not an export. In Ásbrú, select connections → right-click → Export."),
    Code.FULL_CONFIG_SECTION: N_("Parsed the environments section from a full Ásbrú config; prefer 'Export selected connections' for a clean import."),
    Code.SKIPPED_NON_SSH: N_("Skipped non-SSH connection {name!r} (method={method!r})."),
    Code.SKIPPED_MISSING_NAME: N_("Skipped connection {source_id}: missing name."),
    Code.SKIPPED_MISSING_HOST: N_("Skipped connection {name!r}: missing host/IP."),
    Code.EXPECT_NOT_IMPORTED: N_("{name!r}: Ásbrú expect/macros/variables are not imported."),
    Code.RENAMED_ALIAS: N_("Renamed {name!r} → Host alias {nickname!r}."),
    Code.SKIPPED_EMPTY_GROUP: N_("Skipped empty group {name!r} (no importable SSH connections)."),
    Code.GROUPS_ONLY: N_("Export contained groups only; no SSH connections imported."),
    Code.LOAD_FAILED: N_("Could not load the Ásbrú export."),
    Code.GROUP_CREATE_FAILED: N_("Could not create group {name!r}."),
    Code.GROUP_NO_ID: N_("Could not create group {name!r}: no group ID was returned."),
    Code.CONNECTION_CREATE_FAILED: N_("Could not create connection {nickname!r}."),
    Code.ASSIGN_FAILED: N_("Could not assign connection {nickname!r} to its group."),
    Code.PARSE_FAILED: N_("Ásbrú import could not be parsed."),
    Code.ALL_EXIST: N_("All Ásbrú connections already exist; nothing imported."),
    Code.PARTIAL_FAILURES: N_("Ásbrú import completed with partial failures."),
    Code.NO_CHANGES: N_("Ásbrú import produced no changes."),
}

_RPC_ERROR_TEMPLATES = {
    ErrorCode.UNSUPPORTED_CAPABILITY: N_("Ásbrú import is unavailable on this daemon."),
    ErrorCode.API_VERSION_MISMATCH: N_("The daemon API version does not match this application."),
    ErrorCode.DAEMON_UNAVAILABLE: N_("Daemon connection unavailable."),
    ErrorCode.TRANSPORT_CLOSED: N_("Daemon connection unavailable."),
}


def format_asbru_message(message: AsbruImportMessage) -> str:
    """Translate a known reason, then append raw diagnostic unchanged."""
    if type(message) is not AsbruImportMessage:
        raise ValueError("invalid Ásbrú import message")
    try:
        template = _TEMPLATES[message.code]
    except KeyError:
        raise ValueError("Ásbrú import message has no presentation") from None
    translated = _(template).format(**message.parameters)
    return f"{translated}\n{message.diagnostic}" if message.diagnostic else translated


def format_asbru_messages(messages: tuple[AsbruImportMessage, ...]) -> str:
    return "\n".join(format_asbru_message(message) for message in messages)


def _connection_count(count: int) -> str:
    return ngettext("{count} connection", "{count} connections", count).format(count=count)


def _group_count(count: int) -> str:
    return ngettext("{count} group", "{count} groups", count).format(count=count)


def _skipped_count(count: int) -> str:
    return ngettext("{count} existing nickname", "{count} existing nicknames", count).format(count=count)


def format_asbru_preview_counts(add: int, groups: int, skip: int) -> str:
    """Translate each independent count before assembling the question."""
    skipped = _skipped_count(skip)
    if add == 0 and groups == 0:
        return _("No new connections to import. {skipped} would be skipped.").format(
            skipped=skipped
        )
    return _("Import {connections} and {groups}?\n{skipped} will be skipped.").format(
        connections=_connection_count(add), groups=_group_count(groups), skipped=skipped
    )


def format_asbru_result_message(result: AsbruImportResult) -> str:
    if result.message is None:
        return _("Import finished.")
    if result.message.code is Code.IMPORTED:
        return _("Imported {connections} and {groups}.").format(
            connections=_connection_count(len(result.connections_added)),
            groups=_group_count(len(result.groups_added)),
        )
    return format_asbru_message(result.message)


def format_asbru_rpc_error(error: BaseException) -> str:
    """Keep unexpected transport detail opaque and outside gettext."""
    if isinstance(error, SshPilotError):
        template = _RPC_ERROR_TEMPLATES.get(error.code)
        if template is not None:
            return _(template)
    message = _("Could not import the Ásbrú export.")
    diagnostic = str(error).strip()
    return f"{message}\n{diagnostic}" if diagnostic else message
