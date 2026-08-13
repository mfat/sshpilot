"""Secret-backend policy (interfaces and decisions; no GTK prompts)."""
from .policy import (
    SecretBackendName,
    SecretDecisionKind,
    SecretPolicyDecision,
    decide_unlock,
    normalize_backend_name,
    platform_default_order,
    resolve_lookup_order,
    resolve_store_order,
)
from .protocols import (
    BackendInfo,
    FallbackDecision,
    SecretBackendProtocol,
    SecretCapability,
    SecretRef,
)

__all__ = [
    "BackendInfo",
    "FallbackDecision",
    "SecretBackendName",
    "SecretBackendProtocol",
    "SecretCapability",
    "SecretDecisionKind",
    "SecretPolicyDecision",
    "SecretRef",
    "decide_unlock",
    "normalize_backend_name",
    "platform_default_order",
    "resolve_lookup_order",
    "resolve_store_order",
]
