"""Plugin discovery and loading.

Two sources, loaded in this order:

1. Built-in plugins: packages under ``sshpilot/plugins/builtin/`` that
   contain a ``plugin.json``. These ship inside the app (Flatpak,
   PyInstaller bundle, wheel) and include "plugin zero": the SSH protocol
   itself. Loaded via normal package imports, so PyInstaller picks them up
   as long as ``sshpilot.plugins.builtin.*`` is collected (add
   ``collect_submodules('sshpilot.plugins.builtin')`` to the .spec).

2. User plugins: directories under the per-user plugin dir
   (``$XDG_DATA_HOME/sshpilot/plugins`` — inside Flatpak this resolves to
   ``~/.var/app/io.github.mfat.sshpilot/data/sshpilot/plugins``), each with
   a ``plugin.json`` and an ``__init__.py``. Loaded from file paths, so
   they work identically under Flatpak, Homebrew, and the PPA.

plugin.json (kept JSON, not TOML, because requires-python is >=3.9 and
tomllib is 3.11+)::

    {
      "id": "ssh",
      "name": "SSH protocol",
      "api_version": 1,
      "entry": "plugin",          // attribute or module exposing the class
      "builtin": true
    }

The entry module must expose a ``Plugin`` class subclassing
``SshPilotPlugin``.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Optional, Set, Tuple

from sshpilot.core.plugins import EventBus

from .api import API_VERSION, PluginContext, SshPilotPlugin
from .registry import protocol_registry

logger = logging.getLogger(__name__)

BUILTIN_PACKAGE = __package__ + ".builtin"


class _NullUiHost:
    """Accept and discard UI registrations in a process that has no UI."""

    def register_page(self, *_args, **_kwargs) -> None:
        pass

    def open_page(self, *_args, **_kwargs) -> None:
        pass

    def notify(self, *_args, **_kwargs) -> None:
        pass


class _HeadlessHost:
    """A PluginHost stand-in for processes with no window (the daemon).

    Protocol plugins commonly register a page alongside their backend. Handing
    such a plugin a context whose ``ui``/``events`` are ``None`` would abort
    ``activate()`` on the first UI call — before ``register_protocol`` ran —
    so the protocol the daemon came here for would never appear. Accept those
    registrations and drop them instead; nothing in this process can show them.

    ``daemon_client()`` stays ``None``: this *is* the daemon, and a plugin
    reaching back into the client API from here would be a loop, so
    daemon-backed context calls raise exactly as they do in the frontend
    before the client attaches."""

    def __init__(self) -> None:
        self.events = EventBus()
        self.ui = _NullUiHost()

    def daemon_client(self) -> None:
        return None


@dataclass
class LoadedPlugin:
    plugin_id: str
    name: str
    instance: SshPilotPlugin
    builtin: bool
    path: str


@dataclass
class PluginInfo:
    """Manifest-level description of a plugin on disk (no code imported).

    Used by the preferences page, which must also list plugins that are
    disabled or not yet enabled — importing them there would defeat the
    opt-in security posture for user plugins."""
    plugin_id: str
    name: str
    builtin: bool
    required: bool
    path: str
    api_compatible: bool
    api_version: Optional[int] = None
    permissions: List[str] = field(default_factory=list)
    version: Optional[str] = None  # plugin's own version (drives update checks)
    homepage: Optional[str] = None  # source/homepage URL (shown in the info dialog)


def _user_plugin_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share")
    return Path(base) / "sshpilot" / "plugins"


def _read_manifest(directory: Path) -> Optional[dict]:
    manifest = directory / "plugin.json"
    if not manifest.is_file():
        return None
    try:
        with open(manifest, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        logger.error("Invalid plugin manifest %s: %s", manifest, exc)
        return None


def _api_compatible(meta: dict) -> bool:
    declared = meta.get("api_version")
    if declared != API_VERSION[0]:
        logger.warning(
            "Plugin %r targets API v%s, app provides v%s; skipping",
            meta.get("id"), declared, API_VERSION[0])
        return False
    return True


def _instantiate(module, meta: dict) -> Optional[SshPilotPlugin]:
    cls = getattr(module, "Plugin", None)
    if cls is None or not issubclass(cls, SshPilotPlugin):
        logger.error("Plugin %r: entry module has no SshPilotPlugin "
                     "subclass named 'Plugin'", meta.get("id"))
        return None
    return cls()


def _id_set(app_config, key: str) -> frozenset:
    """The plugin-id list stored at ``key``; empty when it can't be read."""
    if app_config is None:
        return frozenset()
    try:
        return frozenset(app_config.get_setting(key, []) or [])
    except Exception:
        return frozenset()


def _load_builtin(make_ctx,
                  disabled: frozenset) -> List[LoadedPlugin]:
    loaded: List[LoadedPlugin] = []
    try:
        builtin_pkg = importlib.import_module(BUILTIN_PACKAGE)
    except ImportError:
        logger.error("Built-in plugin package missing")
        return loaded
    pkg_dir = Path(next(iter(builtin_pkg.__path__)))
    for child in sorted(pkg_dir.iterdir()):
        meta = _read_manifest(child) if child.is_dir() else None
        if not meta or not _api_compatible(meta):
            continue
        pid = meta["id"]
        if pid in disabled and not meta.get("required", False):
            logger.info("Built-in plugin %r disabled by user", pid)
            continue
        try:
            module = importlib.import_module(
                f"{BUILTIN_PACKAGE}.{child.name}")
            instance = _instantiate(module, meta)
            if instance is None:
                continue
            instance.activate(make_ctx(pid))
            loaded.append(LoadedPlugin(pid, meta.get("name", pid),
                                       instance, True, str(child)))
        except Exception:
            logger.exception("Failed to load built-in plugin %r", pid)
    return loaded


def _declared_protocols(meta: dict) -> List[str]:
    """Protocol ids the manifest claims, if it bothers to say (optional)."""
    declared = meta.get("protocols")
    if not isinstance(declared, list):
        return []
    return [p for p in declared if isinstance(p, str) and p]


def _user_plugin_candidates(enabled: frozenset,
                            wanted_protocol: Optional[str]) -> List[Tuple[Path, dict]]:
    """Enabled user plugin directories, best candidate first.

    When a specific protocol is wanted, a manifest that declares ``protocols``
    is taken at its word: it goes first if it claims the protocol and is
    dropped entirely if it claims others instead. That keeps a headless
    process from importing UI plugins it has no use for. Manifests that
    declare nothing stay in the list — the field is optional, so silence
    cannot mean "owns no protocols"."""
    declared_match: List[Tuple[Path, dict]] = []
    undeclared: List[Tuple[Path, dict]] = []
    root = _user_plugin_dir()
    if not root.is_dir():
        return []
    for child in sorted(root.iterdir()):
        meta = _read_manifest(child) if child.is_dir() else None
        if not meta or not _api_compatible(meta):
            continue
        pid = meta.get("id")
        if pid not in enabled:
            logger.debug("User plugin %r present but not enabled", pid)
            continue
        declared = _declared_protocols(meta)
        if wanted_protocol is None or not declared:
            undeclared.append((child, meta))
        elif wanted_protocol in declared:
            declared_match.append((child, meta))
        else:
            logger.debug("User plugin %r declares %r, not %r; not importing it",
                         pid, declared, wanted_protocol)
    return declared_match + undeclared


def _load_user(make_ctx,
               enabled: frozenset,
               *,
               wanted_protocol: Optional[str] = None,
               stop_when: Optional[Callable[[], bool]] = None) -> List[LoadedPlugin]:
    """User plugins are opt-in: only ids the user enabled in preferences
    are imported at all (arbitrary code execution should be a deliberate
    choice, not a consequence of a file existing on disk).

    ``wanted_protocol``/``stop_when`` narrow the sweep for the daemon, which
    wants one protocol backend rather than every plugin: candidates that
    disclaim the protocol are skipped, and the sweep ends the moment
    ``stop_when()`` reports the protocol resolved."""
    loaded: List[LoadedPlugin] = []
    for child, meta in _user_plugin_candidates(enabled, wanted_protocol):
        pid = meta["id"]
        init_py = child / "__init__.py"
        if not init_py.is_file():
            logger.error("User plugin %r has no __init__.py", pid)
            continue
        try:
            module_name = f"sshpilot_user_plugin_{pid}"
            spec = importlib.util.spec_from_file_location(
                module_name, init_py,
                submodule_search_locations=[str(child)])
            module = importlib.util.module_from_spec(spec)
            # Register before exec: the standard importlib pattern. Without it,
            # anything that looks the module up by name during import fails —
            # e.g. @dataclass on Python 3.14 resolves annotations via
            # sys.modules[cls.__module__], which would otherwise be missing.
            sys.modules[module_name] = module
            try:
                spec.loader.exec_module(module)  # type: ignore[union-attr]
            except Exception:
                sys.modules.pop(module_name, None)
                raise
            instance = _instantiate(module, meta)
            if instance is None:
                continue
            instance.activate(make_ctx(pid))
            loaded.append(LoadedPlugin(pid, meta.get("name", pid),
                                       instance, False, str(child)))
        except Exception:
            logger.exception("Failed to load user plugin %r", pid)
        if stop_when is not None and stop_when():
            break
    return loaded


def load_plugins(*, app_config, connection_manager,
                 plugin_host=None) -> List[LoadedPlugin]:
    """Call once at startup, after ConnectionManager exists but before the
    main window spawns any terminals. Returns descriptors for the Plugins
    preferences page. Never raises: a broken plugin must not take the app
    down.

    ``plugin_host`` is the process-wide PluginHost (event bus + UI host); it
    may be None in headless contexts (the protocol backends don't need it)."""
    registry = protocol_registry()

    def make_ctx(plugin_id: str) -> PluginContext:
        # One context per plugin so ctx.plugin_id is known and the scoped
        # secrets/settings/events/ui facades work without the plugin passing
        # its id around.
        return PluginContext(plugin_id=plugin_id,
                             app_config=app_config,
                             connection_manager=connection_manager,
                             protocol_registry=registry,
                             host=plugin_host)

    loaded = _load_builtin(make_ctx, disabled=_id_set(app_config, "plugins.disabled"))
    loaded += _load_user(make_ctx, enabled=_id_set(app_config, "plugins.enabled"))
    global _builtins_ensured_for
    _builtins_ensured_for = id(registry)

    if protocol_registry().get_or_none("ssh") is None:
        # Plugin zero failed — the app is useless without it, so this is
        # the one case worth being loud about.
        raise RuntimeError("SSH protocol plugin failed to load; "
                           "see log for details")
    return loaded


_builtins_ensured_for: Optional[int] = None
# User plugin ids already activated headlessly in this process, scoped to the
# registry they were registered into so a replaced registry starts clean. The
# registry itself is held rather than its id: a dead object's id can be reused,
# which would silently pass this identity check for an unrelated registry.
_headless_user_plugins_for: Any = None
_headless_user_plugin_ids: Set[str] = set()


def _headless_user_plugins(registry) -> Set[str]:
    global _headless_user_plugins_for, _headless_user_plugin_ids
    if _headless_user_plugins_for is not registry:
        _headless_user_plugins_for = registry
        _headless_user_plugin_ids = set()
    return _headless_user_plugin_ids


def ensure_builtin_protocols(*, app_config=None) -> None:
    """Register built-in protocol backends in this process if needed.

    The daemon launch path owns non-SSH terminal spawns but does not run the
    GTK PluginHost. Calling this is idempotent and safe from a headless
    process: it only activates built-ins (honouring ``plugins.disabled``).
    """
    global _builtins_ensured_for
    registry = protocol_registry()
    if _builtins_ensured_for is id(registry) and registry.get_or_none("ssh") is not None:
        return
    _load_builtin(make_ctx=_headless_ctx_factory(app_config, registry),
                  disabled=_id_set(app_config, "plugins.disabled"))
    _builtins_ensured_for = id(registry)


def _headless_ctx_factory(app_config, registry):
    """Per-plugin context builder for a process with no window."""
    host = _HeadlessHost()

    def make_ctx(plugin_id: str) -> PluginContext:
        return PluginContext(
            plugin_id=plugin_id,
            app_config=app_config,
            connection_manager=None,
            protocol_registry=registry,
            host=host,
        )

    return make_ctx


def ensure_user_protocols(*, app_config=None, protocol: Optional[str] = None) -> None:
    """Activate enabled user plugins in this process until ``protocol`` resolves.

    A protocol registered by a user plugin lives in whichever process ran
    ``activate()``. The GTK process does that at startup, but the daemon owns
    session launch and runs separately, so it cannot see those backends and
    refuses the launch as an unsupported protocol.

    Deliberately *not* called up front: the daemon reaches for it only after
    the built-ins fail to resolve a protocol, so an ordinary SSH or telnet
    session never imports third-party code. Plugins stay opt-in by id exactly
    as in the frontend — a file on disk still runs nothing — and each is
    activated at most once per process.
    """
    registry = protocol_registry()
    already = _headless_user_plugins(registry)
    enabled = _id_set(app_config, "plugins.enabled") - already
    if not enabled:
        return

    def resolved() -> bool:
        return protocol is not None and registry.get_or_none(protocol) is not None

    loaded = _load_user(_headless_ctx_factory(app_config, registry),
                        enabled=enabled,
                        wanted_protocol=protocol,
                        stop_when=resolved)
    already.update(plugin.plugin_id for plugin in loaded)


def _builtin_plugin_dir() -> Optional[Path]:
    try:
        builtin_pkg = importlib.import_module(BUILTIN_PACKAGE)
    except ImportError:
        return None
    return Path(next(iter(builtin_pkg.__path__)))


def discover_plugins() -> List[PluginInfo]:
    """Scan built-in and user plugin directories for manifests.

    Pure metadata: never imports plugin code, so it is safe to call for
    plugins that are disabled or not enabled."""
    infos: List[PluginInfo] = []

    def _scan(root: Optional[Path], builtin: bool) -> None:
        if root is None or not root.is_dir():
            return
        for child in sorted(root.iterdir()):
            meta = _read_manifest(child) if child.is_dir() else None
            if not meta or not meta.get("id"):
                continue
            declared = meta.get("api_version")
            infos.append(PluginInfo(
                plugin_id=meta["id"],
                name=meta.get("name", meta["id"]),
                builtin=builtin,
                required=bool(meta.get("required", False)),
                path=str(child),
                api_compatible=declared == API_VERSION[0],
                api_version=declared,
                permissions=[str(p) for p in (meta.get("permissions") or [])
                             if isinstance(p, str)],
                version=(str(meta["version"]) if meta.get("version") else None),
                homepage=(str(meta["homepage"]) if meta.get("homepage") else None),
            ))

    _scan(_builtin_plugin_dir(), builtin=True)
    _scan(_user_plugin_dir(), builtin=False)
    return infos
