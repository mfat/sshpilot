"""Read-only Git inspection for the dev MCP server.

Strictly read-only: only whitelisted, non-mutating ``git`` subcommands are ever
run, always as explicit ``argv`` (never a shell), confined with ``-C`` to the
repository root. This module must never invoke ``git`` operations that change
the repository.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import List, Optional

#: Hard cap on how much diff/log text is returned to the model.
MAX_OUTPUT_BYTES = 512 * 1024
#: Top-level argument allowed for read-only git invocations.
_SAFE_GIT_ARGS = {
    "status": True,
    "log": True,
    "diff": True,
    "rev-parse": True,
}

#: Safe characters for a user-supplied revision (never a leading "-").
_REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/^~-]*$")


class GitError(RuntimeError):
    """Raised when a read-only git inspection fails."""


def _cap(text: str, max_bytes: int = MAX_OUTPUT_BYTES) -> tuple[str, bool]:
    if len(text.encode("utf-8", errors="replace")) <= max_bytes:
        return text, False
    return text[:max_bytes], True


class GitInspector:
    """Runs read-only git inspection against a single repository root."""

    def __init__(self, root: Path):
        self._root = root

    @property
    def root(self) -> Path:
        return self._root

    def is_git_repo(self) -> bool:
        """Return whether *root* is inside a git working tree (read-only)."""
        try:
            return self._run(["rev-parse", "--is-inside-work-tree"]).returncode == 0
        except GitError:
            return False

    def _run(self, args: List[str], timeout: float = 15.0) -> subprocess.CompletedProcess:
        if not args or args[0] not in _SAFE_GIT_ARGS:
            raise GitError(f"refusing git command: {args!r}")
        argv = ["git", "-C", str(self._root), *args]
        try:
            return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            raise GitError("git executable not found") from None
        except subprocess.TimeoutExpired:
            raise GitError("git command timed out") from None

    def _require(self, args: List[str]) -> str:
        proc = self._run(args)
        if proc.returncode != 0:
            message = proc.stderr.strip()
            raise GitError(message or f"git {args[0]} failed")
        # rstrip, not strip: leading spaces are significant in short-format
        # status (bare " X" means a purely worktree change) and in diffs.
        return proc.stdout.rstrip()

    def status(self) -> dict:
        """Return the working-tree status: branch plus short-format entries."""
        branch = self._require(["rev-parse", "--abbrev-ref", "HEAD"])
        output = self._require(["status", "--short", "--untracked-files=normal"])
        entries = []
        for line in output.splitlines():
            line = line.rstrip("\n")
            if len(line) < 4:
                continue
            entries.append(
                {
                    "state": line[:2].strip(),
                    "path": line[3:],
                }
            )
        return {"branch": branch, "entries": entries, "total": len(entries)}

    def log(self, limit: int = 20) -> dict:
        """Return recent commit subjects (sha + one-line subject)."""
        if not isinstance(limit, int) or limit < 1 or limit > 100:
            raise GitError("limit must be an integer between 1 and 100")
        output = self._require(["log", "--oneline", f"-{limit}"])
        commits = []
        for line in output.splitlines():
            line = line.rstrip("\n")
            sha, _, subject = line.partition(" ")
            commits.append({"sha": sha, "subject": subject})
        return {"commits": commits, "total": len(commits)}

    def diff(
        self,
        base: Optional[str] = "HEAD",
        path: Optional[str] = None,
        stat_only: bool = False,
    ) -> dict:
        """Return the diff of *base* (default HEAD) against the working tree."""
        if base is None:
            base = "HEAD"
        if not isinstance(base, str) or not _REVISION_RE.match(base):
            raise GitError(f"invalid revision: {base!r}")
        if path is not None:
            if not isinstance(path, str) or not path:
                raise GitError("path must be a non-empty repository-relative path")
            # Resolve through a raw check: keep it argv-safe and confined to
            # the root by prefixing with --; actual confinement is delegated
            # to the caller's RepoScope for validation of the same path.
            if "\x00" in path:
                raise GitError("path contains a null byte")
        argv: List[str] = ["diff"]
        if stat_only:
            argv.append("--stat")
        argv.append(base)
        if path is not None:
            argv += ["--", path]
        output = self._require(argv)
        text, truncated = _cap(output)
        return {
            "diff": text,
            "bytes": len(text.encode("utf-8")),
            "truncated": truncated,
        }