"""Source/documentation search for the dev MCP server."""

from __future__ import annotations

import re
from typing import List, Optional

from .._scope import RepoScope

#: Default match cap so a broad query cannot flood the model context.
DEFAULT_MAX_RESULTS = 100
#: Default per-file scan cap; larger files are skipped as text.
MAX_FILE_BYTES = 4 * 1024 * 1024

#: Length of a returned match snippet, including any ellipsis.
SNIPPET_LIMIT = 300


def _clip(text: str, limit: int = SNIPPET_LIMIT) -> str:
    clipped = text.rstrip("\n").strip()
    if len(clipped) <= limit:
        return clipped
    return clipped[: limit - 1] + "…"


def search_files(
    scope: RepoScope,
    pattern: str,
    path_prefix: Optional[str] = None,
    ignore_case: bool = False,
    max_results: int = DEFAULT_MAX_RESULTS,
) -> dict:
    """Search repository text files for a regular expression.

    Returns one match record per matching line: the repository-relative file,
    the 1-based line number, and a clipped snippet. Binary and oversized
    files are skipped. The match cap bounds both work and response size.
    """
    if not isinstance(pattern, str) or not pattern:
        raise ValueError("pattern must be a non-empty string")
    if not isinstance(max_results, int) or max_results < 1 or max_results > 10_000:
        raise ValueError("max_results must be an integer between 1 and 10000")
    flags = re.IGNORECASE if ignore_case else 0
    try:
        regex = re.compile(pattern, flags)
    except re.error as error:
        raise ValueError(f"invalid regular expression: {error}") from error

    matches: List[dict] = []
    for relative, text in scope.iter_text_within(prefix=path_prefix, max_bytes=MAX_FILE_BYTES):
        for lineno, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                matches.append(
                    {"file": str(relative), "line": lineno, "snippet": _clip(line)}
                )
                if len(matches) >= max_results:
                    return {"matches": matches, "total": len(matches), "truncated": True}
    return {"matches": matches, "total": len(matches), "truncated": False}