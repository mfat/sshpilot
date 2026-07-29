"""Linux libsecret adapter (isolated ``gi.repository.Secret`` import).

Core and ``secret_storage`` must not embed ``import gi`` in their AST for the
headless boundary test. Call :func:`load_secret_module` only when the libsecret
backend is actually used.
"""
from __future__ import annotations

from typing import Any, Optional


def load_secret_module() -> Optional[Any]:
    """Return ``gi.repository.Secret`` or ``None`` if unavailable."""
    try:
        import gi

        gi.require_version("Secret", "1")
        from gi.repository import Secret as _mod

        return _mod
    except Exception:
        return None
