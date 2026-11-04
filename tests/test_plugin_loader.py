"""Tests for plugin discovery and loading (sshpilot.plugins.loader)."""

import json
import os
import sys
import textwrap

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot.plugins import loader as loader_mod
from sshpilot.plugins import registry as registry_mod
from sshpilot.plugins.loader import discover_plugins, load_plugins


class FakeConfig:
    def __init__(self, settings=None):
        self._settings = dict(settings or {})

    def get_setting(self, key, default=None):
        return self._settings.get(key, default)


@pytest.fixture(autouse=True)
def fresh_registry(monkeypatch, tmp_path):
    monkeypatch.setattr(registry_mod, "_registry", None)
    # Point the user plugin dir at an (initially empty) temp dir.
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))


def _write_user_plugin(tmp_path, plugin_id, api_version=1, body=None):
    plugin_dir = tmp_path / "xdg-data" / "sshpilot" / "plugins" / plugin_id
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(json.dumps({
        "id": plugin_id,
        "name": f"{plugin_id} plugin",
        "api_version": api_version,
    }))
    (plugin_dir / "__init__.py").write_text(body or textwrap.dedent(f"""
        from sshpilot.plugins.api import (
            ProtocolBackend, SpawnSpec, SshPilotPlugin,
        )

        class _Backend(ProtocolBackend):
            protocol_id = "{plugin_id}"
            display_name = "{plugin_id}"

            def capabilities(self):
                return frozenset()

            def build_spawn(self, connection, ctx):
                return SpawnSpec(argv=["true"])

        class Plugin(SshPilotPlugin):
            def activate(self, ctx):
                ctx.register_protocol(_Backend())
    """))
    return plugin_dir


def test_builtin_ssh_plugin_loads():
    loaded = load_plugins(app_config=FakeConfig(), connection_manager=None)
    assert any(p.plugin_id == "ssh" and p.builtin for p in loaded)
    assert registry_mod.protocol_registry().get_or_none("ssh") is not None


def test_required_builtin_ignores_disabled_list():
    cfg = FakeConfig({"plugins.disabled": ["ssh"]})
    loaded = load_plugins(app_config=cfg, connection_manager=None)
    assert any(p.plugin_id == "ssh" for p in loaded)


def test_load_plugins_raises_without_ssh_backend(monkeypatch):
    monkeypatch.setattr(loader_mod, "_load_builtin", lambda ctx, disabled: [])
    with pytest.raises(RuntimeError):
        load_plugins(app_config=FakeConfig(), connection_manager=None)


def test_user_plugin_is_opt_in(tmp_path):
    _write_user_plugin(tmp_path, "dummy")

    loaded = load_plugins(app_config=FakeConfig(), connection_manager=None)
    assert not any(p.plugin_id == "dummy" for p in loaded)
    assert registry_mod.protocol_registry().get_or_none("dummy") is None

    cfg = FakeConfig({"plugins.enabled": ["dummy"]})
    loaded = load_plugins(app_config=cfg, connection_manager=None)
    assert any(p.plugin_id == "dummy" and not p.builtin for p in loaded)
    assert registry_mod.protocol_registry().get_or_none("dummy") is not None


def test_api_version_mismatch_skipped(tmp_path):
    _write_user_plugin(tmp_path, "futuristic", api_version=99)
    cfg = FakeConfig({"plugins.enabled": ["futuristic"]})
    loaded = load_plugins(app_config=cfg, connection_manager=None)
    assert not any(p.plugin_id == "futuristic" for p in loaded)


def test_broken_user_plugin_does_not_break_loading(tmp_path):
    _write_user_plugin(tmp_path, "broken", body="raise RuntimeError('boom')\n")
    cfg = FakeConfig({"plugins.enabled": ["broken"]})
    loaded = load_plugins(app_config=cfg, connection_manager=None)
    assert any(p.plugin_id == "ssh" for p in loaded)
    assert not any(p.plugin_id == "broken" for p in loaded)


def _settings_client(stored):
    """Daemon client double that answers plugin settings from ``stored``."""
    import types

    return types.SimpleNamespace(
        get_plugin_setting=lambda _plugin_id, key, default=None: stored.get(key, default),
        set_plugin_setting=lambda _plugin_id, key, value: stored.__setitem__(key, value),
    )


class _BoundWindow:
    """Enough of a window for ``daemon_client()`` and page registration.

    ``lookup_action`` reports every action as already installed so page
    registration does not construct a ``Gio.SimpleAction``."""

    def __init__(self, client):
        self.client = client

    def lookup_action(self, _name):
        return object()

    def remove_action(self, _name):
        return None

    def get_application(self):
        return None


def _present(host, client):
    """The two real startup signals, in the order the window uses them.

    ``notify_backend_settled`` can run before the window is bound (the client
    was already selected) or after ``dispatch_app_started`` (selection finishes
    once the window is on screen). Callers bind the window themselves when
    they want ``daemon_client()`` to succeed."""
    host._window = _BoundWindow(client)
    host.dispatch_app_started()
    host.notify_backend_settled()


def test_user_plugin_that_reads_settings_in_activate_loads_once_backend_is_up(tmp_path):
    """Issue #1342: health, inventory-import, and tailscale call
    ``ctx.settings.get`` inside ``activate()``.

    Plugin loading runs while the daemon client is still being selected, so
    that read raises ``BackendUnavailable`` and the loader used to abandon
    the plugin. The same read succeeds once the client is attached, which
    happens before ``app_started``. The plugin must be loaded by then, with
    the persisted value rather than the caller's default, and an
    ``app_started`` subscription made during that activation must run once.
    """
    from sshpilot.plugins.host import PluginHost

    body = textwrap.dedent("""
        from sshpilot.plugins.api import Events, SshPilotPlugin

        class Plugin(SshPilotPlugin):
            def activate(self, ctx):
                self.ctx = ctx
                # Registered before the read, as a plugin might do, so a
                # retry must not deliver app_started twice.
                ctx.ui.register_page(
                    "health", "Health", "network-transmit-receive-symbolic",
                    lambda: None)
                ctx.events.subscribe(Events.APP_STARTED, self._on_started)
                self.timeout = ctx.settings.get("timeout", 3.0)
                self.format = ctx.settings.get("format", "plain")
                self.default_user = ctx.settings.get("default_user", "")

            def _on_started(self, _payload):
                self.started = getattr(self, "started", 0) + 1
    """)
    _write_user_plugin(tmp_path, "health", body=body)
    cfg = FakeConfig({"plugins.enabled": ["health"]})
    host = PluginHost(connection_manager=None)
    stored = {"timeout": 9.5, "format": "ansible", "default_user": "cw"}

    loaded = load_plugins(
        app_config=cfg, connection_manager=None, plugin_host=host)
    assert not any(p.plugin_id == "health" for p in loaded)

    _present(host, _settings_client(stored))

    health = next(p for p in loaded if p.plugin_id == "health")
    assert health.instance.timeout == 9.5
    assert health.instance.format == "ansible"
    assert health.instance.default_user == "cw"
    assert health.instance.started == 1
    assert host.ui.page_ids_for_plugin("health") == ["health:health"]


def test_settings_plugin_waits_if_the_backend_settles_before_the_window_binds(tmp_path):
    """Client selection can resolve during window construction, before
    ``bind_window``. Dropping a deferred plugin at that moment loses it:
    ``daemon_client()`` is still None until the window is bound, and
    ``app_started`` is what finally runs with both in place."""
    from sshpilot.plugins.host import PluginHost

    body = textwrap.dedent("""
        from sshpilot.plugins.api import SshPilotPlugin

        class Plugin(SshPilotPlugin):
            def activate(self, ctx):
                self.region = ctx.settings.get("region", "fra1")
    """)
    _write_user_plugin(tmp_path, "inventory-import", body=body)
    cfg = FakeConfig({"plugins.enabled": ["inventory-import"]})
    host = PluginHost(connection_manager=None)
    loaded = load_plugins(
        app_config=cfg, connection_manager=None, plugin_host=host)

    host.notify_backend_settled()
    assert not any(p.plugin_id == "inventory-import" for p in loaded)

    host._window = _BoundWindow(_settings_client({"region": "nyc1"}))
    host.dispatch_app_started()

    plugin = next(p for p in loaded if p.plugin_id == "inventory-import")
    assert plugin.instance.region == "nyc1"


def test_deferred_load_keeps_app_started_and_refreshes_an_open_plugins_page(tmp_path):
    """A plugin that did not need the backend still gets one app_started, and
    a Plugins page built before the client attached is rebuilt so it does not
    stay on Inactive."""
    from sshpilot.plugins.host import PluginHost

    ready = textwrap.dedent("""
        from sshpilot.plugins.api import Events, SshPilotPlugin

        class Plugin(SshPilotPlugin):
            def activate(self, ctx):
                ctx.events.subscribe(Events.APP_STARTED, self._on_started)

            def _on_started(self, _payload):
                self.started = getattr(self, "started", 0) + 1
    """)
    deferred = textwrap.dedent("""
        from sshpilot.plugins.api import SshPilotPlugin

        class Plugin(SshPilotPlugin):
            def activate(self, ctx):
                self.timeout = ctx.settings.get("timeout", 3.0)
    """)
    _write_user_plugin(tmp_path, "notes", body=ready)
    _write_user_plugin(tmp_path, "health", body=deferred)
    cfg = FakeConfig({"plugins.enabled": ["notes", "health"]})
    host = PluginHost(connection_manager=None)
    loaded = load_plugins(
        app_config=cfg, connection_manager=None, plugin_host=host)
    assert any(p.plugin_id == "notes" for p in loaded)
    assert not any(p.plugin_id == "health" for p in loaded)

    class _Prefs:
        def __init__(self):
            self.rebuilt = 0

        def _rebuild_plugins_page(self):
            self.rebuilt += 1

    prefs = _Prefs()
    window = _BoundWindow(_settings_client({"timeout": 1.5}))
    window._preferences_window = prefs
    host._window = window
    host.dispatch_app_started()
    assert prefs.rebuilt == 0  # backend has not settled; nothing retried yet
    host.notify_backend_settled()

    notes = next(p for p in loaded if p.plugin_id == "notes")
    health = next(p for p in loaded if p.plugin_id == "health")
    assert notes.instance.started == 1
    assert health.instance.timeout == 1.5
    assert prefs.rebuilt == 1


def test_settings_plugin_stays_unloaded_when_the_backend_never_connects(tmp_path):
    from sshpilot.plugins.host import PluginHost

    body = textwrap.dedent("""
        from sshpilot.plugins.api import SshPilotPlugin

        class Plugin(SshPilotPlugin):
            def activate(self, ctx):
                self.value = ctx.settings.get("format", "plain")
    """)
    _write_user_plugin(tmp_path, "tailscale", body=body)
    cfg = FakeConfig({"plugins.enabled": ["tailscale"]})
    host = PluginHost(connection_manager=None)
    loaded = load_plugins(
        app_config=cfg, connection_manager=None, plugin_host=host)

    host._window = _BoundWindow(client=None)
    host.dispatch_app_started()
    host.notify_backend_settled()

    assert not any(p.plugin_id == "tailscale" for p in loaded)


def test_daemon_protocol_load_does_not_retry_settings_reads(tmp_path):
    """The daemon has no client to attach later. A settings read in activate()
    must still skip the plugin; waiting would never finish, and the protocol
    would look registered when its backend never ran."""
    body = textwrap.dedent("""
        from sshpilot.plugins.api import (
            ProtocolBackend, SpawnSpec, SshPilotPlugin,
        )

        class _Backend(ProtocolBackend):
            protocol_id = "settings-proto"
            display_name = "settings-proto"

            def capabilities(self):
                return frozenset()

            def build_spawn(self, connection, ctx):
                return SpawnSpec(argv=["true"])

        class Plugin(SshPilotPlugin):
            def activate(self, ctx):
                ctx.settings.get("region", "fra1")
                ctx.register_protocol(_Backend())
    """)
    _write_user_plugin(tmp_path, "settings-proto", body=body)
    cfg = FakeConfig({"plugins.enabled": ["settings-proto"]})

    loader_mod.ensure_user_protocols(app_config=cfg, protocol="settings-proto")

    assert registry_mod.protocol_registry().get_or_none("settings-proto") is None


def test_activate_error_other_than_missing_backend_is_not_retried(tmp_path):
    from sshpilot.plugins.host import PluginHost

    body = textwrap.dedent("""
        from sshpilot.plugins.api import SshPilotPlugin

        class Plugin(SshPilotPlugin):
            def activate(self, ctx):
                raise RuntimeError("boom")
    """)
    _write_user_plugin(tmp_path, "broken-activate", body=body)
    cfg = FakeConfig({"plugins.enabled": ["broken-activate"]})
    host = PluginHost(connection_manager=None)
    loaded = load_plugins(
        app_config=cfg, connection_manager=None, plugin_host=host)

    _present(host, _settings_client({}))

    assert not any(p.plugin_id == "broken-activate" for p in loaded)


def test_discover_reads_manifest_version(tmp_path):
    # A `version` in plugin.json populates PluginInfo.version (drives update
    # detection); its absence leaves it None.
    versioned = (tmp_path / "xdg-data" / "sshpilot" / "plugins" / "versioned")
    versioned.mkdir(parents=True)
    (versioned / "plugin.json").write_text(json.dumps({
        "id": "versioned", "name": "Versioned", "api_version": 1,
        "version": "1.2.3", "homepage": "https://example.com/src",
    }))
    (versioned / "__init__.py").write_text("")
    _write_user_plugin(tmp_path, "nover")  # no version field

    by_id = {i.plugin_id: i for i in discover_plugins()}
    assert by_id["versioned"].version == "1.2.3"
    assert by_id["versioned"].homepage == "https://example.com/src"
    assert by_id["nover"].version is None
    assert by_id["nover"].homepage is None


def test_user_plugin_with_dataclass_loads(tmp_path):
    # A user plugin using @dataclass + `from __future__ import annotations`
    # must load. On Python 3.14 @dataclass resolves annotations via
    # sys.modules[cls.__module__], so the loader has to register the module
    # before exec_module — otherwise this raises AttributeError on import.
    body = textwrap.dedent("""
        from __future__ import annotations
        from dataclasses import dataclass
        from sshpilot.plugins.api import SshPilotPlugin

        @dataclass
        class Row:
            name: str
            port: int = 22

        class Plugin(SshPilotPlugin):
            def activate(self, ctx):
                self.row = Row("x")
    """)
    _write_user_plugin(tmp_path, "dataclassy", body=body)
    cfg = FakeConfig({"plugins.enabled": ["dataclassy"]})
    loaded = load_plugins(app_config=cfg, connection_manager=None)
    assert any(p.plugin_id == "dataclassy" and not p.builtin for p in loaded)


def test_discover_plugins_lists_without_importing(tmp_path):
    plugin_dir = _write_user_plugin(
        tmp_path, "lazy",
        body="open(__file__ + '.executed', 'w').close()\n")
    infos = discover_plugins()
    by_id = {i.plugin_id: i for i in infos}

    assert "ssh" in by_id
    assert by_id["ssh"].builtin and by_id["ssh"].required
    assert by_id["ssh"].api_compatible

    assert "lazy" in by_id
    assert not by_id["lazy"].builtin
    # Discovery must not execute plugin code.
    assert not (plugin_dir / "__init__.py.executed").exists()


# --- headless (daemon-process) protocol resolution ------------------------

def _write_manifest(plugin_dir, **extra):
    manifest = json.loads((plugin_dir / "plugin.json").read_text())
    manifest.update(extra)
    (plugin_dir / "plugin.json").write_text(json.dumps(manifest))


def test_ensure_user_protocols_registers_a_user_backend(tmp_path):
    """The daemon launches sessions in its own process, where a user plugin's
    activate() never ran — so its protocol has to be registered there too."""
    _write_user_plugin(tmp_path, "ssm")
    cfg = FakeConfig({"plugins.enabled": ["ssm"]})

    assert registry_mod.protocol_registry().get_or_none("ssm") is None
    loader_mod.ensure_user_protocols(app_config=cfg, protocol="ssm")
    assert registry_mod.protocol_registry().get_or_none("ssm") is not None


def test_ensure_user_protocols_ignores_plugins_not_enabled(tmp_path):
    _write_user_plugin(tmp_path, "ssm")
    loader_mod.ensure_user_protocols(app_config=FakeConfig(), protocol="ssm")
    assert registry_mod.protocol_registry().get_or_none("ssm") is None


def test_ensure_user_protocols_activates_without_a_window(tmp_path):
    """A protocol plugin may register a page too. With no UI to register it
    into, that call must not abort activate() before register_protocol runs."""
    _write_user_plugin(tmp_path, "ssm", body=textwrap.dedent("""
        from sshpilot.plugins.api import (
            ProtocolBackend, SpawnSpec, SshPilotPlugin, Events,
        )

        class _Backend(ProtocolBackend):
            protocol_id = "ssm"
            display_name = "ssm"

            def capabilities(self):
                return frozenset()

            def build_spawn(self, connection, ctx):
                return SpawnSpec(argv=["true"])

        class Plugin(SshPilotPlugin):
            def activate(self, ctx):
                ctx.ui.register_page("p", "P", "icon", lambda: None)
                ctx.ui.notify("hello")
                ctx.events.subscribe(Events.APP_STARTED, lambda _p: None)
                ctx.register_protocol(_Backend())
    """))
    cfg = FakeConfig({"plugins.enabled": ["ssm"]})

    loader_mod.ensure_user_protocols(app_config=cfg, protocol="ssm")
    assert registry_mod.protocol_registry().get_or_none("ssm") is not None


def test_ensure_user_protocols_skips_plugins_that_disclaim_the_protocol(tmp_path):
    """A manifest that declares its protocols is believed, so the daemon does
    not import an unrelated plugin (and whatever that import drags in)."""
    other = _write_user_plugin(tmp_path, "aaa-other")
    _write_manifest(other, protocols=["something-else"])
    # Sorts first, so without the manifest filter it would be imported before
    # the plugin we actually want. The loader swallows import failures, so
    # prove the skip with a marker rather than by raising.
    (other / "__init__.py").write_text(
        "open(__file__ + '.executed', 'w').close()\n")
    ssm = _write_user_plugin(tmp_path, "zzz-ssm")
    _write_manifest(ssm, protocols=["zzz-ssm"])
    cfg = FakeConfig({"plugins.enabled": ["aaa-other", "zzz-ssm"]})

    loader_mod.ensure_user_protocols(app_config=cfg, protocol="zzz-ssm")
    assert registry_mod.protocol_registry().get_or_none("zzz-ssm") is not None
    assert not (other / "__init__.py.executed").exists()


def test_ensure_user_protocols_stops_once_the_protocol_resolves(tmp_path):
    """Undeclared plugins are swept in id order, but only until the wanted
    protocol appears — a later plugin is left alone."""
    _write_user_plugin(tmp_path, "aaa-ssm")
    later = _write_user_plugin(tmp_path, "zzz-later")
    (later / "__init__.py").write_text(
        "open(__file__ + '.executed', 'w').close()\n")
    cfg = FakeConfig({"plugins.enabled": ["aaa-ssm", "zzz-later"]})

    loader_mod.ensure_user_protocols(app_config=cfg, protocol="aaa-ssm")
    assert registry_mod.protocol_registry().get_or_none("aaa-ssm") is not None
    assert not (later / "__init__.py.executed").exists()


def test_ensure_user_protocols_activates_each_plugin_once(tmp_path):
    """A second launch of an unknown protocol must not re-activate what is
    already loaded (double registration is rejected and logged as a warning)."""
    plugin_dir = _write_user_plugin(tmp_path, "ssm")
    (plugin_dir / "__init__.py").write_text(
        (plugin_dir / "__init__.py").read_text()
        + "\nwith open(__file__ + '.count', 'a') as fh:\n    fh.write('x')\n")
    cfg = FakeConfig({"plugins.enabled": ["ssm"]})

    loader_mod.ensure_user_protocols(app_config=cfg, protocol="ssm")
    loader_mod.ensure_user_protocols(app_config=cfg, protocol="nope")
    assert (plugin_dir / "__init__.py.count").read_text() == "x"
