"""Utilities for assembling scp command arguments."""

from __future__ import annotations

from typing import Iterable, List, Tuple
import re

_REMOTE_SPEC_RE = re.compile(r"^[^@]+@(?:\[[^\]]+\]|[^:]+):.+$")


def _strip_brackets(value: str) -> str:
    if value.startswith('[') and value.endswith(']'):
        return value[1:-1]
    return value


def _extract_host(target: str) -> str:
    if '@' in target:
        host = target.split('@', 1)[1]
    else:
        host = target
    return _strip_brackets(host)


def _normalize_remote_sources(target: str, sources: Iterable[str]) -> List[str]:
    host = _extract_host(target)
    host_variants = [host] if host else []
    if host and ':' in host:
        bracketed = f"[{host}]"
        if bracketed not in host_variants:
            host_variants.append(bracketed)
    normalized: List[str] = []
    for item in sources:
        path = (item or '').strip()
        if not path:
            continue
        if path.startswith(f"{target}:"):
            normalized.append(path)
            continue
        if host_variants:
            matched_host_variant = False
            for host_variant in host_variants:
                if path.startswith(f"{host_variant}:"):
                    normalized.append(path)
                    matched_host_variant = True
                    break
            if matched_host_variant:
                continue
        if _REMOTE_SPEC_RE.match(path):
            normalized.append(path)
            continue
        normalized.append(f"{target}:{path}")
    return normalized


def assemble_scp_transfer_args(
    target: str,
    sources: Iterable[str],
    destination: str,
    direction: str,
) -> Tuple[List[str], str]:
    """Return normalized scp sources and destination arguments for a transfer.

    Parameters
    ----------
    target:
        The ``user@host`` string for the connection (user may be omitted).
    sources:
        Iterable of source paths supplied by the caller.
    destination:
        Destination path (remote directory for uploads or local path for downloads).
    direction:
        Either ``"upload"`` or ``"download"``.
    """
    direction_value = (direction or '').lower()
    if direction_value not in {'upload', 'download'}:
        raise ValueError(f"Unsupported scp direction: {direction}")

    if direction_value == 'upload':
        cleaned_sources = [s for s in sources if s]
        return cleaned_sources, f"{target}:{destination}"

    remote_sources = _normalize_remote_sources(target, sources)
    return remote_sources, destination


__all__ = ['assemble_scp_transfer_args']
