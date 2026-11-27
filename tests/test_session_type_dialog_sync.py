"""SessionType none must show on the ssh -N switch and survive a save.

Issue #1389: the host block has ``SessionType none`` but the connection
dialog's "Do not start a remote shell" switch is off, and turning it on
does not stick. The loader always hands the dialog ``sessiontype none``.

``test_session_type_none_is_written_and_removed_in_the_ssh_config`` enables
and disables that switch and writes each result through the daemon
connection service into a temporary SSH config.
"""

from types import SimpleNamespace

import pytest


def test_loader_keeps_session_type_none_as_an_extra_option(tmp_path):
    from sshpilot.core.connections.ssh_config_loader import load_ssh_configuration

    config = tmp_path / "config"
    config.write_text(
        "Host tunnel\n"
        "    HostName example.com\n"
        "    SessionType none\n",
        encoding="utf-8",
    )

    loaded = load_ssh_configuration(config, isolated=True)
    record = next(item for item in loaded.connections if item.id == "tunnel")

    assert record.data.get("extra_ssh_config") == "sessiontype none"


def _real_gtk_available():
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw

        Adw.init()
        return type(Adw.PreferencesGroup()).__name__ == "PreferencesGroup"
    except Exception:
        return False


class _Manager:
    connections = []
    isolated_mode = False

    def load_ssh_keys(self):
        return []

    def get_key_passphrase(self, _path):
        return ""

    def find_connection_by_nickname(self, _nickname):
        return None

    def get_password(self, _host, _user):
        return None

    def format_ssh_config_entry(self, _data):
        return ""


def _connection(extra_ssh_config):
    return SimpleNamespace(
        nickname="tunnel",
        hostname="example.com",
        username="alice",
        port=22,
        auth_method=0,
        key_select_mode=0,
        pubkey_auth_no=False,
        identity_files=[],
        keyfile="",
        private_key=None,
        certificate_files=[],
        certificate="",
        proxy_jump=[],
        forward_agent=False,
        x11_forwarding=False,
        password="",
        key_passphrase="",
        extra_ssh_config=extra_ssh_config,
        data={},
        aliases=[],
        local_command="",
        remote_command="",
        pre_command="",
        forwarding_rules=[],
        identity_agent="",
        add_keys_to_agent="",
        pkcs11_provider="",
        security_key_provider="",
        protocol="ssh",
    )


def _open_dialog(extra_ssh_config):
    from gi.repository import Gtk
    from sshpilot.connection_dialog import ConnectionDialog

    dialog = ConnectionDialog(Gtk.Window(), connection=None, connection_manager=_Manager())
    dialog.connection = _connection(extra_ssh_config)
    dialog.is_editing = True
    dialog.load_connection_data()
    return dialog


def _save(dialog):
    captured = {}

    def _on_saved(_dialog, payload, *_rest):
        captured.update(payload)
        _rest[-1].complete(True)

    dialog.connect("connection-saved", _on_saved)
    dialog.on_save_clicked()
    return captured


@pytest.mark.integration
@pytest.mark.skipif(not _real_gtk_available(), reason="needs real libadwaita")
def test_session_type_none_enables_the_switch_and_survives_save():
    dialog = _open_dialog("sessiontype none")
    try:
        assert dialog.port_forwarding_only_row.get_active() is True
        # The reported failure: the switch is off, turning it on, then saving,
        # leaves it off the next time the dialog loads the saved line.
        dialog.port_forwarding_only_row.set_active(False)
        dialog.port_forwarding_only_row.set_active(True)
        assert dialog.port_forwarding_only_row.get_active() is True

        saved = _save(dialog)
        assert "sessiontype none" in saved["extra_ssh_config"].lower()
    finally:
        dialog.close()

    reopened = _open_dialog(saved["extra_ssh_config"])
    try:
        assert reopened.port_forwarding_only_row.get_active() is True
        # The switch owns the line: saved with the block, not listed on
        # the Extra Options page.
        assert "sessiontype none" in reopened._combined_extra_ssh_config().lower()
        assert "sessiontype" not in reopened.advanced_tab.get_extra_ssh_config().lower()
    finally:
        reopened.close()


@pytest.mark.integration
@pytest.mark.skipif(not _real_gtk_available(), reason="needs real libadwaita")
def test_exit_on_forward_failure_yes_enables_the_switch_and_survives_save():
    dialog = _open_dialog("sessiontype none\nexitonforwardfailure yes")
    try:
        assert dialog.port_forwarding_only_row.get_active() is True
        assert dialog.exit_on_forward_failure_row.get_active() is True
        dialog.exit_on_forward_failure_row.set_active(False)
        dialog.exit_on_forward_failure_row.set_active(True)
        assert dialog.exit_on_forward_failure_row.get_active() is True
        assert dialog.port_forwarding_only_row.get_active() is True

        saved = _save(dialog)
        lowered = saved["extra_ssh_config"].lower()
        assert "exitonforwardfailure yes" in lowered
        assert "sessiontype none" in lowered
    finally:
        dialog.close()

    reopened = _open_dialog(saved["extra_ssh_config"])
    try:
        assert reopened.exit_on_forward_failure_row.get_active() is True
        assert reopened.port_forwarding_only_row.get_active() is True
    finally:
        reopened.close()


def _session_type_lines(text):
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip().lower().startswith("sessiontype")
    ]


def _loaded_extra(root):
    from sshpilot.core.connections.ssh_config_loader import load_ssh_configuration

    loaded = load_ssh_configuration(root, isolated=True)
    record = next(item for item in loaded.connections if item.id == "tunnel")
    return record.data.get("extra_ssh_config") or ""


def _repository(tmp_path):
    from sshpilot.core.connection_application_service import (
        ConnectionApplicationService,
    )
    from sshpilot.core.connections.repository import ConnectionRepository
    from sshpilot.core.connections.ssh_config_store import SshConfigStore

    root = tmp_path / "ssh_config"
    root.write_text(
        "Host tunnel\n"
        "    HostName example.com\n"
        "    User alice\n",
        encoding="utf-8",
    )
    repository = ConnectionRepository(
        ssh_store=SshConfigStore(root, isolated=True),
        state_path=tmp_path / "connections.json",
        legacy_config_path=tmp_path / "config.json",
        isolated=True,
    )
    service = ConnectionApplicationService(repository, client_name="test")
    return service, repository, root


def _save_through_daemon(service, repository, dialog):
    """Persist the dialog payload with the daemon connection service.

    The save signal's completion callback only tells the dialog the write
    finished. The SSH config file is updated first, by
    ``ConnectionApplicationService.update_connection``.
    """
    from sshpilot.api.models.common import ConnectionId
    from sshpilot.api.models.connections import UpdateConnectionRequest

    failure = []

    def _on_saved(_dialog, payload, *_rest):
        try:
            changed = set(payload.get("__changed_fields") or ())
            if "extra_ssh_config" not in changed:
                raise AssertionError(
                    "dialog save omitted extra_ssh_config from the changed fields"
                )
            record = repository.get_record("tunnel")
            service.update_connection(
                ConnectionId("tunnel"),
                UpdateConnectionRequest(
                    config_patch={
                        "extra_ssh_config": payload["extra_ssh_config"],
                    },
                    expected_generation=record.generation,
                ),
            )
            _rest[-1].complete(True)
        except Exception as exc:
            failure.append(exc)
            _rest[-1].complete(False)

    dialog.connect("connection-saved", _on_saved)
    dialog.on_save_clicked()
    if failure:
        raise failure[0]


@pytest.mark.integration
@pytest.mark.skipif(not _real_gtk_available(), reason="needs real libadwaita")
def test_session_type_none_is_written_and_removed_in_the_ssh_config(tmp_path):
    service, repository, root = _repository(tmp_path)
    assert repository.get_record("tunnel") is not None
    assert _session_type_lines(root.read_text(encoding="utf-8")) == []

    dialog = _open_dialog("")
    try:
        assert dialog.port_forwarding_only_row.get_active() is False
        dialog.port_forwarding_only_row.set_active(True)
        _save_through_daemon(service, repository, dialog)
    finally:
        dialog.close()

    enabled = root.read_text(encoding="utf-8")
    assert _session_type_lines(enabled) == ["SessionType none"]
    assert "HostName example.com" in enabled
    assert "User alice" in enabled
    assert _loaded_extra(root) == "sessiontype none"

    reloaded = _open_dialog(_loaded_extra(root))
    try:
        assert reloaded.port_forwarding_only_row.get_active() is True
        reloaded.port_forwarding_only_row.set_active(False)
        _save_through_daemon(service, repository, reloaded)
    finally:
        reloaded.close()

    disabled = root.read_text(encoding="utf-8")
    assert _session_type_lines(disabled) == []
    assert "HostName example.com" in disabled
    assert "User alice" in disabled
    assert _loaded_extra(root) == ""

    reopened = _open_dialog(_loaded_extra(root))
    try:
        assert reopened.port_forwarding_only_row.get_active() is False
    finally:
        reopened.close()
