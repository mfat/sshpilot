"""Guards for the WindowConfigDialogsMixin extraction.

The known-hosts editor, preferences launcher, and config export/import methods
were moved verbatim out of window.py into sshpilot/window_dialogs.py as a mixin.
These checks ensure every method resolves to the mixin module (so no stray copy
in the window.py body silently shadows it) and the mixin is wired into the MRO.
"""

import sys
import types


def _window_module():
    if 'cairo' not in sys.modules:
        sys.modules['cairo'] = types.SimpleNamespace()
    from sshpilot import window as window_module
    return window_module


_DIALOG_METHODS = (
    "show_known_hosts_editor",
    "show_preferences",
    "show_export_dialog",
    "show_import_dialog",
    "_show_import_mode_dialog",
)


def test_config_dialog_methods_resolve_to_mixin_module():
    wm = _window_module()
    for name in _DIALOG_METHODS:
        method = getattr(wm.MainWindow, name)
        assert method.__module__ == "sshpilot.window_dialogs", (
            f"{name} resolved to {method.__module__}, expected the mixin — a stray "
            "copy in window.py is shadowing it"
        )


def test_config_dialogs_mixin_in_mro():
    wm = _window_module()
    mro_names = [c.__name__ for c in wm.MainWindow.__mro__]
    assert "WindowConfigDialogsMixin" in mro_names


# ---------------------------------------------------------------------------
# SSH-server backups: the prompts the daemon raises while connecting
# ---------------------------------------------------------------------------


class _FakeSubscription:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _FakeClient:
    """Just enough daemon client for the interaction presenter to subscribe."""

    def __init__(self):
        self.subscriptions = []

    def subscribe_events(self, _callback):
        subscription = _FakeSubscription()
        self.subscriptions.append(subscription)
        return subscription

    def list_interactions(self):
        return []


class _SyncBridge:
    def submit_interaction(self, operation, *, on_success, on_error, on_discard=None):
        try:
            result = operation()
        except BaseException as exc:  # pragma: no cover - defensive
            on_error(exc)
        else:
            on_success(result)
        return None


def _ssh_backup_window(controller):
    """A window stub carrying only what the SSH-backup flows touch."""
    wm = _window_module()
    from sshpilot.window_dialogs import WindowConfigDialogsMixin

    class _Window(WindowConfigDialogsMixin):
        def __init__(self):
            self.client = _FakeClient()
            self.client_bridge = _SyncBridge()
            self.secrets_controller = controller
            self.dialogs = []

        def _simple_dialog(self, heading, body):
            self.dialogs.append((heading, body))

        def _run_after_vault_unlock_for_secrets(self, run, **_kwargs):
            run()

        @staticmethod
        def _connection_ids_for(connections):
            return list(connections or [])

    assert wm is not None
    return _Window()


def _patch_backup_flow(monkeypatch):
    """Run the flow's spinner, thread and idle callback inline."""
    from sshpilot import bitwarden_backup_setup, window_dialogs

    monkeypatch.setattr(
        bitwarden_backup_setup,
        "progress_dialog",
        lambda *args, **kwargs: (lambda *_a: None, lambda *_a: None),
    )
    monkeypatch.setattr(
        window_dialogs.GLib, "idle_add", lambda fn, *args: fn(*args)
    )

    class _InlineThread:
        def __init__(self, *, target, daemon=False):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(
        window_dialogs, "threading", types.SimpleNamespace(Thread=_InlineThread)
    )


def test_ssh_export_presents_the_servers_prompts_and_closes_them(monkeypatch):
    """The daemon connects to the backup server itself, so its password /
    passphrase / host-key prompts arrive with no session scope the frontend
    could bind to. The export must have a presenter open across the call —
    without one the prompt is never claimed and the export dies at the
    interaction timeout — and must close it once the call returns.
    """
    _patch_backup_flow(monkeypatch)
    seen = {}

    class _Controller:
        def export_backup(self, **kwargs):
            seen["subscribed"] = [
                s for s in window.client.subscriptions if not s.closed
            ]
            seen["destination"] = kwargs["destination"]
            return types.SimpleNamespace(
                status=types.SimpleNamespace(value="success"),
                counts={},
                message=None,
            )

    window = _ssh_backup_window(_Controller())
    target = types.SimpleNamespace(nickname="backup-server", hostname="10.0.0.9")

    window._export_to_ssh_server([], {"app_settings": True}, None, target,
                                 "~/sshpilot-backups")

    assert seen["destination"] == "ssh:backup-server:~/sshpilot-backups"
    assert len(seen["subscribed"]) == 1, "no presenter was listening during the export"
    assert all(s.closed for s in window.client.subscriptions), (
        "the presenter outlived the export call"
    )


def test_ssh_backup_listing_presents_the_servers_prompts(monkeypatch):
    """Listing connects too, so it needs the same presenter as the export."""
    _patch_backup_flow(monkeypatch)
    seen = {}

    class _Controller:
        def list_ssh_backups(self, **_kwargs):
            seen["subscribed"] = [
                s for s in window.client.subscriptions if not s.closed
            ]
            return []

    window = _ssh_backup_window(_Controller())
    target = types.SimpleNamespace(nickname="backup-server", hostname="10.0.0.9")

    window._ssh_import_list_backups(target, "~/sshpilot-backups")

    assert len(seen["subscribed"]) == 1, "no presenter was listening during the listing"
    assert all(s.closed for s in window.client.subscriptions)


def test_secrets_persist_check_never_blocks_on_daemon_rpc():
    """Presenting an SSH password prompt must not issue a blocking state RPC.

    Regression for SSH-server backup export to a host with no saved password
    (e.g. ``localhost``): the daemon holds the secret-service lock across its
    connect, whose password/host-key prompts are presented here. A synchronous
    ``load_state()`` query then deadlocks against that very operation — no
    prompt is shown until the export times out and releases the lock, so the
    password dialog only appears after the failure.
    """
    from sshpilot import window_dialogs

    calls = []

    class _Controller:
        def state(self):
            return types.SimpleNamespace(persists_secrets=False)

        def load_state(self):  # must never be called on the GTK thread here
            calls.append(1)
            raise AssertionError("blocking RPC during interaction presentation")

    parent = types.SimpleNamespace(secrets_controller=_Controller())
    assert window_dialogs._secrets_persist_for(parent) is False
    assert calls == []

    class _NoCache:
        def state(self):
            return None

        def load_state(self):
            calls.append(1)
            raise AssertionError("blocking RPC during interaction presentation")

    assert window_dialogs._secrets_persist_for(
        types.SimpleNamespace(secrets_controller=_NoCache())) is True
    assert calls == []
