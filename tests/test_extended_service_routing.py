"""Phase 10.1 extended-service routing — no silent GTK-owned fallbacks."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from sshpilot.api.capabilities import Capability
from sshpilot.api.daemon_client import DaemonClient
from sshpilot.extended_service_policy import (
    LEGACY_LOCAL_FORWARD_SETTING,
    LEGACY_LOCAL_SFTP_SETTING,
    LEGACY_SCP_SETTING,
    ExtendedServiceRoute,
    allow_legacy_local_forward,
    allow_legacy_local_sftp,
    allow_legacy_scp,
    prefer_daemon_extended_services,
    resolve_file_manager_route,
)
from sshpilot.plugins import api as plugins_api


def _config(settings: dict):
    return SimpleNamespace(
        get_setting=lambda key, default=None: settings.get(key, default)
    )


class StubDaemonClient(DaemonClient):
    """DaemonClient subclass that skips socket handshake for unit tests."""

    def __init__(self):
        self._capabilities = None

    def get_capabilities(self):
        return self._capabilities

    def open_forward(self, request):
        raise NotImplementedError

    def get_forward(self, forward_id):
        raise NotImplementedError


def _full_sftp_caps():
    return frozenset(
        {
            Capability.SFTP_READ,
            Capability.SFTP_WRITE,
            Capability.SFTP_EVENTS,
            Capability.SFTP_METADATA,
            Capability.SFTP_MUTATE,
            Capability.TRANSFERS_READ,
            Capability.TRANSFERS_WRITE,
        }
    )


def _full_forward_caps():
    return frozenset(
        {
            Capability.FORWARDS_READ,
            Capability.FORWARDS_WRITE,
            Capability.FORWARDS_LOCAL,
        }
    )


def _caps(supported):
    caps = Mock()
    caps.supported = frozenset(supported)
    return caps


@pytest.fixture(autouse=True)
def _clear_plugin_forwards():
    with plugins_api._FORWARDS_LOCK:
        plugins_api._FORWARDS.clear()
    yield
    with plugins_api._FORWARDS_LOCK:
        plugins_api._FORWARDS.clear()


class TestPolicyHelpers:
    def test_daemon_env_prefers_daemon(self):
        assert prefer_daemon_extended_services(
            environment={"SSHPILOT_CLIENT_MODE": "daemon"}
        )

    def test_explicit_in_process_allows_legacy(self):
        config = _config({"terminal.daemon_backed_ssh": True})
        env = {"SSHPILOT_CLIENT_MODE": "in_process"}
        assert not prefer_daemon_extended_services(config, environment=env)
        assert allow_legacy_local_sftp(config, environment=env)
        assert allow_legacy_scp(config, environment=env)
        assert allow_legacy_local_forward(config, environment=env)

    def test_stage_c_config_promotes_daemon(self):
        config = _config({"terminal.daemon_backed_ssh": True})
        assert prefer_daemon_extended_services(config, environment={})
        assert (
            resolve_file_manager_route(config, environment={})
            is ExtendedServiceRoute.DAEMON
        )

    def test_legacy_settings_override_daemon(self):
        config = _config(
            {
                "terminal.daemon_backed_ssh": True,
                LEGACY_LOCAL_SFTP_SETTING: True,
                LEGACY_SCP_SETTING: True,
                LEGACY_LOCAL_FORWARD_SETTING: True,
            }
        )
        env = {"SSHPILOT_CLIENT_MODE": "daemon"}
        assert allow_legacy_local_sftp(config, environment=env)
        assert allow_legacy_scp(config, environment=env)
        assert allow_legacy_local_forward(config, environment=env)
        assert (
            resolve_file_manager_route(config, environment=env)
            is ExtendedServiceRoute.LEGACY_LOCAL
        )


class TestFileManagerBackendRouting:
    def test_daemon_with_full_caps_selects_daemon_backend(self):
        from sshpilot.file_manager import create_file_manager_backend

        client = StubDaemonClient()
        client._capabilities = _caps(_full_sftp_caps())
        sentinel = object()

        with patch(
            "sshpilot.daemon_sftp_backend.DaemonSftpManager",
            return_value=sentinel,
        ) as daemon_cls, patch(
            "sshpilot.file_manager.openssh_backend.OpenSSHSFTPManager"
        ) as openssh:
            backend = create_file_manager_backend(
                "host",
                "user",
                22,
                daemon_client=client,
                bridge=Mock(),
                connection_id="conn:00000000-0000-4000-8000-000000000001",
                prefer_daemon=True,
            )
        assert backend is sentinel
        daemon_cls.assert_called_once()
        openssh.assert_not_called()

    def test_daemon_missing_sftp_write_raises_no_local_spawn(self):
        from sshpilot.file_manager import create_file_manager_backend

        client = StubDaemonClient()
        client._capabilities = _caps(_full_sftp_caps() - {Capability.SFTP_WRITE})

        with patch(
            "sshpilot.file_manager.openssh_backend.OpenSSHSFTPManager"
        ) as openssh:
            with pytest.raises(RuntimeError, match="missing:.*sftp.write"):
                create_file_manager_backend(
                    "host",
                    "user",
                    22,
                    daemon_client=client,
                    bridge=Mock(),
                    connection_id="conn:00000000-0000-4000-8000-000000000001",
                    prefer_daemon=True,
                )
        openssh.assert_not_called()

    def test_daemon_missing_transfer_caps_raises(self):
        from sshpilot.file_manager import create_file_manager_backend

        client = StubDaemonClient()
        client._capabilities = _caps(
            _full_sftp_caps()
            - {Capability.TRANSFERS_READ, Capability.TRANSFERS_WRITE}
        )

        with patch(
            "sshpilot.file_manager.openssh_backend.OpenSSHSFTPManager"
        ) as openssh:
            with pytest.raises(RuntimeError, match="missing:.*transfers"):
                create_file_manager_backend(
                    "host",
                    "user",
                    22,
                    daemon_client=client,
                    bridge=Mock(),
                    connection_id="conn:00000000-0000-4000-8000-000000000001",
                    prefer_daemon=True,
                )
        openssh.assert_not_called()

    def test_daemon_mode_without_client_raises_no_silent_fallback(self):
        from sshpilot.file_manager import create_file_manager_backend

        with patch(
            "sshpilot.file_manager.openssh_backend.OpenSSHSFTPManager"
        ) as openssh:
            with pytest.raises(RuntimeError, match="no DaemonClient"):
                create_file_manager_backend(
                    "host",
                    "user",
                    22,
                    prefer_daemon=True,
                )
        openssh.assert_not_called()

    def test_in_process_allows_openssh_backend(self, monkeypatch):
        monkeypatch.setenv("SSHPILOT_CLIENT_MODE", "in_process")
        from sshpilot.file_manager import create_file_manager_backend

        sentinel = object()
        with patch(
            "sshpilot.file_manager.openssh_backend.OpenSSHSFTPManager",
            return_value=sentinel,
        ) as openssh:
            backend = create_file_manager_backend("host", "user", 22)
        assert backend is sentinel
        openssh.assert_called_once()

    def test_explicit_legacy_sftp_allows_openssh_in_daemon_mode(self, monkeypatch):
        monkeypatch.setenv("SSHPILOT_CLIENT_MODE", "daemon")
        from sshpilot.file_manager import create_file_manager_backend

        config = _config({LEGACY_LOCAL_SFTP_SETTING: True})
        sentinel = object()
        with patch(
            "sshpilot.file_manager.openssh_backend.OpenSSHSFTPManager",
            return_value=sentinel,
        ) as openssh:
            backend = create_file_manager_backend(
                "host", "user", 22, config=config, prefer_daemon=True
            )
        assert backend is sentinel
        openssh.assert_called_once()


class TestEnsureLocalForwardRouting:
    def _ctx(self, *, client, config=None):
        from sshpilot.plugins.api import PluginContext

        cm = Mock()
        conn = Mock()
        conn.nickname = "Demo"
        conn.uuid = "00000000-0000-4000-8000-000000000099"
        cm.find_connection_by_nickname.return_value = conn

        # host=None avoids Events/UI facades; attach client after construction.
        ctx = PluginContext(
            plugin_id="test",
            app_config=config or _config({}),
            connection_manager=cm,
            protocol_registry=Mock(),
            host=None,
        )
        ctx._host = SimpleNamespace(client=client, window=None)
        return ctx

    def test_daemon_missing_forward_caps_raises_no_local_spawn(self):
        client = StubDaemonClient()
        client._capabilities = _caps(())
        ctx = self._ctx(
            client=client,
            config=_config({"terminal.daemon_backed_ssh": True}),
        )

        with patch("subprocess.Popen") as popen, patch("subprocess.run") as run:
            with pytest.raises(RuntimeError, match="missing capabilities"):
                ctx.ensure_local_forward("Demo", 8080, timeout=0.5)
        popen.assert_not_called()
        run.assert_not_called()

    def test_daemon_unavailable_raises_no_silent_fallback(self):
        ctx = self._ctx(
            client=None,
            config=_config({"terminal.daemon_backed_ssh": True}),
        )
        with patch("subprocess.Popen") as popen:
            with pytest.raises(RuntimeError, match="no DaemonClient"):
                ctx.ensure_local_forward("Demo", 8080, timeout=0.5)
        popen.assert_not_called()

    def test_explicit_legacy_forward_allows_local_path(self):
        ctx = self._ctx(
            client=None,
            config=_config(
                {
                    "terminal.daemon_backed_ssh": True,
                    LEGACY_LOCAL_FORWARD_SETTING: True,
                }
            ),
        )
        with patch(
            "sshpilot.port_utils.find_available_port", return_value=18080
        ), patch(
            "sshpilot.port_utils.is_port_available", side_effect=[False]
        ), patch(
            "sshpilot.ssh_multiplex.is_active", return_value=False
        ), patch(
            "sshpilot.ssh_connection_builder.build_ssh_connection"
        ) as build, patch(
            "sshpilot.ssh_connection_builder.apply_headless_askpass_env",
            side_effect=lambda env, *_a, **_k: env or {},
        ), patch("subprocess.Popen") as popen:
            prepared = Mock()
            prepared.command = ["ssh", "-N"]
            prepared.env = {}
            prepared.password = None
            build.return_value = prepared
            proc = Mock()
            proc.poll.return_value = None
            popen.return_value = proc

            port = ctx.ensure_local_forward("Demo", 8080, timeout=2.0)

        assert port == 18080
        popen.assert_called_once()

    def test_daemon_open_forward_success(self):
        from sshpilot.api.models.operations import ForwardState

        client = StubDaemonClient()
        client._capabilities = _caps(_full_forward_caps())
        summary = Mock()
        summary.id = "fwd:1"
        summary.bind_port = 19090
        client.open_forward = Mock(return_value=summary)
        current = Mock()
        current.state = ForwardState.ACTIVE
        current.bind_port = 19090
        client.get_forward = Mock(return_value=current)

        ctx = self._ctx(
            client=client,
            config=_config({"terminal.daemon_backed_ssh": True}),
        )
        with patch(
            "sshpilot.port_utils.find_available_port", return_value=19090
        ), patch("subprocess.Popen") as popen:
            port = ctx.ensure_local_forward("Demo", 8080, timeout=2.0)

        assert port == 19090
        client.open_forward.assert_called_once()
        popen.assert_not_called()


class TestLegacyScpGate:
    def test_daemon_mode_refuses_scp_without_setting(self):
        assert not allow_legacy_scp(
            _config({"terminal.daemon_backed_ssh": True}),
            environment={},
        )

    def test_legacy_scp_setting_allows(self):
        assert allow_legacy_scp(
            _config(
                {
                    "terminal.daemon_backed_ssh": True,
                    LEGACY_SCP_SETTING: True,
                }
            ),
            environment={"SSHPILOT_CLIENT_MODE": "daemon"},
        )
