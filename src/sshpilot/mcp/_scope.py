"""Safe, repository-confined file access for the dev MCP server.

This module is pure stdlib and must stay importable without the optional
``mcp`` dependency so path-safety tests can run in any environment.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Iterator, List, Optional

#: Environment variable that pins the repository root explicitly.
SSHPILOT_MCP_ROOT = "SSHPILOT_MCP_ROOT"

#: Marker used for Git checkouts.
_GIT_MARKER = ".git"
#: Additional markers that identify a project checkout root.
_PROJECT_MARKERS = ("pyproject.toml", "meson.build")

#: Directory names that are never searched or indexed (VCS, build, and
#: tooling artifacts). Deliberately conservative: source, docs, and tests
#: should always win.
SKIP_DIR_NAMES = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "build",
        "builddir",
        "dist",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".tox",
        ".flatpak-builder",
        "sshpilot.egg-info",
        ".tx",
        ".codebase-memory",
    }
)
#: File names/suffixes that are never searched or indexed.
SKIP_FILE_SUFFIXES = frozenset({".pyc", ".pyo", ".so", ".dll", ".dylib"})


class ScopeError(RuntimeError):
    """Raised when a path cannot be resolved inside the repository scope."""


class RepoScope:
    """Confines dev-MCP file access to a single repository root (read-only)."""

    def __init__(self, root: Path):
        resolved = Path(root).expanduser().resolve()
        if not resolved.is_dir():
            raise ScopeError(f"repository root is not a directory: {resolved}")
        self._root = resolved

    @classmethod
    def discover(cls, cwd: Optional[Path] = None, env: Optional[Dict[str, str]] = None) -> "RepoScope":
        """Discover the repository root.

        Precedence: the ``SSHPILOT_MCP_ROOT`` environment variable, then the
        nearest ancestor of *cwd* (or the process working directory) that
        contains a Git marker or a project marker.
        """
        environment = os.environ if env is None else env
        start = Path(cwd).resolve() if cwd is not None else Path.cwd().resolve()
        pinned = environment.get(SSHPILOT_MCP_ROOT)
        if pinned:
            return cls(Path(pinned))
        for directory in (start, *start.parents):
            if (directory / _GIT_MARKER).exists() or any(
                (directory / marker).exists() for marker in _PROJECT_MARKERS
            ):
                return cls(directory)
        raise ScopeError(
            f"no repository root found above {start}; set {SSHPILOT_MCP_ROOT}"
        )

    @property
    def root(self) -> Path:
        """The canonical, symlink-resolved repository root."""
        return self._root

    def is_within(self, path: Path) -> bool:
        """Return whether *path* (symlinks resolved) stays inside the root."""
        try:
            real = Path(path).resolve()
        except OSError:
            return False
        return real == self._root or self._root in real.parents

    def resolve(self, relative: str) -> Path:
        """Resolve a repository-relative path and confine it to the root.

        Rejects empty paths, absolute paths, null bytes, traversal escapes,
        and symlinks that leave the repository.
        """
        if not isinstance(relative, str):
            raise TypeError(f"path must be a string, got {type(relative).__name__}")
        if not relative:
            raise ScopeError("empty path")
        if "\x00" in relative:
            raise ScopeError("path contains a null byte")
        if os.path.isabs(relative):
            raise ScopeError(f"absolute path not allowed: {relative!r}")
        real = (self._root / relative).resolve()
        if real != self._root and self._root not in real.parents:
            raise ScopeError(f"path escapes repository root: {relative!r}")
        return real

    def read_text(self, relative: str, max_bytes: int = 64 * 1024) -> str:
        """Read a UTF-8 text file from the repository, read-only and capped."""
        if max_bytes < 1:
            raise ValueError("max_bytes must be >= 1")
        path = self.resolve(relative)
        if not path.is_file():
            raise ScopeError(f"not a file inside repository: {relative!r}")
        if path.stat().st_size > max_bytes:
            raise ScopeError(f"file exceeds {max_bytes} bytes: {relative!r}")
        return path.read_text(encoding="utf-8")

    def list_directory(self, relative: str = ".") -> List[dict]:
        """List the entries of a repository-relative directory, sorted.

        Returned entries carry ``name`` and one of ``is_dir``/``is_file``.
        """
        path = self.resolve(relative)
        if not path.is_dir():
            raise ScopeError(f"not a directory inside repository: {relative!r}")
        entries = []
        for child in sorted(path.iterdir(), key=lambda item: item.name):
            entries.append(
                {
                    "name": child.name,
                    "is_dir": child.is_dir() and not child.is_symlink(),
                    "is_file": child.is_file(),
                }
            )
        return entries

    def iter_files(
        self,
        prefix: Optional[str] = None,
        include_suffixes: Optional[set] = None,
    ) -> Iterator[Path]:
        """Yield repository-relative file paths, skipping artifact directories.

        ``prefix`` scopes the walk to a repository-relative directory. Only
        files whose real path stays inside the root (symlink-escape-safe) are
        yielded.
        """
        base = self.resolve(prefix) if prefix else self._root
        suffixes = include_suffixes
        for dirpath, dirnames, filenames in os.walk(base, topdown=True):
            dirnames[:] = sorted(
                name
                for name in dirnames
                if name not in SKIP_DIR_NAMES and not name.startswith(".git")
            )
            for filename in sorted(filenames):
                full = Path(dirpath) / filename
                if full.suffix in SKIP_FILE_SUFFIXES:
                    continue
                if suffixes is not None and full.suffix not in suffixes:
                    continue
                if not self.is_within(full):
                    continue
                yield full.relative_to(self._root)
        return

    def iter_text_within(
        self, prefix: Optional[str] = None, max_bytes: int = 2 * 1024 * 1024
    ) -> Iterator[tuple[Path, str]]:
        """Yield (relative path, text) for readable text files within cap."""
        for relative in self.iter_files(prefix=prefix):
            path = self._root / relative
            if not path.is_file() or path.stat().st_size > max_bytes:
                continue
            raw = path.read_bytes()
            if b"\x00" in raw:
                continue
            try:
                yield relative, raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
