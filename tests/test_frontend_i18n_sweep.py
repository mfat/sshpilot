"""Regression coverage for the final frontend gettext sweep."""

from __future__ import annotations

import ast
import sys
import types
import webbrowser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_terminal_encoding_translates_before_formatting(monkeypatch):
    from sshpilot import terminal

    msgids = []
    toasts = []
    monkeypatch.setattr(
        terminal,
        "_",
        lambda msgid: msgids.append(msgid) or "localized:{requested}:{fallback}",
    )
    widget = types.SimpleNamespace(_show_toast=toasts.append)

    terminal.TerminalWidget._notify_invalid_encoding(widget, "KOI8-R", "UTF-8")

    assert msgids == [
        "Encoding '{requested}' is not supported. Using {fallback} instead."
    ]
    assert toasts == ["localized:KOI8-R:UTF-8"]


def test_terminal_close_dialog_localizes_copy_and_actions(monkeypatch):
    from sshpilot import terminal

    msgids = []

    class Dialog:
        def __init__(self, heading, body):
            self.heading = heading
            self.body = body
            self.responses = {}

        def add_response(self, response_id, label):
            self.responses[response_id] = label

        def set_response_appearance(self, *_args):
            pass

        def set_default_response(self, *_args):
            pass

        def set_close_response(self, *_args):
            pass

        def connect(self, *_args):
            pass

        def present(self, *_args):
            pass

    dialog = Dialog("", "")
    fake_adw = types.SimpleNamespace(
        AlertDialog=types.SimpleNamespace(
            new=lambda heading, body: (
                setattr(dialog, "heading", heading)
                or setattr(dialog, "body", body)
                or dialog
            )
        ),
        ResponseAppearance=types.SimpleNamespace(
            DESTRUCTIVE="destructive", SUGGESTED="suggested"
        ),
    )
    monkeypatch.setattr(sys.modules["gi.repository"], "Adw", fake_adw)
    monkeypatch.setattr(
        terminal, "_", lambda msgid: msgids.append(msgid) or f"localized:{msgid}"
    )
    widget = types.SimpleNamespace(
        get_root=lambda: object(),
        _daemon_controller=types.SimpleNamespace(detach=lambda: None),
        _on_daemon_close_dialog_response=lambda *_args: None,
    )

    terminal.TerminalWidget._show_daemon_close_dialog(widget)

    assert dialog.heading == "localized:Close Terminal Session"
    assert dialog.body.startswith("localized:What should happen")
    assert dialog.responses == {
        "detach": "localized:Detach",
        "terminate": "localized:Terminate",
        "cancel": "localized:Cancel",
    }
    assert msgids == [
        "Close Terminal Session",
        "What should happen to the remote terminal session?",
        "Detach",
        "Terminate",
        "Cancel",
    ]


def test_terminal_view_only_infobar_is_localized(monkeypatch):
    from sshpilot import terminal

    labels = []

    class InfoBar:
        def set_message_type(self, *_args):
            pass

        def set_show_close_button(self, *_args):
            pass

        def add_child(self, label):
            labels.append(label.label)

        def set_visible(self, *_args):
            pass

    fake_gtk = types.SimpleNamespace(
        InfoBar=InfoBar,
        Label=lambda *, label: types.SimpleNamespace(label=label),
        MessageType=types.SimpleNamespace(INFO="info"),
    )
    monkeypatch.setattr(sys.modules["gi.repository"], "Gtk", fake_gtk)
    monkeypatch.setattr(terminal, "_", lambda msgid: f"localized:{msgid}")
    widget = types.SimpleNamespace(
        _view_only_overlay=None,
        prepend=lambda _child: None,
    )

    terminal.TerminalWidget._show_view_only_indicator(widget)

    assert labels == [
        "localized:View only — another user controls this terminal"
    ]


def test_sshcopyid_errors_localize_context_but_keep_diagnostics_opaque(monkeypatch):
    from sshpilot import sshcopyid_window

    msgids = []
    errors = []
    monkeypatch.setattr(
        sshcopyid_window,
        "_",
        lambda msgid: msgids.append(msgid) or f"localized:{msgid}",
    )
    active = types.SimpleNamespace(get_active=lambda: True)
    inactive = types.SimpleNamespace(get_active=lambda: False)
    window = types.SimpleNamespace(
        _closed=False,
        _loading_keys=False,
        _generating=False,
        radio_existing=active,
        radio_paste=inactive,
        radio_generate=inactive,
        _do_copy_existing=lambda: (_ for _ in ()).throw(
            RuntimeError("opaque ssh-copy-id detail")
        ),
        _error=lambda *args: errors.append(args),
    )

    sshcopyid_window.SshCopyIdWindow._on_ok_clicked(window)

    assert errors == [
        (
            "localized:Operation failed",
            "localized:Could not start the requested action.",
            "opaque ssh-copy-id detail",
        )
    ]
    assert msgids == ["Operation failed", "Could not start the requested action."]


def test_sshcopyid_pasted_key_error_keeps_diagnostic_opaque(monkeypatch):
    from sshpilot import sshcopyid_window

    errors = []
    monkeypatch.setattr(sshcopyid_window, "_", lambda msgid: f"localized:{msgid}")
    buffer = types.SimpleNamespace(
        get_bounds=lambda: (object(), object()),
        get_text=lambda *_args: "ssh-ed25519 AAAATEST user@example",
    )
    window = types.SimpleNamespace(
        paste_view=types.SimpleNamespace(get_buffer=lambda: buffer),
        force_toggle=types.SimpleNamespace(get_active=lambda: False),
        _parent=types.SimpleNamespace(
            _show_ssh_copy_id_terminal_using_main_widget=lambda *_args, **_kwargs: (
                _ for _ in ()
            ).throw(RuntimeError("opaque pasted-key detail"))
        ),
        _conn=object(),
        _error=lambda *args: errors.append(args),
        close=lambda: None,
    )

    sshcopyid_window.SshCopyIdWindow._do_copy_pasted(window)

    assert errors == [
        (
            "localized:Copy failed",
            "localized:Could not copy the pasted key to the server.",
            "opaque pasted-key detail",
        )
    ]


def test_help_fallback_translates_before_inserting_url(monkeypatch):
    from sshpilot import window_help

    msgids = []
    dialogs = []
    monkeypatch.setattr(
        window_help,
        "Gio",
        types.SimpleNamespace(
            AppInfo=types.SimpleNamespace(
                launch_default_for_uri=lambda *_args: (_ for _ in ()).throw(
                    RuntimeError("no portal")
                )
            )
        ),
    )
    monkeypatch.setattr(webbrowser, "open", lambda _url: False)
    monkeypatch.setattr(
        window_help,
        "Gtk",
        types.SimpleNamespace(
            MessageDialog=lambda **kwargs: dialogs.append(
                types.SimpleNamespace(present=lambda: None, kwargs=kwargs)
            )
            or dialogs[-1],
            MessageType=types.SimpleNamespace(ERROR="error"),
            ButtonsType=types.SimpleNamespace(OK="ok"),
        ),
    )
    monkeypatch.setattr(
        window_help,
        "_",
        lambda msgid: msgids.append(msgid) or f"localized:{msgid}",
    )

    window_help.WindowHelpMixin.open_help_url(object())

    assert dialogs[0].kwargs["text"] == "localized:Failed to open help"
    assert dialogs[0].kwargs["secondary_text"] == (
        "localized:Please open this page manually:\n"
        "https://github.com/mfat/sshpilot/wiki"
    )
    assert msgids[-1] == "Please open this page manually:\n{url}"


def test_docker_stable_context_is_localized_and_raw_diagnostics_are_not(monkeypatch):
    from sshpilot.plugins.builtin.docker_manager import widgets

    msgids = []
    monkeypatch.setattr(
        widgets, "_", lambda msgid: msgids.append(msgid) or f"localized:{msgid}"
    )

    assert widgets.describe_docker_failure(
        "Cannot connect to the Docker daemon at unix:///var/run/docker.sock"
    ) == "localized:Docker daemon isn't running on this host"
    assert widgets.describe_docker_failure(
        "vendor-specific opaque diagnostic\nsecond line"
    ) == "vendor-specific opaque diagnostic"
    assert widgets.error_text(RuntimeError("opaque exception detail")) == (
        "localized:Error: opaque exception detail"
    )
    assert "vendor-specific opaque diagnostic" not in msgids
    assert "opaque exception detail" not in msgids


def test_docker_frontend_status_empty_loading_and_confirmations_use_gettext():
    expected_by_file = {
        "page.py": {
            "(no connections)",
            "Connecting to {name}…",
            "Detecting container runtime…",
            "Neither Docker nor Podman found on this host",
            "SSH password required to open Docker Console.",
        },
        "tab_containers.py": {
            "Loading containers…",
            "No containers",
            "Remove container?",
            "Could not open shell",
        },
        "tab_images.py": {
            "Loading images…",
            "No images",
            "Prune unused images?",
            "Prune complete",
        },
        "tab_compose.py": {
            "Loading compose projects…",
            "No compose projects",
            "Tear down stack?",
        },
    }
    base = ROOT / "src/sshpilot/plugins/builtin/docker_manager"
    for filename, expected in expected_by_file.items():
        tree = ast.parse((base / filename).read_text(encoding="utf-8"))
        msgids = {
            node.args[0].value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        }
        assert expected <= msgids


def test_serial_display_label_is_localized_without_changing_plugin_identity(
    monkeypatch,
):
    from sshpilot import connection_dialog
    from sshpilot.plugins.builtin.serial_protocol import SerialProtocolBackend

    backend = SerialProtocolBackend()
    monkeypatch.setattr(
        connection_dialog, "_", lambda msgid: f"localized:{msgid}"
    )

    assert backend.protocol_id == "serial"
    assert backend.display_name == "Serial"
    assert connection_dialog._protocol_display_name(backend) == "localized:Serial"


def test_plural_fragments_translate_before_formatting(monkeypatch):
    from sshpilot import actions

    calls = []

    def fake_ngettext(singular, plural, count):
        calls.append((singular, plural, count))
        return "localized:{count} unit" if count == 1 else "localized:{count} units"

    monkeypatch.setattr(actions, "ngettext", fake_ngettext)
    monkeypatch.setattr(
        actions,
        "_",
        lambda msgid: "{connections} + {subgroups}",
    )

    assert actions._describe_group_contents(1, 2) == (
        "localized:1 unit + localized:2 units"
    )
    assert calls == [
        ("{count} connection", "{count} connections", 1),
        ("{count} subgroup", "{count} subgroups", 2),
    ]


def test_residual_frontend_presenters_localize_without_changing_values(
    monkeypatch,
):
    from sshpilot import askpass_utils, connection_display, forwarding_only_ui

    monkeypatch.setattr(
        connection_display, "_", lambda msgid: "localized:{target} (alias)"
    )
    connection = types.SimpleNamespace(
        username="alice", hostname="", nickname="", host="gateway", port=22
    )
    assert connection_display.format_connection_host_display(connection) == (
        "localized:alice@gateway (alias)"
    )

    monkeypatch.setattr(
        forwarding_only_ui, "_", lambda msgid: f"localized:{msgid}"
    )
    assert forwarding_only_ui.format_destination_endpoint(
        {"type": "dynamic", "listen_port": 1080}
    ) == "localized:dynamic"

    monkeypatch.setenv("SSHPILOT_PASSWORD_USER", "alice")
    monkeypatch.setenv("SSHPILOT_PASSWORD_HOSTS", "server.example")
    monkeypatch.setattr(
        askpass_utils,
        "_",
        lambda msgid: "localized:{user}@{host}'s password:",
    )
    assert askpass_utils._standalone_password_label() == (
        "localized:alice@server.example's password:"
    )


def test_sftp_frontend_controller_localizes_stable_fallbacks_only():
    backend_source = (
        ROOT / "src/sshpilot/daemon_sftp_backend.py"
    ).read_text(encoding="utf-8")
    controller_source = (
        ROOT / "src/sshpilot/sftp_service_controller.py"
    ).read_text(encoding="utf-8")

    assert 'message = _("The SFTP connection was lost")' in backend_source
    assert 'else _("SFTP failed")' in controller_source
    assert 'message = _("The SFTP service was closed")' in controller_source
    assert 'message = _(str(error))' not in backend_source
    assert 'message = _(str(error))' not in controller_source


def test_omni_search_localizes_cli_context_before_formatting(monkeypatch):
    from sshpilot import omni_search

    msgids = []
    monkeypatch.setattr(
        omni_search,
        "_",
        lambda msgid: msgids.append(msgid) or f"localized:{msgid}",
    )

    assert omni_search._format_cli_validation_error(
        "Could not parse SSH destination: ssh bad"
    ) == "localized:Could not parse SSH destination: ssh bad"
    assert msgids == ["Could not parse SSH destination: {command}"]

    msgids.clear()
    opaque = "vendor parser rejected token 7"
    assert omni_search._format_cli_validation_error(opaque) == opaque
    assert msgids == []


def test_forwarding_validation_localizes_known_copy_and_preserves_unknown_details(
    monkeypatch,
):
    from sshpilot import connection_dialog_port_forwarding as forwarding

    msgids = []
    monkeypatch.setattr(
        forwarding,
        "_",
        lambda msgid: msgids.append(msgid) or f"localized:{msgid}",
    )

    assert forwarding._format_forwarding_validation_error(
        "Listen port must be a number"
    ) == "localized:Listen port must be a number"
    assert forwarding._format_forwarding_validation_error(
        "Unsupported forwarding type: vendor-mode"
    ) == "localized:Unsupported forwarding type: vendor-mode"
    opaque = "vendor validator rejected field 7"
    assert forwarding._format_forwarding_validation_error(opaque) == opaque
    assert opaque not in msgids


def test_runtime_gettext_python_sources_are_in_potfiles():
    potfiles = {
        line.strip()
        for line in (ROOT / "po/POTFILES").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    missing = []
    for source in (ROOT / "src/sshpilot").rglob("*.py"):
        relative = source.relative_to(ROOT).as_posix()
        if "/plugins/examples/" in f"/{relative}":
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"))
        uses_gettext = any(
            isinstance(node, ast.ImportFrom)
            and node.module is not None
            and (
                node.module == "gettext"
                or node.module == "i18n"
                or node.module.endswith(".i18n")
            )
            and any(
                (alias.asname or alias.name) in {"_", "N_", "ngettext"}
                for alias in node.names
            )
            for node in ast.walk(tree)
        )
        if uses_gettext and relative not in potfiles:
            missing.append(relative)
    assert missing == []


def test_frontend_gettext_never_wraps_an_f_string():
    violations = []
    for source in (ROOT / "src/sshpilot").rglob("*.py"):
        relative = source.relative_to(ROOT).as_posix()
        if "/plugins/examples/" in f"/{relative}":
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_"
                and node.args
                and isinstance(node.args[0], ast.JoinedStr)
            ):
                violations.append(f"{relative}:{node.lineno}")
    assert violations == []
