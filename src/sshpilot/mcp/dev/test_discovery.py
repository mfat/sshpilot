"""Pytest test discovery for the dev MCP server."""

from __future__ import annotations

import re
from typing import List, Optional

from .._scope import RepoScope, ScopeError

#: Standard pytest filename conventions.
TEST_NAME_PATTERNS = (re.compile(r"^test_.*\.py$"), re.compile(r"^.*_test\.py$"))


def find_test_files(scope: RepoScope, path_prefix: Optional[str] = None) -> dict:
    """Discover pytest-style test files (``test_*.py`` / ``*_test.py``)."""
    if path_prefix is not None:
        try:
            scope.resolve(path_prefix)
        except ScopeError as error:
            raise ValueError(str(error)) from error
    files: List[str] = []
    for relative in scope.iter_files(prefix=path_prefix, include_suffixes={".py"}):
        if any(pattern.match(relative.name) for pattern in TEST_NAME_PATTERNS):
            files.append(str(relative))
    files.sort()
    return {"test_files": files, "total": len(files)}