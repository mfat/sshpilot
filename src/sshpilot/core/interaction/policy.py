"""Askpass / interaction policy (GTK-free).

Core decides *what* interaction is required. GTK implements a provider that
turns :class:`InteractionRequest` into dialogs. Daemon/headless code depends on
these models — never on GTK classes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional, Sequence


class PromptKind(str, Enum):
    PASSWORD = "password"
    PASSPHRASE = "passphrase"
    HOST_KEY = "hostkey"
    USERNAME = "username"
    KEYBOARD_INTERACTIVE = "interactive"
    VAULT_UNLOCK = "vault_unlock"
    PRESENCE = "presence"
    UNKNOWN = "unknown"


class InteractionOutcome(str, Enum):
    ACCEPTED = "accepted"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"
    MALFORMED = "malformed"
    RETRY_EXHAUSTED = "retry_exhausted"


class ResponseType(str, Enum):
    SECRET = "secret"
    CONFIRM_YES = "confirm_yes"
    CONFIRM_NO = "confirm_no"
    ACCEPT = "accept"
    REJECT = "reject"
    USERNAME = "username"
    CANCEL = "cancel"


_PRESENCE_MARKERS = (
    "confirm user presence",
    "tap your security key",
    "tap secure token",
    "touch your yubikey",
    "touch your security key",
    "touch your authenticator",
    "touch the authenticator",
)
_HOSTKEY_MARKERS = (
    "(yes/no",
    "continue connecting",
    "please type 'yes'",
)
# Word-boundary match: bare substrings would misfire on hostnames/usernames.
_OTP_MFA_MARKER_RE = re.compile(
    r"\b(?:pin|otp|totp|mfa|2fa|two[-\s]?factor|one[-\s]?time)\b",
    re.IGNORECASE,
)
_TYPED_INTERACTIVE_RE = re.compile(
    r"\b(?:pin|otp|totp|mfa|2fa|two[-\s]?factor|"
    r"verification code|one[-\s]?time|authentication code)\b",
    re.IGNORECASE,
)


def classify_prompt(text: str) -> Optional[PromptKind]:
    """Classify an OpenSSH askpass prompt string (no UI)."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return None
    last = lines[-1].lower()
    if any(marker in last for marker in _PRESENCE_MARKERS):
        return PromptKind.PRESENCE
    if any(marker in last for marker in _HOSTKEY_MARKERS):
        return PromptKind.HOST_KEY
    if _TYPED_INTERACTIVE_RE.search(last) and (
        last.endswith(":") or last.startswith("enter ") or " for " in last
    ):
        if "passphrase" in last:
            return PromptKind.PASSPHRASE
        if "password" in last and not _OTP_MFA_MARKER_RE.search(last):
            return PromptKind.PASSWORD
        return PromptKind.KEYBOARD_INTERACTIVE
    if last.endswith(":"):
        if "passphrase" in last:
            return PromptKind.PASSPHRASE
        if "password" in last:
            return PromptKind.PASSWORD
        if "user" in last and "name" in last:
            return PromptKind.USERNAME
    return None


@dataclass(frozen=True)
class InteractionPolicy:
    """Retry / timeout / remember rules for an interaction family."""

    timeout_seconds: float = 120.0
    max_attempts: int = 3
    allow_remember: bool = True
    allow_stored_secret: bool = True
    allow_cancel: bool = True
    headless_fails: bool = True


DEFAULT_POLICIES: Mapping[PromptKind, InteractionPolicy] = {
    PromptKind.PASSWORD: InteractionPolicy(timeout_seconds=120.0),
    PromptKind.PASSPHRASE: InteractionPolicy(timeout_seconds=120.0),
    PromptKind.HOST_KEY: InteractionPolicy(
        timeout_seconds=180.0, max_attempts=1, allow_remember=False
    ),
    PromptKind.USERNAME: InteractionPolicy(timeout_seconds=120.0, allow_remember=False),
    PromptKind.KEYBOARD_INTERACTIVE: InteractionPolicy(
        timeout_seconds=120.0, allow_remember=False, allow_stored_secret=False
    ),
    PromptKind.VAULT_UNLOCK: InteractionPolicy(timeout_seconds=300.0, max_attempts=5),
    PromptKind.PRESENCE: InteractionPolicy(
        timeout_seconds=120.0, max_attempts=1, allow_remember=False, allow_stored_secret=False
    ),
    PromptKind.UNKNOWN: InteractionPolicy(allow_remember=False, allow_stored_secret=False),
}


@dataclass(frozen=True)
class InteractionRequest:
    kind: PromptKind
    prompt: str = ""
    hostname: str = ""
    username: str = ""
    port: int = 22
    key_display: str = ""
    attempt: int = 1
    policy: Optional[InteractionPolicy] = None
    allowed_responses: Sequence[ResponseType] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.policy is None:
            object.__setattr__(
                self,
                "policy",
                DEFAULT_POLICIES.get(self.kind, InteractionPolicy()),
            )
        if not self.allowed_responses:
            object.__setattr__(self, "allowed_responses", _default_responses(self.kind))


def _default_responses(kind: PromptKind) -> Tuple[ResponseType, ...]:
    if kind == PromptKind.HOST_KEY:
        return (
            ResponseType.ACCEPT,
            ResponseType.REJECT,
            ResponseType.CANCEL,
        )
    if kind == PromptKind.USERNAME:
        return (ResponseType.USERNAME, ResponseType.CANCEL)
    if kind == PromptKind.PRESENCE:
        return (ResponseType.CONFIRM_YES, ResponseType.CANCEL)
    return (ResponseType.SECRET, ResponseType.CANCEL)


# Fix missing Tuple import
from typing import Tuple  # noqa: E402


@dataclass(frozen=True)
class InteractionResponse:
    outcome: InteractionOutcome
    response_type: Optional[ResponseType] = None
    secret: Optional[str] = None  # never logged by core
    remember: bool = False
    username: Optional[str] = None
    message: str = ""


def build_request_from_prompt(
    text: str,
    *,
    hostname: str = "",
    username: str = "",
    port: int = 22,
    attempt: int = 1,
    ssh_askpass_prompt: str = "",
) -> InteractionRequest:
    """Build a typed request from askpass text / OpenSSH prompt hint."""
    hint = (ssh_askpass_prompt or "").strip().lower()
    if hint == "none":
        kind = PromptKind.PRESENCE
    elif hint == "confirm":
        kind = PromptKind.HOST_KEY
    else:
        kind = classify_prompt(text) or PromptKind.UNKNOWN
    policy = DEFAULT_POLICIES.get(kind, InteractionPolicy())
    return InteractionRequest(
        kind=kind,
        prompt=text or "",
        hostname=hostname,
        username=username,
        port=port,
        attempt=attempt,
        policy=policy,
    )


def decide_headless(request: InteractionRequest) -> InteractionResponse:
    """No-interaction failure path for daemon/CLI without a provider."""
    if request.policy.allow_stored_secret and request.metadata.get("stored_secret"):
        return InteractionResponse(
            outcome=InteractionOutcome.ACCEPTED,
            response_type=ResponseType.SECRET,
            secret=str(request.metadata["stored_secret"]),
            remember=False,
        )
    if request.policy.headless_fails:
        return InteractionResponse(
            outcome=InteractionOutcome.UNAVAILABLE,
            message="No interaction provider available",
        )
    return InteractionResponse(outcome=InteractionOutcome.CANCELLED, response_type=ResponseType.CANCEL)


def validate_response(
    request: InteractionRequest,
    response: InteractionResponse,
) -> InteractionResponse:
    """Normalize / reject malformed responses; enforce retry exhaustion."""
    if response.outcome == InteractionOutcome.TIMEOUT:
        return response
    if response.outcome == InteractionOutcome.CANCELLED:
        if not request.policy.allow_cancel:
            return InteractionResponse(
                outcome=InteractionOutcome.REJECTED,
                message="Cancellation is not allowed for this interaction",
            )
        return response
    if request.attempt > request.policy.max_attempts:
        return InteractionResponse(
            outcome=InteractionOutcome.RETRY_EXHAUSTED,
            message="Maximum interaction attempts exceeded",
        )
    rtype = response.response_type
    if rtype is None:
        return InteractionResponse(
            outcome=InteractionOutcome.MALFORMED,
            message="Missing response_type",
        )
    if rtype not in request.allowed_responses:
        return InteractionResponse(
            outcome=InteractionOutcome.MALFORMED,
            message=f"Response type {rtype.value} not allowed",
        )
    if rtype == ResponseType.SECRET and not response.secret:
        return InteractionResponse(
            outcome=InteractionOutcome.MALFORMED,
            message="Secret response requires a secret value",
        )
    if response.remember and not request.policy.allow_remember:
        return InteractionResponse(
            outcome=response.outcome,
            response_type=response.response_type,
            secret=response.secret,
            remember=False,
            username=response.username,
            message="Remember disallowed by policy",
        )
    return response
