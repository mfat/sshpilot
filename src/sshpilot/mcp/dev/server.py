"""sshpilot-dev-mcp server: contributor-safe repository intelligence.

The ``mcp`` package is imported lazily so this module stays importable (and
unit-testable for path safety) in environments without the optional MCP
dependency.
"""

from __future__ import annotations

import sys
from typing import Any, Optional

from .._scope import RepoScope, ScopeError
from . import api_surface, architecture, execution, search, symbols, test_discovery
from ._git import GitInspector, GitError

SERVER_NAME = "sshpilot-dev-mcp"
SERVER_VERSION = "0.1.0"
DEFAULT_MAX_FILE_BYTES = 64 * 1024


def create_server(scope: Optional[RepoScope] = None) -> Any:
    """Build the dev MCP server, discovering the repository if not pinned."""
    from mcp.server.mcpserver import MCPServer

    if scope is None:
        scope = RepoScope.discover()

    server = MCPServer(
        name=SERVER_NAME,
        version=SERVER_VERSION,
        description=(
            "Contributor-safe repository intelligence for SSH Pilot. Confined "
            "read-only access to a single repository checkout."
        ),
    )

    @server.tool()
    def repo_info() -> dict:
        """Describe the repository this server is confined to."""
        root = scope.root
        return {
            "root": str(root),
            "basename": root.name,
            "git_repository": (root / ".git").is_dir(),
        }

    @server.tool()
    def read_text_file(path: str, max_bytes: int = DEFAULT_MAX_FILE_BYTES) -> str:
        """Read a UTF-8 text file from the repository (read-only, capped).

        ``path`` is relative to the repository root; traversal outside the
        repository is rejected.
        """
        try:
            return scope.read_text(path, max_bytes=max_bytes)
        except ScopeError as error:
            raise ValueError(str(error)) from error

    @server.tool()
    def list_directory(path: str = ".") -> dict:
        """List the entries of a repository-relative directory (sorted).

        Entries report ``name`` and whether they are a directory or file.
        """
        try:
            return {"path": path, "entries": scope.list_directory(path)}
        except ScopeError as error:
            raise ValueError(str(error)) from error

    @server.tool()
    def search_source(
        pattern: str,
        path_prefix: Optional[str] = None,
        ignore_case: bool = False,
        max_results: int = 100,
    ) -> dict:
        """Search repository text files for a regular expression.

        Returns one match per line (``file``, 1-based ``line``, clipped
        ``snippet``). ``path_prefix`` restricts the walk to a subdirectory.
        Binary, oversized, and artifact files are skipped.
        """
        return search.search_files(
            scope,
            pattern,
            path_prefix=path_prefix,
            ignore_case=ignore_case,
            max_results=max_results,
        )

    @server.tool()
    def find_symbol(
        name: str,
        kind: Optional[str] = None,
        partial: bool = True,
        max_results: int = 100,
    ) -> dict:
        """Find class/function/method definitions in Python sources.

        ``partial=True`` matches symbols containing *name* (default);
        ``partial=False`` requires an exact match. ``kind`` restricts to
        ``class``, ``method``, ``async_method``, ``function``, or
        ``async_function``.
        """
        return symbols.find_symbols(
            scope,
            name,
            kind=kind,
            partial=partial,
            max_results=max_results,
        )

    @server.tool()
    def git_status() -> dict:
        """Return the working-tree status (branch + short-format entries).

        Pure read-only Git inspection; never mutates the repository.
        """
        git = GitInspector(scope.root)
        if not git.is_git_repo():
            raise ValueError(f"not a git repository: {scope.root}")
        try:
            return git.status()
        except GitError as error:
            raise ValueError(str(error)) from error

    @server.tool()
    def git_log(limit: int = 20) -> dict:
        """Return recent commit SHAs and subjects (read-only).

        ``limit`` must be between 1 and 100.
        """
        git = GitInspector(scope.root)
        if not git.is_git_repo():
            raise ValueError(f"not a git repository: {scope.root}")
        try:
            return git.log(limit)
        except GitError as error:
            raise ValueError(str(error)) from error

    @server.tool()
    def git_diff(
        base: Optional[str] = "HEAD",
        path: Optional[str] = None,
        stat_only: bool = False,
    ) -> dict:
        """Show the diff of ``base`` (default HEAD) against the working tree.

        ``path`` restricts the diff to a repository-relative path;
        ``stat_only`` shows change statistics instead of full hunks. Output is
        capped at 512 KiB. Read-only.
        """
        git = GitInspector(scope.root)
        if not git.is_git_repo():
            raise ValueError(f"not a git repository: {scope.root}")
        try:
            return git.diff(base, path=path, stat_only=stat_only)
        except GitError as error:
            raise ValueError(str(error)) from error

    @server.tool()
    def find_tests(path: Optional[str] = None) -> dict:
        """Discover pytest-style test files (``test_*.py`` / ``*_test.py``).

        ``path`` restricts discovery to a repository-relative prefix.
        """
        return test_discovery.find_test_files(scope, path_prefix=path)

    @server.tool()
    def inspect_api() -> dict:
        """Return an overview of the generated SSH Pilot API surface.

        Loads ``docs/api/generated/schema.json`` and reports protocol and
        implementation versions, method counts, model counts, and how each
        client method is implemented (implemented/daemon-only/schema-only).
        """
        return api_surface.inspect_api(scope)

    @server.tool()
    def list_api_methods(surface: str = "all") -> dict:
        """List client and/or daemon API methods with status and capability.

        ``surface`` is ``all`` (default), ``client``, or ``daemon``.
        """
        if surface not in {"all", "client", "daemon"}:
            raise ValueError("surface must be 'all', 'client', or 'daemon'")
        return api_surface.list_methods(scope, surface=surface)

    @server.tool()
    def trace_api_method(method: str) -> dict:
        """Trace an API method end to end.

        Accepts a client method name (e.g. ``open_session``) or a daemon wire
        method name (e.g. ``sessions.open``). Returns the request/result
        signature, the required capability, the client's wire call(s), and the
        daemon dispatch handler for each wire method.
        """
        try:
            return api_surface.inspect_method(scope, method)
        except api_surface.ApiNotFoundError as error:
            raise ValueError(str(error)) from error

    @server.tool()
    def check_api_drift() -> dict:
        """Compare the generated API schema against the source registrations.

        Reports client-contract, daemon-handler, and daemon-capability drift
        between ``docs/api/generated/schema.json`` and the AST-scanned method
        and capability registries.
        """
        return api_surface.check_drift(scope)

    @server.tool()
    def check_frontend_neutrality() -> dict:
        """Verify internal layers (core/api/daemon/mcp) stay GTK/GI-free.

        Read-only AST scan over the current checkout.
        """
        return architecture.check_frontend_neutrality(scope)

    @server.tool()
    def review_public_api() -> dict:
        """Review the public API surface from the generated schema.

        Reports versions, client/daemon method counts, client method status,
        and the models exported by ``sshpilot.api.models``.
        """
        return architecture.review_public_api(scope)

    @server.tool()
    def trace_interaction_scope(method: str = "") -> dict:
        """Trace how interaction methods attach to stable resource scopes.

        ``method`` may name one interaction client method (e.g.
        ``respond_to_interaction``); empty means all interaction methods.
        """
        try:
            return architecture.trace_interaction_scope(scope, method)
        except architecture.ArchitectureError as error:
            raise ValueError(str(error)) from error

    @server.tool()
    def review_commit(base: str = "HEAD") -> dict:
        """Assess the base..working-tree range for architecture impact.

        Classifies changed paths by layer and flags API-surface changes,
        generated-artifact changes, and frontend/core boundary crossings.
        read-only.
        """
        try:
            return architecture.review_commit(scope, base=base)
        except architecture.ArchitectureError as error:
            raise ValueError(str(error)) from error

    @server.tool()
    def run_tests(suite: str) -> dict:
        """Run one selected pytest suite (architecture/api/core/daemon/mcp).

        The suite must be on the allowlist; it runs against the Python that is
        executing this server. Output is capped; timing out is reported, not
        fatal.
        """
        try:
            return execution.run_tests(scope, suite)
        except execution.ExecutionError as error:
            raise ValueError(str(error)) from error

    @server.tool()
    def validate_api_artifacts() -> dict:
        """Verify the generated API artifacts are current (``--check``).

        Runs the artifact generator's check mode only; never regenerates.
        """
        try:
            return execution.check_api_artifacts(scope)
        except execution.ExecutionError as error:
            raise ValueError(str(error)) from error

    @server.tool()
    def run_lint(path: Optional[str] = None) -> dict:
        """Run ``ruff check`` over ``src/`` (default) or an allowed path.

        ``path`` must be repository-relative and under ``src/``, ``tests/``,
        or ``scripts/``.
        """
        try:
            return execution.run_lint(scope, path)
        except execution.ExecutionError as error:
            raise ValueError(str(error)) from error

    return server


def main() -> int:
    """Entry point for the ``sshpilot-mcp-dev`` console script."""
    try:
        from mcp.server.mcpserver import MCPServer  # noqa: F401
    except ImportError:
        print(
            "sshpilot-dev-mcp requires the optional 'mcp' dependency: "
            "pip install 'sshpilot[mcp]'",
            file=sys.stderr,
        )
        return 2
    try:
        server = create_server()
    except ScopeError as error:
        print(f"sshpilot-dev-mcp: {error}", file=sys.stderr)
        return 1
    server.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())