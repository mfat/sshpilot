"""Pure helpers for remote (POSIX) path manipulation and SSH target formatting.

Extracted verbatim from window.py so the logic is importable without pulling in
the GTK window module. This also breaks the import cycle these helpers used to
create: scp_window.py and scp_utils.py reached back into window.py just to reuse
them. They have no GTK or I/O dependencies — only posixpath/shlex — so they live
in a leaf module that anything may import.
"""

import posixpath
import shlex
from typing import Optional


def _format_ssh_target(host: str, user: str) -> str:
    """Format SSH target as user@host."""
    host_component = host or ''
    if host_component and ':' in host_component and not (
        host_component.startswith('[') and host_component.endswith(']')
    ):
        host_component = f'[{host_component}]'
    return f'{user}@{host_component}' if user else host_component


def _normalize_remote_path(path: str) -> str:
    text = (path or '').strip()
    if not text:
        return '.'
    if text in {'.', '/', '~'}:
        return text
    if text.startswith('~/'):
        trimmed = text.rstrip('/')
        return trimmed if trimmed else '~'
    if text.startswith('/'):
        normalized = posixpath.normpath(text)
        return normalized if normalized.startswith('/') else f'/{normalized}'
    normalized = posixpath.normpath(text)
    return normalized or '.'


def _remote_parent(path: str) -> Optional[str]:
    normalized = _normalize_remote_path(path)
    if normalized in {'.', '/'}:
        return None
    if normalized == '~':
        return '/'
    if normalized.startswith('~/'):
        parent = normalized.rsplit('/', 1)[0]
        return parent or '~'
    parent = posixpath.dirname(normalized.rstrip('/'))
    if not parent:
        return '.'
    if parent == normalized:
        return None
    return parent


def _remote_join(base: str, child: str) -> str:
    base_normalized = _normalize_remote_path(base)
    child = (child or '').strip()
    if child in {'', '.'}:
        return base_normalized
    if child == '..':
        parent = _remote_parent(base_normalized)
        return parent if parent is not None else base_normalized
    if base_normalized in {'.', ''}:
        return _normalize_remote_path(child)
    if base_normalized == '~':
        return _normalize_remote_path(f"~/{child.lstrip('/')}")
    if base_normalized == '/':
        return _normalize_remote_path(f"/{child.lstrip('/')}")
    return _normalize_remote_path(f"{base_normalized.rstrip('/')}/{child}")


def _quote_remote_path_for_shell(path: str) -> str:
    normalized = _normalize_remote_path(path)
    if normalized == '.':
        return '.'
    if normalized == '/':
        return '/'
    if normalized == '~':
        return '$HOME'
    if normalized.startswith('~/'):
        remainder = normalized[2:]
        if not remainder:
            return '$HOME'
        parts = [shlex.quote(seg) for seg in remainder.split('/')]
        return '$HOME/' + '/'.join(parts)
    return shlex.quote(normalized)
