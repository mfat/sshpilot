"""Transport-neutral protocol envelopes and codecs for local IPC."""

from importlib import import_module
from typing import Any

from .envelopes import (
    ErrorData,
    ErrorResponseEnvelope,
    EventEnvelope,
    HandshakeRequest,
    HandshakeResult,
    RequestEnvelope,
    SuccessResponseEnvelope,
)
from .framing import (
    MAX_FRAME_SIZE,
    FrameDecoder,
    FramingError,
    encode_frame,
    receive_frame,
)

__all__ = [
    "MAX_FRAME_SIZE",
    "ErrorData",
    "ErrorResponseEnvelope",
    "EventEnvelope",
    "FrameDecoder",
    "FramingError",
    "HandshakeRequest",
    "HandshakeResult",
    "RequestEnvelope",
    "SuccessResponseEnvelope",
    "attach_session_request_from_wire",
    "attach_session_request_to_wire",
    "attach_session_result_from_wire",
    "attach_session_result_to_wire",
    "close_session_request_from_wire",
    "close_session_request_to_wire",
    "create_connection_request_from_wire",
    "create_connection_request_to_wire",
    "decode_envelope",
    "delete_connection_request_from_wire",
    "delete_connection_request_to_wire",
    "delete_connection_result_from_wire",
    "delete_connection_result_to_wire",
    "detach_session_request_from_wire",
    "detach_session_request_to_wire",
    "encode_envelope",
    "encode_frame",
    "error_from_wire",
    "error_to_wire",
    "open_session_request_from_wire",
    "open_session_request_to_wire",
    "receive_frame",
    "session_exit_info_from_wire",
    "session_exit_info_to_wire",
    "session_summary_from_wire",
    "session_summary_to_wire",
    "update_connection_request_from_wire",
    "update_connection_request_to_wire",
]

# The codec imports every API model; resolve its exports on first use so that
# importing a light transport submodule (``endpoint``, ``framing``) does not
# pay for it -- ``daemon.lifecycle`` and the GUI's early daemon start do.
_LAZY_EXPORTS = {
    "attach_session_request_from_wire": ("codec", "attach_session_request_from_wire"),
    "attach_session_request_to_wire": ("codec", "attach_session_request_to_wire"),
    "attach_session_result_from_wire": ("codec", "attach_session_result_from_wire"),
    "attach_session_result_to_wire": ("codec", "attach_session_result_to_wire"),
    "close_session_request_from_wire": ("codec", "close_session_request_from_wire"),
    "close_session_request_to_wire": ("codec", "close_session_request_to_wire"),
    "create_connection_request_from_wire": ("codec", "create_connection_request_from_wire"),
    "create_connection_request_to_wire": ("codec", "create_connection_request_to_wire"),
    "decode_envelope": ("codec", "decode_envelope"),
    "delete_connection_request_from_wire": ("codec", "delete_connection_request_from_wire"),
    "delete_connection_request_to_wire": ("codec", "delete_connection_request_to_wire"),
    "delete_connection_result_from_wire": ("codec", "delete_connection_result_from_wire"),
    "delete_connection_result_to_wire": ("codec", "delete_connection_result_to_wire"),
    "detach_session_request_from_wire": ("codec", "detach_session_request_from_wire"),
    "detach_session_request_to_wire": ("codec", "detach_session_request_to_wire"),
    "encode_envelope": ("codec", "encode_envelope"),
    "error_from_wire": ("codec", "error_from_wire"),
    "error_to_wire": ("codec", "error_to_wire"),
    "open_session_request_from_wire": ("codec", "open_session_request_from_wire"),
    "open_session_request_to_wire": ("codec", "open_session_request_to_wire"),
    "session_exit_info_from_wire": ("codec", "session_exit_info_from_wire"),
    "session_exit_info_to_wire": ("codec", "session_exit_info_to_wire"),
    "session_summary_from_wire": ("codec", "session_summary_from_wire"),
    "session_summary_to_wire": ("codec", "session_summary_to_wire"),
    "update_connection_request_from_wire": ("codec", "update_connection_request_from_wire"),
    "update_connection_request_to_wire": ("codec", "update_connection_request_to_wire"),
}


def __getattr__(name: str) -> Any:
    target = _LAZY_EXPORTS.get(name)
    if target is not None:
        mod_name, attr_name = target
        mod = import_module(f".{mod_name}", __name__)
        val = getattr(mod, attr_name)
        globals()[name] = val
        return val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals().keys()) | set(__all__))
