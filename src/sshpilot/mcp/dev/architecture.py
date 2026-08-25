"""Architecture intelligence for the dev MCP server.

Read-only, AST-based checks over the current checkout:

* frontend-neutrality (core/api/daemon/mcp stay GTK/GI-free, and dev MCP stays
  inside its import allowlist),
* public API review (generated schema + AST-scanned exports),
* interaction-scope tracing (how interaction methods attach to stable resource
  scopes: session / SFTP service / operation),
* commit review and validation planning (which layers a commit touches, which
  focused tests apply, and whether API/generated contracts are still clean).

All checks are read-only and never mutate the repository.
"""

from __future__ import annotations

import ast
from typing import Dict, Iterable, List, Set, Tuple

from .._scope import RepoScope, ScopeError

#: Packages that must stay GTK/GI-free (platform adapters legitimately use GI).
GTK_FREE_PACKAGES = ("core", "api", "daemon", "mcp")
#: Frontend (GTK/user-facing) top-level packages under ``src/sshpilot``.
FRONTEND_PACKAGES = ("gtk", "web", "cli")
#: GI/GTK import markers that must never appear in internal packages.
GTK_FORBIDDEN = ("gi", "gi.repository", "Gtk", "GLib", "GObject")

#: Namespace prefixes for layer classification of changed paths.
_LAYER_PREFIXES = (
    ("gtk", "frontend"),
    ("web", "frontend"),
    ("cli", "frontend"),
    ("core", "core"),
    ("daemon", "daemon"),
    ("api", "api"),
    ("mcp", "mcp"),
    ("platform", "platform"),
    ("tests", "tests"),
    ("docs/api/generated", "generated-artifacts"),
)

_GIT_SPECIAL_HEADERS = frozenset(
    {"index", "---", "+++", "@@", "diff", "similarity", "rename", "new", "deleted", "copied"}
)


class ArchitectureError(RuntimeError):
    """Raised for invalid architecture-intelligence input."""


def _python_files(scope: RepoScope, prefixes: Iterable[str]) -> Iterable[Tuple[str, str]]:
    """Yield (relative path, source text) for Python files under *prefixes*."""
    targets = tuple(
        f"src/sshpilot/{prefix}/" for prefix in prefixes
    )
    for relative in scope.iter_files(include_suffixes={".py"}):
        text = str(relative)
        if not any(text.startswith(target) for target in targets):
            continue
        try:
            source = scope.read_text(text, max_bytes=2 * 1024 * 1024)
        except ScopeError:
            continue
        yield text, source


def check_frontend_neutrality(scope: RepoScope) -> dict:
    """Verify internal layers stay GTK/GI-free and dev MCP stays constrained.

    Scans ``core``, ``api``, ``daemon``, ``mcp`` for GTK/GI imports (the MCP
    boundary test enforces the full import allowlist; this tool reports the
    quick GTK/GI scan over every internal layer).
    """
    violations: List[dict] = []
    for path, source in _python_files(scope, GTK_FREE_PACKAGES):
        try:
            tree = ast.parse(source, filename=path)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            imported = set()
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
            for name in imported:
                if name in GTK_FORBIDDEN or name.startswith(("gi.", "Gtk", "GLib")):
                    violations.append({"file": path, "import": name})
    return {
        "check": "frontend-neutrality",
        "clean": not violations,
        "violations": violations,
        "scanned_packages": sorted(GTK_FREE_PACKAGES),
    }


def review_public_api(scope: RepoScope) -> dict:
    """Review the public API surface from the generated schema and exports.

    Reports versions, client/daemon method counts by status/capability, and
    which public model classes are exported by ``sshpilot.api.models`` but
    never reached by a client or daemon contract (candidates for dead public
    surface).
    """
    try:
        from . import api_surface
    except ImportError:  # pragma: no cover - protection for odd installs
        raise ArchitectureError("api_surface unavailable") from None

    schema = api_surface.load_schema(scope)
    client_contract = schema["client_method_contract"]
    daemon_contract = schema["daemon_method_contract"]

    used_models: Set[str] = set()
    for contract in (client_contract, daemon_contract):
        for entry in contract.values():
            capability = entry.get("capability")
            if isinstance(capability, str):
                used_models.add(capability)

    # ``api.models`` re-exports its public surface through ``__all__``.  Scan
    # the model modules for class definitions, then intersect with that
    # export list.  Scanning every top-level AST definition here also picks up
    # validators and conversion helpers, which are not models.
    exported_names: Set[str] = set()
    model_classes: Set[str] = set()
    for relative in scope.iter_files(include_suffixes={".py"}):
        text = str(relative)
        if not text.startswith("src/sshpilot/api/models/"):
            continue
        try:
            source = scope.read_text(text, max_bytes=2 * 1024 * 1024)
        except ScopeError:
            continue
        tree = ast.parse(source, filename=text)
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                model_classes.add(node.name)
            if text.endswith("/__init__.py"):
                for statement in tree.body:
                    if not isinstance(statement, ast.Assign):
                        continue
                    if not any(
                        isinstance(target, ast.Name) and target.id == "__all__"
                        for target in statement.targets
                    ):
                        continue
                    if isinstance(statement.value, (ast.List, ast.Tuple)):
                        exported_names.update(
                            element.value
                            for element in statement.value.elts
                            if isinstance(element, ast.Constant)
                            and isinstance(element.value, str)
                        )

    exported = sorted(exported_names & model_classes)

    status_counts: Dict[str, int] = {}
    for entry in client_contract.values():
        status_counts[entry["status"]] = status_counts.get(entry["status"], 0) + 1

    return {
        "format": schema.get("format"),
        "protocol_version": schema["protocol_version"],
        "api_implementation_version": schema["api_implementation_version"],
        "client_methods": len(client_contract),
        "daemon_methods": len(daemon_contract),
        "client_status": status_counts,
        "models_defined": len(exported),
        "models": exported,
        "model_citations": len(used_models),
    }


#: Interaction-management client methods and the scope field that ties each to
#: a stable resource (session / SFTP service / operation) per the SSH Pilot
#: interaction rule.
INTERACTION_METHODS = (
    "list_interactions",
    "get_interaction",
    "claim_interaction",
    "release_interaction",
    "respond_to_interaction",
    "cancel_interaction",
)


def trace_interaction_scope(scope: RepoScope, method: str = "") -> dict:
    """Trace how an interaction-management method attaches to a resource scope.

    ``method`` accepts an interaction client method name (``claim_interaction``,
    ``respond_to_interaction``, ...). With no ``method``, summaries every
    interaction method and the capability it requires.
    """
    try:
        from . import api_surface
    except ImportError:  # pragma: no cover
        raise ArchitectureError("api_surface unavailable") from None

    if method and method not in INTERACTION_METHODS:
        raise ArchitectureError(
            f"{method!r} is not an interaction method; expected one of "
            + ", ".join(INTERACTION_METHODS)
        )
    selected = (method,) if method else INTERACTION_METHODS
    traced: List[dict] = []
    for name in selected:
        detail = api_surface.inspect_method(scope, name)
        # Derive the stable public scope from the interaction model attributes
        # (session_id / sftp service id / operation id are the documented scopes).
        signature = detail.get("signature", {})
        params = {p["name"] for p in signature.get("parameters", [])}
        scope_kind = _infer_interaction_scope_kind(params)
        traced.append(
            {
                "method": name,
                "requirement": detail.get("requirement"),
                "status": detail.get("status"),
                "scope_kind": scope_kind,
                "wire_methods": [w["method"] for w in detail.get("wire_methods", [])],
            }
        )
    return {
        "check": "interaction-scope",
        "total": len(traced),
        "methods": traced,
    }


def _infer_interaction_scope_kind(params: Set[str]) -> str:
    """Best-effort resource scope inferred from the request parameter names."""
    if "session_id" in params:
        return "session"
    if "service_id" in params:
        return "sftp"
    if "operation_id" in params:
        return "operation"
    return "connection"


#: Subsystems and their canonical layer label for commit classification.
def review_commit(scope: RepoScope, base: str = "HEAD") -> dict:
    """Assess a commit/range for architecture impact.

    Uses the read-only git helper on ``base``..working tree (default ``HEAD``)
    and classifies every changed path by layer. Flags API surface changes,
    generated-artifact changes, frontend/core boundary crossings, and whether a
    docs/API-code drift check is warranted.
    """
    from ._git import GitError, GitInspector

    git = GitInspector(scope.root)
    if not git.is_git_repo():
        raise ArchitectureError(f"not a git repository: {scope.root}")
    try:
        status = git.status()
        diff = git.diff(base=base, stat_only=True)
    except GitError as error:
        raise ArchitectureError(str(error)) from error
    changed = [entry["path"] for entry in status["entries"]]
    classifications: Dict[str, List[str]] = {}
    for path in changed:
        layer = _classify_path(path)
        classifications.setdefault(layer, []).append(path)

    api_changed = bool(classifications.get("api"))
    artifacts_changed = bool(classifications.get("generated-artifacts"))
    frontend_changed = bool(classifications.get("frontend"))
    core_changed = bool(classifications.get("core"))
    boundary_crossed = frontend_changed and (core_changed or api_changed)

    return {
        "check": "commit-review",
        "base": base,
        "changed_total": len(changed),
        "by_layer": classifications,
        "boundary_crossed": boundary_crossed,
        "api_surface_changed": api_changed,
        "generated_artifacts_changed": artifacts_changed,
        "diff_stat": diff.get("diff", ""),
        "recommend_api_drift_check": api_changed or artifacts_changed,
    }


def plan_api_change(scope: RepoScope, method: str) -> dict:
    """Return the authoritative surfaces to inspect when changing an API method.

    This complements generic code graphs: it starts from the generated public
    contract, then names the SSH Pilot-specific source, capability, handler,
    generated-artifact, documentation, and headless-test obligations.
    """
    from . import api_surface

    try:
        trace = api_surface.inspect_method(scope, method)
    except api_surface.ApiNotFoundError as error:
        raise ArchitectureError(str(error)) from error

    checklist = [
        {
            "surface": "typed-api",
            "paths": ["src/sshpilot/api/"],
            "reason": "Update the public request/result/event/error models and client contract.",
        },
        {
            "surface": "daemon-dispatch",
            "paths": ["src/sshpilot/daemon/"],
            "reason": "Keep authoritative state and process ownership in the daemon.",
        },
        {
            "surface": "capabilities",
            "paths": ["src/sshpilot/api/capabilities.py"],
            "reason": "Advertise availability only when the full implementation exists.",
        },
        {
            "surface": "contracts-and-docs",
            "paths": [
                "tests/api/",
                "tests/architecture/",
                "docs/api/",
                "docs/api/generated/",
            ],
            "reason": "Update contract tests, generated artifacts, and the API changelog when applicable.",
        },
    ]
    return {
        "check": "api-change-plan",
        "method": method,
        "trace": trace,
        "checklist": checklist,
        "validation": [
            "check_api_drift",
            "validate_api_artifacts",
            "check_frontend_neutrality",
            "run_tests(suite='api')",
            "run_tests(suite='architecture')",
        ],
        "wire_protocol_note": (
            "Do not bump the wire protocol unless compatibility actually changes."
        ),
    }


def recommend_tests(scope: RepoScope, paths: List[str]) -> dict:
    """Recommend focused headless checks for repository-relative changed paths."""
    if not paths:
        raise ArchitectureError("paths must contain at least one repository-relative path")

    normalized: List[str] = []
    for path in paths:
        if not isinstance(path, str) or not path.strip():
            raise ArchitectureError("every path must be a non-empty string")
        candidate = path.replace("\\", "/")
        if candidate.startswith("./"):
            candidate = candidate[2:]
        drive_absolute = len(candidate) >= 2 and candidate[1] == ":"
        if (
            not candidate
            or candidate == "."
            or candidate.startswith("/")
            or drive_absolute
            or ".." in candidate.split("/")
        ):
            raise ArchitectureError(f"path must stay inside the repository: {path!r}")
        try:
            scope.resolve(candidate)
        except ScopeError as error:
            raise ArchitectureError(str(error)) from error
        if candidate not in normalized:
            normalized.append(candidate)

    layers = {_classify_path(path) for path in normalized}
    suites: Set[str] = set()
    checks: Set[str] = {"run_lint"}
    reasons: List[str] = []

    if "architecture" in layers or layers & {"api", "core", "daemon", "frontend", "mcp"}:
        suites.add("architecture")
        checks.add("check_frontend_neutrality")
        reasons.append("Internal or frontend boundaries changed.")
    if "api" in layers or "generated-artifacts" in layers:
        suites.add("api")
        checks.update({"check_api_drift", "validate_api_artifacts"})
        reasons.append("The public API contract or generated API artifacts changed.")
    if "core" in layers:
        suites.add("core")
        reasons.append("Core application behavior changed.")
    if "daemon" in layers:
        suites.add("daemon")
        reasons.append("Authoritative daemon behavior changed.")
    if "mcp" in layers:
        suites.add("mcp")
        reasons.append("An MCP server or trust boundary changed.")
    for suite in ("architecture", "api", "core", "daemon", "mcp", "cli", "integration"):
        if any(path == f"tests/{suite}" or path.startswith(f"tests/{suite}/") for path in normalized):
            suites.add(suite)
            reasons.append(f"The {suite} test suite itself changed.")

    focused_tests: Set[str] = set()
    source_stems: Set[str] = set()
    for path in normalized:
        if not path.endswith(".py") or path.startswith("tests/"):
            continue
        parts = path.split("/")
        stem = parts[-1][:-3]
        source_stems.add(stem)
        if len(parts) >= 2:
            source_stems.add(f"{parts[-2]}_{stem}")
    for path in normalized:
        if path.startswith("tests/") and path.endswith(".py"):
            try:
                if scope.resolve(path).is_file():
                    focused_tests.add(path)
            except ScopeError:
                pass
    if source_stems:
        for relative in scope.iter_files(include_suffixes={".py"}):
            text = str(relative)
            basename = text.rsplit("/", 1)[-1]
            if text.startswith("tests/") and any(
                basename.startswith(f"test_{stem}") for stem in source_stems
            ):
                focused_tests.add(text)

    return {
        "check": "test-recommendations",
        "paths": normalized,
        "layers": sorted(layers),
        "focused_tests": sorted(focused_tests),
        "suites": sorted(suites),
        "checks": sorted(checks),
        "reasons": reasons,
        "commands": [
            *[f"python -m pytest -q {path}" for path in sorted(focused_tests)],
            *[f"python -m pytest -q tests/{suite}" for suite in sorted(suites)],
        ],
    }


def validate_change(scope: RepoScope, base: str = "HEAD") -> dict:
    """Run safe static/contract checks and recommend tests for current changes.

    This intentionally does not run test suites automatically. It performs the
    fast architecture checks, executes the existing generated-artifact check
    when API surfaces changed, and returns explicit focused test commands.
    """
    from . import api_surface, execution

    review = review_commit(scope, base=base)
    changed_paths = sorted(
        path for paths in review["by_layer"].values() for path in paths
    )
    recommendations = (
        recommend_tests(scope, changed_paths)
        if changed_paths
        else {
            "check": "test-recommendations",
            "paths": [],
            "layers": [],
            "focused_tests": [],
            "suites": [],
            "checks": [],
            "reasons": ["No working-tree changes detected."],
            "commands": [],
        }
    )
    neutrality = check_frontend_neutrality(scope)
    api_drift = None
    artifacts = None
    clean = neutrality["clean"]
    if review["recommend_api_drift_check"]:
        api_drift = api_surface.check_drift(scope)
        artifacts = execution.check_api_artifacts(scope)
        clean = clean and api_drift["clean"] and artifacts["success"]

    return {
        "check": "change-validation",
        "base": base,
        "clean": clean,
        "review": review,
        "frontend_neutrality": neutrality,
        "api_drift": api_drift,
        "api_artifacts": artifacts,
        "recommended_tests": recommendations,
    }


def _classify_path(path: str) -> str:
    for prefix, layer in _LAYER_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return layer
        if path.startswith(f"src/sshpilot/{prefix}/"):
            return layer
    if path.startswith("src/sshpilot/"):
        return "application"
    if path.startswith("scripts/"):
        return "scripts"
    if path.startswith("docs/"):
        return "docs"
    return "other"
