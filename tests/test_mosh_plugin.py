"""Tests for the built-in Mosh protocol plugin (reuses the SSH command/auth path)."""

import os
import sys
import types

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot.connection_manager import Connection
from sshpilot.api.models.sessions import PluginSessionFailureCode
from sshpilot.plugins import registry as registry_mod
from sshpilot.plugins.api import PluginContext
from sshpilot.plugins.builtin._session_failure import BuiltinProtocolError
from sshpilot.plugins.builtin.mosh_protocol import Plugin, MoshProtocolBackend
from sshpilot.plugins.loader import load_plugins


class FakeConfig:
    def get_setting(self, key, default=None):
        return default


@pytest.fixture(autouse=True)
def fresh_registry(monkeypatch, tmp_path):
    monkeypatch.setattr(registry_mod, "_registry", None)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))


def _ctx():
    return PluginContext(plugin_id="mosh", app_config=None, connection_manager=None,
                         protocol_registry=registry_mod.protocol_registry())


def _stub_ssh_builders(monkeypatch, env=None, extra_opts=None):
    """Stub the reused SSH builders so tests don't touch real ssh config/auth."""
    import sshpilot.ssh_connection_builder as scb

    def fake_bnc(conn, app_config, command_type="ssh", remote_command=None, extra_args=None):
        return ["ssh", "-F", "/cfg", *(extra_args or []), "myhost"]

    fake_auth = types.SimpleNamespace(env=env or {}, extra_opts=extra_opts or [],
                                      use_sshpass=False, password=None,
                                      use_askpass=False, password_mode=False)
    monkeypatch.setattr(scb, "build_native_command", fake_bnc)
    monkeypatch.setattr(scb, "resolve_native_auth", lambda *a, **k: fake_auth)


def test_loader_discovers_and_activates_mosh():
    loaded = load_plugins(app_config=FakeConfig(), connection_manager=None)
    assert any(p.plugin_id == 'mosh' and p.builtin for p in loaded)
    backend = registry_mod.protocol_registry().get_or_none('mosh')
    assert backend is not None and backend.default_port == 22


def test_fields_and_validate(monkeypatch):
    import sshpilot.plugins.builtin.mosh_protocol as mod

    monkeypatch.setattr(mod, "_", lambda msgid: f"translated:{msgid}")
    backend = MoshProtocolBackend()
    assert backend.capabilities() == frozenset()
    by_key = {f.key: f for f in backend.connection_fields()}
    assert by_key['host'].required
    assert by_key['host'].placeholder == 'translated:hostname or IP address'
    assert by_key['username'].placeholder == 'translated:(from ~/.ssh/config)'
    assert by_key['keyfile'].kind == 'file'
    assert by_key['predict'].choices == [
        ('adaptive', 'translated:Adaptive (default)'),
        ('always', 'translated:Always'),
        ('never', 'translated:Never'),
        ('experimental', 'translated:Experimental'),
    ]
    assert backend.validate({'host': 'h'}) == []
    assert backend.validate({}) == ['translated:A host is required.']
    assert backend.validate({'host': 'h', 'port': 65536}) == [
        'translated:Port must be between 1 and 65535.'
    ]
    assert backend.validate({'host': 'h', 'port': 'x'}) == [
        'translated:Port must be a number.'
    ]


def test_build_spawn_basic(monkeypatch):
    import sshpilot.plugins.builtin.mosh_protocol as mod
    monkeypatch.setattr(mod.shutil, 'which', lambda name: '/usr/bin/mosh')
    _stub_ssh_builders(monkeypatch, env={'SSH_ASKPASS': '/x'})
    conn = Connection({'nickname': 'm', 'protocol': 'mosh', 'host': 'myhost'})
    spec = MoshProtocolBackend().build_spawn(conn, _ctx())
    assert spec.argv == ['/usr/bin/mosh', '--ssh=ssh -F /cfg', 'myhost']
    assert spec.env.get('SSH_ASKPASS') == '/x'   # merged auth env


def test_build_spawn_threads_port_key_user(monkeypatch):
    import sshpilot.plugins.builtin.mosh_protocol as mod
    monkeypatch.setattr(mod.shutil, 'which', lambda name: '/usr/bin/mosh')
    _stub_ssh_builders(monkeypatch)
    conn = Connection({'nickname': 'm', 'protocol': 'mosh', 'host': 'myhost',
                       'username': 'bob', 'port': 2222, 'keyfile': '/keys/id'})
    spec = MoshProtocolBackend().build_spawn(conn, _ctx())
    # the ssh prefix (everything after --ssh=) carries the threaded options
    ssh_arg = next(a for a in spec.argv if a.startswith('--ssh='))
    assert '-p 2222' in ssh_arg
    assert '-i /keys/id' in ssh_arg
    assert '-l bob' in ssh_arg
    assert spec.argv[-1] == 'myhost'


def test_build_spawn_predict_and_port(monkeypatch):
    import sshpilot.plugins.builtin.mosh_protocol as mod
    monkeypatch.setattr(mod.shutil, 'which', lambda name: '/usr/bin/mosh')
    _stub_ssh_builders(monkeypatch)
    conn = Connection({'nickname': 'm', 'protocol': 'mosh', 'host': 'myhost',
                       'predict': 'always', 'mosh_port': '60000:60010'})
    spec = MoshProtocolBackend().build_spawn(conn, _ctx())
    assert spec.argv[0] == '/usr/bin/mosh'
    assert '--predict=always' in spec.argv
    assert spec.argv[spec.argv.index('--port') + 1] == '60000:60010'
    assert spec.argv[-1] == 'myhost'
    assert any(a.startswith('--ssh=') for a in spec.argv)


def test_build_spawn_omits_default_predict(monkeypatch):
    import sshpilot.plugins.builtin.mosh_protocol as mod
    monkeypatch.setattr(mod.shutil, 'which', lambda name: '/usr/bin/mosh')
    _stub_ssh_builders(monkeypatch)
    conn = Connection({'nickname': 'm', 'protocol': 'mosh', 'host': 'myhost',
                       'predict': 'adaptive'})
    spec = MoshProtocolBackend().build_spawn(conn, _ctx())
    assert not any(a.startswith('--predict') for a in spec.argv)
    assert '--port' not in spec.argv


def test_build_spawn_missing_binary(monkeypatch):
    import sshpilot.plugins.builtin.mosh_protocol as mod
    monkeypatch.setattr(mod.shutil, 'which', lambda name: None)
    conn = Connection({'nickname': 'm', 'protocol': 'mosh', 'host': 'h'})
    with pytest.raises(BuiltinProtocolError, match='not installed') as excinfo:
        MoshProtocolBackend().build_spawn(conn, _ctx())
    assert excinfo.value.failure.code is PluginSessionFailureCode.MOSH_UNAVAILABLE
    assert dict(excinfo.value.failure.parameters) == {
        "client_program": "mosh",
        "server_program": "mosh-server",
    }


def test_build_spawn_missing_host(monkeypatch):
    import sshpilot.plugins.builtin.mosh_protocol as mod
    monkeypatch.setattr(mod.shutil, 'which', lambda name: '/usr/bin/mosh')
    conn = Connection({'nickname': 'm', 'protocol': 'mosh'})
    conn.hostname = ''
    conn.host = ''
    with pytest.raises(BuiltinProtocolError, match='[Nn]o host') as excinfo:
        MoshProtocolBackend().build_spawn(conn, _ctx())
    assert excinfo.value.failure.code is PluginSessionFailureCode.HOST_REQUIRED


def test_build_spawn_preparation_error_keeps_diagnostic_separate(monkeypatch):
    import sshpilot.plugins.builtin.mosh_protocol as mod
    import sshpilot.ssh_connection_builder as builder

    def fail_auth(*_args, **_kwargs):
        raise RuntimeError("opaque auth")

    monkeypatch.setattr(mod.shutil, 'which', lambda name: '/usr/bin/mosh')
    monkeypatch.setattr(builder, "resolve_native_auth", fail_auth)
    conn = Connection({'nickname': 'm', 'protocol': 'mosh', 'host': 'host'})

    with pytest.raises(BuiltinProtocolError) as excinfo:
        MoshProtocolBackend().build_spawn(conn, _ctx())

    failure = excinfo.value.failure
    assert failure.code is PluginSessionFailureCode.MOSH_PREPARATION_FAILED
    assert failure.diagnostic == "opaque auth"
    assert dict(failure.parameters) == {}


def test_build_spawn_command_preparation_error_uses_same_stable_code(monkeypatch):
    import sshpilot.plugins.builtin.mosh_protocol as mod
    import sshpilot.ssh_connection_builder as builder

    def fail_command(*_args, **_kwargs):
        raise ValueError("opaque command builder detail")

    monkeypatch.setattr(mod.shutil, 'which', lambda name: '/usr/bin/mosh')
    monkeypatch.setattr(
        builder,
        "resolve_native_auth",
        lambda *_args, **_kwargs: builder.NativeAuth(env={}, extra_opts=[]),
    )
    monkeypatch.setattr(builder, "build_native_command", fail_command)
    conn = Connection({'nickname': 'm', 'protocol': 'mosh', 'host': 'host'})

    with pytest.raises(BuiltinProtocolError) as excinfo:
        MoshProtocolBackend().build_spawn(conn, _ctx())

    failure = excinfo.value.failure
    assert failure.code is PluginSessionFailureCode.MOSH_PREPARATION_FAILED
    assert failure.diagnostic == "opaque command builder detail"


def test_build_spawn_invalid_ssh_options_use_stable_field_key(monkeypatch):
    import sshpilot.plugins.builtin.mosh_protocol as mod

    monkeypatch.setattr(mod.shutil, 'which', lambda name: '/usr/bin/mosh')
    _stub_ssh_builders(monkeypatch)
    conn = Connection({
        'nickname': 'm',
        'protocol': 'mosh',
        'host': 'host',
        'extra_ssh_opts': "-o '",
    })

    with pytest.raises(BuiltinProtocolError) as excinfo:
        MoshProtocolBackend().build_spawn(conn, _ctx())

    failure = excinfo.value.failure
    assert failure.code is PluginSessionFailureCode.ARGUMENTS_INVALID
    assert dict(failure.parameters) == {"field": "extra_ssh_opts"}
    assert failure.diagnostic


def test_activate_registers_backend():
    Plugin().activate(_ctx())
    assert registry_mod.protocol_registry().get('mosh') is not None
