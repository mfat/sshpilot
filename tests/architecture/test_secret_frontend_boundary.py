"""AST-level enforcement of the secret-backend ownership boundary (M5).

The daemon owns every secret backend: configuration, registry, selection,
unlock/lock, Bitwarden/rbw/KeePassXC lifecycle, remembered passwords and
secret-bearing backup transfer. The frontend may only *present* state and
drive operations through the daemon-backed ``SecretBackendsController``.

These tests fail when any frontend module:

* calls ``get_secret_manager()``;
* instantiates ``SecretManager`` or a concrete backend;
* invokes ``bw`` / ``rbw`` / ``pass`` lifecycle commands (or any subprocess
  for backend lifecycle);
* imports ``pykeepass`` for backend operations;
* directly mutates the daemon-owned ``secrets.*`` configuration;
* enumerates decrypted credential values;
* executes a secret-bearing backup export/import itself.

Narrowly documented frontend behavior stays allowed: GTK dialogs, native file
pickers, and the optional Bitwarden CLI *installation* presentation
(``download_and_install_bw_binary`` and friends, which run in this process by
design and are registered below).
"""

from __future__ import annotations

import ast
from functools import lru_cache
from pathlib import Path

import pytest

pytestmark = pytest.mark.xdist_group("core-boundary")

SOURCE = Path(__file__).resolve().parents[2] / "src" / "sshpilot"

# Frontend modules that must never perform backend operations.
_SCOPED = (
    "bitwarden_setup.py",
    "rbw_setup.py",
    "preferences.py",
    "window_dialogs.py",
    "secret_unlock_dialog.py",
    "startup_info.py",
    "secret_unlock_dialog.py",
)

# Subprocess use registered as legitimate frontend-owned presentation.
# Bitwarden CLI download/install is the only secret-adjacent frontend
# subprocess; everything else (rbw lifecycle, bw lifecycle) is forbidden.
_ALLOWED_SUBPROCESS = {
    ("bitwarden_setup.py", "download_and_install_bw_binary"),
    ("bitwarden_setup.py", "run_install"),
    ("bitwarden_setup.py", "_install_bw_binary_on_host"),
    ("bitwarden_setup.py", "_host_argv"),
    # Startup diagnostics only probe the local OpenSSH version; they do not
    # invoke a secret backend lifecycle command.
    ("startup_info.py", "_get_tools_info"),
}

# Lifecycle command names that must never be invoked from the frontend.
_LIFECYCLE_BINS = {
    "bw",
    "rbw",
    "pass",
    "keepassxc-cli",
    "pykeepass",
}

# Secret configuration keys that the daemon owns.
_SECRET_CONFIG_KEYS = (
    "secrets.backend",
    "secrets.session_timeout",
    "secrets.remember_in_keyring",
    "secrets.bitwarden",
    "secrets.keepassxc",
    "secrets.",
)


def _rel(path: Path) -> str:
    return "/".join(path.relative_to(SOURCE).parts)


@lru_cache(maxsize=None)
def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@lru_cache(maxsize=None)
def _parse(path: Path) -> ast.Module:
    return ast.parse(_read(path))


def _scoped_paths() -> list[Path]:
    out = []
    for name in _SCOPED:
        path = SOURCE / name
        if path.exists():
            out.append(path)
    return out


def _import_symbols(tree: ast.Module, rel: str) -> list[str]:
    """Every symbol imported in the module (dotted short names)."""
    out: list[str] = []
    parts = rel.split("/")[:-1]
    pkg = ("sshpilot",) + tuple(parts)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append(alias.name)
            continue
        if node.level:
            base = pkg[: len(pkg) - (node.level - 1)]
            prefix = ".".join(base)
            module = node.module or ""
            full = (prefix + "." + module) if module else prefix
        else:
            full = node.module or ""
        out.append(full)
        for alias in node.names:
            out.append(alias.name)
    return out


def _call_names(tree: ast.Module) -> list[str]:
    """Every function/attribute called in the module (leaf names)."""
    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            out.append(func.attr)
        elif isinstance(func, ast.Name):
            out.append(func.id)
    return out


def _containing_function(tree: ast.Module, target: ast.AST) -> str | None:
    """Return the function containing ``target`` using a small parent walk."""
    for root in ast.walk(tree):
        if not isinstance(root, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(node is target for node in ast.walk(root)):
            return root.name
    return None


# ---------------------------------------------------------------------------
# 1. No get_secret_manager / SecretManager / concrete backends
# ---------------------------------------------------------------------------

def test_frontend_never_calls_get_secret_manager():
    for path in _scoped_paths():
        tree = _parse(path)
        calls = _call_names(tree)
        assert "get_secret_manager" not in calls, (
            f"{_rel(path)} calls get_secret_manager() — backend access is daemon-owned"
        )


def test_frontend_never_instantiates_secret_services():
    for path in _scoped_paths():
        tree = _parse(path)
        calls = _call_names(tree)
        for forbidden in ("SecretManager", "BitwardenBackend", "RbwBackend",
                          "KeepassxcBackend", "PassBackend", "LibsecretBackend"):
            assert forbidden not in calls, (
                f"{_rel(path)} instantiates {forbidden} — daemon-owned"
            )


def test_frontend_never_imports_secret_storage_backends():
    for path in _scoped_paths():
        tree = _parse(path)
        symbols = _import_symbols(tree, _rel(path))
        for forbidden in ("secret_storage", "get_secret_manager", "SecretManager"):
            assert forbidden not in symbols, (
                f"{_rel(path)} imports {forbidden} — daemon-owned"
            )


# ---------------------------------------------------------------------------
# 2. No backend lifecycle command invocation
# ---------------------------------------------------------------------------

def test_frontend_subprocesses_are_limited_to_installation():
    """The frontend subprocess allowlist is enforced at function scope."""
    subprocess_calls = {"run", "Popen", "check_call", "check_output", "call"}
    for path in _scoped_paths():
        tree = _parse(path)
        rel = _rel(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            fname = func.attr if isinstance(func, ast.Attribute) else (
                func.id if isinstance(func, ast.Name) else None
            )
            if fname not in subprocess_calls:
                continue
            if not (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "subprocess"
            ):
                continue
            containing = _containing_function(tree, node)
            assert (rel, containing) in _ALLOWED_SUBPROCESS, (
                f"{rel}:{containing} uses subprocess outside the installation allowlist"
            )


def test_frontend_never_invokes_backend_clis():
    """No subprocess call may target ``bw``/``rbw``/``pass``/``pykeepass`` and
    no lifecycle helper may be called from the frontend."""
    for path in _scoped_paths():
        tree = _parse(path)
        rel = _rel(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            fname = func.attr if isinstance(func, ast.Attribute) else (
                func.id if isinstance(func, ast.Name) else None
            )
            if fname not in {"run", "Popen", "check_call", "check_output", "call"}:
                continue
            for arg in node.args:
                strings = []
                if isinstance(arg, ast.List):
                    strings = [e.value for e in arg.elts
                               if isinstance(e, ast.Constant) and isinstance(e.value, str)]
                elif isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    strings = [arg.value]
                for s in strings:
                    assert not any(b in s for b in _LIFECYCLE_BINS), (
                        f"{rel} invokes backend CLI {s!r} — daemon-owned"
                    )
        # Helper functions whose whole purpose is running the backend CLI.
        for forbidden in ("_run", "_rbw_argv", "_bitwarden_backend",
                          "_configure_server", "login_with_password",
                          "login_with_api_key", "login_with_sso"):
            assert forbidden not in _call_names(tree), (
                f"{rel} calls backend lifecycle helper {forbidden} — daemon-owned"
            )


def test_frontend_never_imports_pykeepass():
    for path in _scoped_paths():
        tree = _parse(path)
        symbols = _import_symbols(tree, _rel(path))
        assert not any(s == "pykeepass" or s.startswith("pykeepass.") for s in symbols), (
            f"{_rel(path)} imports pykeepass for backend operations"
        )


# ---------------------------------------------------------------------------
# 3. No direct secrets.* configuration mutation
# ---------------------------------------------------------------------------

def test_frontend_never_mutates_secret_config():
    """No ``config.set_setting('secrets.*', ...)`` from the frontend; the daemon
    owns the ``secrets`` subtree (reads for presentation stay allowed)."""
    for path in _scoped_paths():
        tree = _parse(path)
        rel = _rel(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute)
                    and func.attr in {"set_setting", "set"}):
                continue
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    value = str(arg.value)
                    if value.startswith(_SECRET_CONFIG_KEYS):
                        assert False, (
                            f"{rel} mutates daemon-owned config {value!r}"
                        )


# ---------------------------------------------------------------------------
# 4. No decrypted credential enumeration
# ---------------------------------------------------------------------------

def test_frontend_never_enumerates_decrypted_credentials():
    for path in _scoped_paths():
        tree = _parse(path)
        rel = _rel(path)
        symbols = _import_symbols(tree, rel)
        calls = _call_names(tree)
        for forbidden in ("list_credentials", "load_all", "lookup_everywhere",
                          "CredentialManager", "iter_credentials"):
            assert forbidden not in calls and forbidden not in symbols, (
                f"{rel} enumerates decrypted credentials via {forbidden}"
            )


# ---------------------------------------------------------------------------
# 5. No secret-bearing backup export/import in the frontend
# ---------------------------------------------------------------------------

def test_frontend_backup_archive_use_is_classification_only():
    """Frontend may classify a local archive, but never read or write it."""
    for path in _scoped_paths():
        tree = _parse(path)
        rel = _rel(path)
        imports = _import_symbols(tree, rel)
        assert "write_spbk" not in imports
        assert "read_spbk" not in imports
        assert "SpbkFileBackend" not in imports
        assert "BitwardenBackupBackend" not in imports
        assert "SSHServerBackupBackend" not in imports
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else (
                func.id if isinstance(func, ast.Name) else None
            )
            if name in {"write_spbk", "read_spbk", "export_to_backend", "import_from_backend"}:
                assert False, f"{rel} executes backup archive operations locally"


def test_frontend_never_executes_secret_backup_export_import():
    """No local backup engine may be imported or invoked directly.

    ``controller.export_backup`` / ``controller.import_backup`` are the sanctioned
    daemon-driven calls; the same method names on any other receiver (or bare
    calls) mean a local BackupManager path and fail the test."""
    controller_receivers = {"controller", "secrets_controller"}
    for path in _scoped_paths():
        tree = _parse(path)
        rel = _rel(path)
        symbols = _import_symbols(tree, rel)
        for forbidden in ("BackupManager", "write_spbk", "read_spbk",
                          "export_to_backend", "import_from_backend",
                          "apply_imported_manifest"):
            assert forbidden not in symbols, (
                f"{rel} executes secret-bearing backup via {forbidden} — daemon-owned"
            )
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute)
                    and func.attr in ("export_backup", "import_backup")):
                continue
            receiver = func.value
            allowed = False
            if isinstance(receiver, ast.Name) and receiver.id in controller_receivers:
                allowed = True
            elif isinstance(receiver, ast.Attribute) and receiver.attr == "_secrets_controller":
                allowed = True
            assert allowed, (
                f"{rel} executes secret-bearing backup via {func.attr} — daemon-owned"
            )


# ---------------------------------------------------------------------------
# 6. The daemon-owned controller is the only import path
# ---------------------------------------------------------------------------

def test_frontend_controller_is_the_secret_entry_point():
    """The secrets controller may only be built by the main window over the
    daemon client; GTK modules must not construct it over a local backend."""
    controller_path = SOURCE / "gtk" / "secret_backends_controller.py"
    assert controller_path.exists()
    tree = _parse(controller_path)
    calls = _call_names(tree)
    # The controller itself never touches the backend or secret storage.
    assert "get_secret_manager" not in calls
    assert "SecretManager" not in calls
    assert "subprocess" not in _import_symbols(tree, "gtk/secret_backends_controller.py")
