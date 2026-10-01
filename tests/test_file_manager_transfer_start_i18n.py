"""Controller -> adapter -> Future -> File Manager -> translated dialog."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from sshpilot.api.capabilities import Capability
from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.gtk import transfer_error_messages
from sshpilot.transfer_service_controller import TransferServiceController
from tests.daemon.test_daemon_sftp_backend_file_ops import _manager


@pytest.mark.parametrize("kind", [
    "batch", "upload", "download", "controller_closed", "bridge_closed", "transport",
    "ambiguous", "not_ready", "invalid_request", "opaque", "success",
])
def test_transfer_start_error_reaches_translated_dialog(load_file_manager_window, monkeypatch, kind):
    window_module = load_file_manager_window()
    from sshpilot import daemon_sftp_backend
    from sshpilot.file_manager import progress_dialog
    from sshpilot.sftp_service_controller import SftpControllerState

    supported = {Capability.TRANSFERS_READ, Capability.TRANSFERS_WRITE,
                 Capability.TRANSFERS_UPLOAD, Capability.TRANSFERS_DOWNLOAD, Capability.TRANSFERS_BATCH}
    missing = {
        "batch": Capability.TRANSFERS_BATCH,
        "upload": Capability.TRANSFERS_UPLOAD,
        "download": Capability.TRANSFERS_DOWNLOAD,
    }.get(kind)
    if missing:
        supported.remove(missing)
    client = SimpleNamespace(get_capabilities=lambda: SimpleNamespace(supported=frozenset(supported)))
    diagnostic = "external {capability}\nline 2\n"

    class Bridge:
        def submit(self, _operation, *, on_success, on_error):
            if kind == "bridge_closed":
                raise RuntimeError("GTK client bridge is closed")
            if kind == "transport":
                on_error(SshPilotError(ErrorCode.TRANSPORT_CLOSED, "unstable transport text"))
            elif kind == "ambiguous":
                on_error(SshPilotError(ErrorCode.MUTATION_AMBIGUOUS, "The session change may have completed"))
            elif kind == "opaque":
                on_error(OSError(diagnostic))
            else:
                from dataclasses import replace

                from tests.test_transfer_service_controller import _summary
                from sshpilot.api.models.transfers import TransferState

                on_success(replace(_summary(TransferState.COMPLETED), items_total=1, items_done=1))

    manager = _manager()
    manager._transfers = TransferServiceController(client, Bridge())
    manager._operation_seq = 0
    manager.connect = MagicMock()
    manager.disconnect = MagicMock()
    manager.emit = MagicMock()
    if kind == "controller_closed":
        manager._transfers.close()
    if kind == "not_ready":
        manager._sftp_controller.state = SftpControllerState.CLOSED
    calls = []
    translations = {
        "The daemon does not support {capability}": "Capacité indisponible : {capability}",
        "The transfer could not be started": "Démarrage du transfert impossible",
        "The daemon connection was closed.": "Connexion au daemon fermée.",
        "The transfer may have started. Refresh before trying again.": "Le transfert a peut-être démarré. Actualisez avant de réessayer.",
        "The SFTP service is not ready": "Le service SFTP n’est pas prêt",
        "Error: {message}": "Erreur : {message}",
    }

    def translate(msgid):
        calls.append(msgid)
        return translations.get(msgid, msgid)

    monkeypatch.setattr(transfer_error_messages, "_", translate)
    monkeypatch.setattr(daemon_sftp_backend, "_", translate)
    monkeypatch.setattr(progress_dialog, "_", translate)
    monkeypatch.setattr(window_module.GLib, "idle_add", lambda fn, *args: fn(*args))

    display = SimpleNamespace(
        _completion_shown=False, _stop_render_timer=MagicMock(), operation_type="upload",
        _set_dialog_heading=MagicMock(), _set_status_text=MagicMock(), _latest_fraction=None,
        _set_speed_and_time=MagicMock(), _enter_completion_actions=MagicMock(),
    )
    dialog = MagicMock()
    dialog.is_cancelled = False

    def complete(success=True, error_message=None):
        if not success:
            progress_dialog.SFTPProgressDialog._show_completion_ui(display, success, error_message)

    dialog.show_completion.side_effect = complete
    window = window_module.FileManagerWindow.__new__(window_module.FileManagerWindow)
    window._manager = manager
    window._transfer_dialogs = []
    window._present_progress_dialog = MagicMock(return_value=dialog)
    window._attach_refresh = MagicMock()
    local_path = "" if kind == "invalid_request" else "/local/a"
    operation = "download" if kind == "download" else "upload"
    future = window._start_transfer_batch(operation, [(local_path, "/remote/a", False)])
    if kind == "success":
        assert future.result().items_done == 1
        dialog.show_completion.assert_called_once_with(success=True)
        assert calls == []
        return

    expected = {
        "controller_closed": "Démarrage du transfert impossible",
        "bridge_closed": "Démarrage du transfert impossible",
        "transport": "Connexion au daemon fermée.",
        "ambiguous": "Le transfert a peut-être démarré. Actualisez avant de réessayer.",
        "not_ready": "Le service SFTP n’est pas prêt",
        "invalid_request": "Démarrage du transfert impossible",
        "opaque": diagnostic,
    }.get(kind, f"Capacité indisponible : {missing.value}" if missing else "")
    dialog.show_completion.assert_called_once_with(success=False, error_message=expected)
    display._set_status_text.assert_called_once_with("Erreur : " + expected)
    assert diagnostic not in calls
