"""Frontend-owned presentation of built-in plugin session launch failures."""

from __future__ import annotations

from gettext import gettext as _

from ..api.models.sessions import (
    PluginSessionFailure,
    PluginSessionFailureCode,
)
from ..i18n import N_


_PLUGIN_SESSION_FAILURE_TEMPLATES = {
    PluginSessionFailureCode.CONTAINER_REQUIRED: N_(
        "No container is configured for this connection."
    ),
    PluginSessionFailureCode.CONTAINER_RUNTIME_UNAVAILABLE: N_(
        "The '{runtime}' program is not installed. Install it to use container connections."
    ),
    PluginSessionFailureCode.POD_REQUIRED: N_(
        "No pod is configured for this connection."
    ),
    PluginSessionFailureCode.KUBECTL_UNAVAILABLE: N_(
        "The '{program}' program is not installed. Install it to use Kubernetes connections."
    ),
    PluginSessionFailureCode.MOSH_UNAVAILABLE: N_(
        "The '{client_program}' program is not installed. Install it (and "
        "'{server_program}' on the host) to use Mosh connections."
    ),
    PluginSessionFailureCode.HOST_REQUIRED: N_(
        "No host is configured for this connection."
    ),
    PluginSessionFailureCode.MOSH_PREPARATION_FAILED: N_(
        "The Mosh connection could not be prepared."
    ),
    PluginSessionFailureCode.ARGUMENTS_INVALID: N_(
        "{field} could not be parsed."
    ),
    PluginSessionFailureCode.SERIAL_DEVICE_REQUIRED: N_(
        "No serial device is configured for this connection."
    ),
    PluginSessionFailureCode.SERIAL_SCREEN_HARDWARE_FLOW_UNSUPPORTED: N_(
        "Only '{fallback_program}' is available, which cannot set hardware "
        "({flow}) flow control. Install '{preferred_program}' to use this connection."
    ),
    PluginSessionFailureCode.SERIAL_SCREEN_DATABITS_UNSUPPORTED: N_(
        "Only '{fallback_program}' is available, which cannot set {databits} data "
        "bits. Install '{preferred_program}' to use this connection."
    ),
    PluginSessionFailureCode.SERIAL_SCREEN_HARDWARE_FLOW_AND_DATABITS_UNSUPPORTED: N_(
        "Only '{fallback_program}' is available, which cannot set hardware "
        "({flow}) flow control or {databits} data bits. Install "
        "'{preferred_program}' to use this connection."
    ),
    PluginSessionFailureCode.SERIAL_PROGRAMS_UNAVAILABLE: N_(
        "Neither '{preferred_program}' nor '{fallback_program}' is installed. "
        "Install one to use serial connections."
    ),
}

_PLUGIN_FIELD_LABELS = {
    "command": N_("Command"),
    "extra_ssh_opts": N_("Extra SSH options"),
}


def format_plugin_session_failure(failure: PluginSessionFailure) -> str:
    """Translate one plugin failure, then append its diagnostic unchanged."""

    if type(failure) is not PluginSessionFailure:
        raise ValueError("invalid plugin session failure")
    try:
        template = _PLUGIN_SESSION_FAILURE_TEMPLATES[failure.code]
    except KeyError:
        raise ValueError(
            "plugin session failure code has no frontend presentation"
        ) from None
    parameters = dict(failure.parameters)
    if failure.code is PluginSessionFailureCode.ARGUMENTS_INVALID:
        try:
            field_label = _PLUGIN_FIELD_LABELS[parameters["field"]]
        except KeyError:
            raise ValueError(
                "plugin session failure field has no frontend presentation"
            ) from None
        parameters["field"] = _(field_label)
    message = _(template).format(**parameters)
    return f"{message}\n\n{failure.diagnostic}" if failure.diagnostic else message


__all__ = ["format_plugin_session_failure"]
