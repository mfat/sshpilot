"""Opening or saving the connection editor never reads a saved secret.

The editor asks only whether a password or passphrase is saved, and only an
unlocked secure storage; the secret itself is fetched by Reveal alone, and
Replace/Remove are applied on Save.
"""
from __future__ import annotations

import types

import pytest

import sshpilot.connection_dialog_secrets as secrets_module
from sshpilot.connection_dialog import ConnectionDialog, FileListEditor
from sshpilot.connection_dialog_secrets import (
    PLACEHOLDER,
    SavedSecretField,
    SecretStatus,
    check_status,
    planned_operation,
)


class Entry:
    def __init__(self, text=""):
        self.text = text
        self.properties = {}
        self.handlers = []

    def get_text(self):
        return self.text

    def set_text(self, text):
        self.text = text
        for handler in self.handlers:
            handler(self)

    def connect(self, _signal, handler):
        self.handlers.append(handler)

    def set_position(self, _position):
        pass

    def set_property(self, name, value):
        self.properties[name] = value

    def add_css_class(self, _name):
        pass

    def remove_css_class(self, _name):
        pass


class Client:
    """A daemon client that records every secret-related call."""

    def __init__(self, *, locked=False, login_required=False, saved=True,
                 state_error=False):
        self.locked = locked
        self.login_required = login_required
        self.saved = saved
        self.state_error = state_error
        self.calls = []

    def get_secret_state(self):
        self.calls.append("state")
        if self.state_error:
            raise RuntimeError("no secure storage")
        return types.SimpleNamespace(
            needs_unlock=self.locked, login_required=self.login_required
        )

    def has_connection_password(self, connection_id):
        self.calls.append(("has_password", connection_id))
        return self.saved

    def has_key_passphrase(self, key_path):
        self.calls.append(("has_passphrase", key_path))
        return self.saved

    def reveal_connection_password(self, connection_id):
        self.calls.append(("reveal_password", connection_id))
        return bytearray(b"hunter2")

    def reveal_key_passphrase(self, key_path):
        self.calls.append(("reveal_passphrase", key_path))
        return bytearray(b"key-secret")

    def _forbidden(self, *args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("a secret was written or the vault unlocked")

    store_connection_password = delete_connection_password = _forbidden
    store_key_passphrase = delete_key_passphrase = unlock_secrets = _forbidden


@pytest.fixture
def inline(monkeypatch):
    """Run worker threads and idle callbacks at once."""

    class Thread:
        def __init__(self, target, daemon=False, **_kw):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(secrets_module.threading, "Thread", Thread)
    monkeypatch.setattr(
        secrets_module, "GLib",
        types.SimpleNamespace(idle_add=lambda callback, *args: callback(*args)),
    )


def _field(entry, client, kind, unlock=None, prompts=None):
    prompts = prompts if prompts is not None else []

    def default_unlock(start, declined):
        prompts.append("unlock")
        start()

    if kind == "password":
        check = lambda: check_status(client, lambda: client.has_connection_password("web"))
        reveal = lambda: client.reveal_connection_password("web").decode()
    else:
        check = lambda: check_status(client, lambda: client.has_key_passphrase("/k"))
        reveal = lambda: client.reveal_key_passphrase("/k").decode()
    return SavedSecretField(
        entry, kind=kind, check=check, reveal=reveal,
        unlock=unlock or default_unlock,
    )


# -- status ---------------------------------------------------------------------

def test_a_locked_vault_is_not_asked_and_has_its_own_status():
    client = Client(locked=True)
    assert check_status(client, lambda: client.has_connection_password("web")) \
        is SecretStatus.LOCKED
    assert client.calls == ["state"]
    signed_out = Client(login_required=True)
    assert check_status(signed_out, lambda: signed_out.has_key_passphrase("/k")) \
        is SecretStatus.LOCKED
    assert signed_out.calls == ["state"]


def test_an_unlocked_vault_says_saved_or_none():
    assert check_status(Client(saved=True), lambda: True) is SecretStatus.SAVED
    assert check_status(Client(saved=False), lambda: False) is SecretStatus.NONE


def test_an_unreachable_vault_is_unavailable_not_empty():
    assert check_status(Client(state_error=True), lambda: True) \
        is SecretStatus.UNAVAILABLE

    def broken():
        raise RuntimeError("backend failed")

    assert check_status(Client(), broken) is SecretStatus.UNAVAILABLE


# -- what Save does -------------------------------------------------------------

@pytest.mark.parametrize("text, revealed, remove, expected", [
    ("", None, False, None),                 # never read, left alone
    ("hunter2", "hunter2", False, None),     # revealed, left alone
    ("new", None, False, ("store", "new")),  # Replace
    ("new", "hunter2", False, ("store", "new")),
    ("", None, True, ("delete", "")),        # Remove
    ("", "hunter2", False, ("delete", "")),  # revealed, then erased
    ("new", None, True, ("store", "new")),   # typed after Remove: replaced
])
def test_planned_operation(text, revealed, remove, expected):
    assert planned_operation(text, revealed, remove) == expected


# -- the acceptance case: locked vault, change the port, save --------------------

@pytest.mark.parametrize("kind", ["password", "passphrase"])
def test_opening_with_a_locked_vault_reads_and_unlocks_nothing(inline, kind):
    client = Client(locked=True)
    prompts = []
    entry = Entry()
    field = _field(entry, client, kind, prompts=prompts)

    field.load_status()                      # the dialog opens

    assert field.status is SecretStatus.LOCKED
    assert client.calls == ["state"]         # no presence check, no reveal
    assert prompts == []                     # no unlock prompt
    assert entry.get_text() == ""
    assert field.operation() is None         # ... and Save writes nothing


@pytest.mark.parametrize("kind", ["password", "passphrase"])
def test_opening_with_saved_credentials_only_asks_whether_they_exist(inline, kind):
    client = Client(saved=True)
    entry = Entry()
    field = _field(entry, client, kind)

    field.load_status()

    assert field.status is SecretStatus.SAVED
    assert not any(str(call[0]).startswith("reveal") for call in client.calls[1:])
    # The field looks filled, with dots that are not the secret.
    assert entry.get_text() == PLACEHOLDER
    assert field.value() == "" and field.operation() is None


def test_typing_over_the_dots_replaces_and_erasing_them_removes(inline):
    entry = Entry()
    field = _field(entry, Client(), "password")
    field.load_status()

    entry.set_text(PLACEHOLDER + "n")        # a key pressed after the dots
    assert entry.get_text() == "n"
    assert field.operation() == ("store", "n")

    other = Entry()
    erased = _field(other, Client(), "password")
    erased.load_status()
    other.set_text(PLACEHOLDER[:-1])         # backspace
    assert other.get_text() == "" and erased.remove_pending
    assert erased.operation() == ("delete", "")
    erased.toggle_remove()                   # Undo: the dots are back
    assert other.get_text() == PLACEHOLDER and erased.operation() is None


def test_a_locked_vault_shows_no_dots(inline):
    entry = Entry()
    field = _field(entry, Client(locked=True), "password")
    field.load_status()
    assert entry.get_text() == ""


def test_the_save_gate_asks_for_no_unlock_when_no_secret_changes():
    dialog = ConnectionDialog.__new__(ConnectionDialog)
    dialog.key_editor = types.SimpleNamespace(has_pending_passphrases=lambda: False)

    def no_state():
        raise AssertionError("the vault state is not needed for this save")

    dialog.parent_window = types.SimpleNamespace(
        secrets_controller=types.SimpleNamespace(load_state=no_state)
    )
    assert dialog._needs_secret_unlock_before_save(
        {"password": "", "password_changed": False}
    ) is False
    # A revealed password left as it is writes nothing either.
    assert dialog._needs_secret_unlock_before_save(
        {"password": "hunter2", "password_changed": False}
    ) is False


def test_an_untouched_key_row_with_a_saved_passphrase_plans_nothing():
    editor = FileListEditor.__new__(FileListEditor)
    editor._with_passphrase = True
    editor._verify = None
    row = types.SimpleNamespace(
        _pass_entry=Entry(""), _pass_path="/k", _pass_norm="/k",
        _pass_initial="", _pass_saved=True, _pass_remove_pending=False,
    )
    editor._rows = [row]
    assert editor.has_pending_passphrases() is False
    assert editor.pending_passphrase_operations() == []

    row._pass_remove_pending = True          # Remove
    assert editor.has_pending_passphrases() is True
    assert editor.pending_passphrase_operations() == [("delete", "/k", "")]


# -- Reveal, Replace, Remove ----------------------------------------------------

@pytest.mark.parametrize("kind, secret", [
    ("password", "hunter2"), ("passphrase", "key-secret"),
])
def test_reveal_is_the_only_read_and_unlocks_first(inline, kind, secret):
    client = Client(locked=True)
    prompts = []
    entry = Entry()
    field = _field(entry, client, kind, prompts=prompts)
    field.load_status()

    field.reveal()

    assert prompts == ["unlock"]
    assert entry.get_text() == secret and field.revealed == secret
    assert field.status is SecretStatus.SAVED
    assert field.operation() is None         # showing it changes nothing


def test_a_declined_unlock_reveals_nothing_and_can_be_retried(inline):
    client = Client(locked=True)
    entry = Entry()
    answers = [False, True]

    def unlock(start, declined):
        (start if answers.pop(0) else declined)()

    field = _field(entry, client, "password", unlock=unlock)
    field.load_status()

    field.reveal()
    assert entry.get_text() == "" and field.status is SecretStatus.LOCKED
    assert not any(call[0] == "reveal_password" for call in client.calls[1:])

    field.reveal()
    assert entry.get_text() == "hunter2"


def test_reveal_does_not_overwrite_what_the_user_typed(inline):
    entry = Entry()
    field = _field(entry, Client(), "password")
    field.load_status()
    entry.set_text("typed")
    field.reveal()
    assert entry.get_text() == "typed"
    assert field.operation() == ("store", "typed")


def test_remove_is_pending_until_save_and_can_be_taken_back(inline):
    entry = Entry()
    field = _field(entry, Client(), "password")
    field.load_status()

    field.toggle_remove()
    assert field.operation() == ("delete", "")
    field.toggle_remove()                    # undo; Cancel does the same by closing
    assert field.operation() is None


def test_the_editor_checks_status_through_the_daemon_only():
    dialog = ConnectionDialog.__new__(ConnectionDialog)
    client = Client(locked=True)
    dialog.connection = types.SimpleNamespace(
        username="alice", nickname="web", id="web", protocol="ssh",
    )
    dialog.parent_window = types.SimpleNamespace(
        client=client, _daemon_ready=lambda: True
    )
    assert dialog._check_saved_password() is SecretStatus.LOCKED
    assert client.calls == ["state"]

    dialog.parent_window = types.SimpleNamespace(
        client=client, _daemon_ready=lambda: False
    )
    assert dialog._check_saved_password() is SecretStatus.UNAVAILABLE
