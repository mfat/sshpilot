from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.common import ConnectionId, SessionId
from sshpilot.api.models.interactions import (
    InteractionType,
    RememberPolicy,
    SecretDecision,
)
from sshpilot.daemon.privileged_file_service import PrivilegedFileService

CONNECTION_ID = ConnectionId("demo")
SCOPE_ID = SessionId("sftp:demo")


class _Process:
    def __init__(self, argv, kwargs, script):
        self.argv = list(argv)
        self.kwargs = kwargs
        self.script = script
        self.returncode = 1

    def communicate(self, data=None, timeout=None):
        del timeout
        returncode, stdout, stderr = self.script(list(self.argv), data)
        self.returncode = returncode
        return stdout, stderr

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        del timeout
        return self.returncode


class _Provider:
    def __init__(self):
        self.calls = []

    def prepare_remote_command_launch(self, connection_id, remote_command):
        self.calls.append((connection_id, remote_command))
        return ("ssh", connection_id, remote_command), {"PATH": "/usr/bin"}


class _BrokerResult:
    def __init__(self, decision=SecretDecision.SUBMIT, secret=None, remember_policy=None):
        self.decision = decision
        self.secret = secret
        self.remember_policy = remember_policy or RememberPolicy.DO_NOT_STORE
        self.cleared = False

    def clear(self):
        self.cleared = True
        self.secret = None


class _Broker:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.prepared = []
        self.created = []
        self._counter = 0

    def prepare_operation_launch(self, argv, environment, **kwargs):
        self.prepared.append((tuple(argv), dict(environment), kwargs))
        env = dict(environment)
        env["SSH_ASKPASS"] = "/private/helper"
        return tuple(argv), env

    def create(self, **kwargs):
        self.created.append(kwargs)
        self._counter += 1
        return SimpleNamespace(id=f"interaction:{self._counter}")

    def wait_for_result(self, _interaction_id):
        return self.responses.pop(0) if self.responses else None


def _popen_factory(script):
    calls = []

    def popen(argv, **kwargs):
        calls.append((list(argv), dict(kwargs)))
        return _Process(argv, kwargs, script)

    return calls, popen


def _service(script, *, broker=None, lookup=None, store=None, clear=None, max_attempts=3):
    calls, popen = _popen_factory(script)
    broker = broker or _Broker()
    service = PrivilegedFileService(
        _Provider(),
        broker,
        popen=popen,
        environ={"PATH": "/usr/bin"},
        max_password_attempts=max_attempts,
        secret_lookup=lookup or (lambda _host, _user: ""),
        secret_store=store or (lambda _host, _user, _password: True),
        secret_clear=clear or (lambda _host, _user: True),
    )
    return service, calls, broker


def _read(service, path="/etc/something"):
    return service.read(
        connection_id=CONNECTION_ID,
        scope_id=SCOPE_ID,
        hostname="example.test",
        username="alice",
        port=22,
        path=path,
    )


def _replace(service, path="/etc/something", payload=b"new\n", expected_revision=None, backup=True):
    expected_revision = expected_revision or hashlib.sha256(b"old\n").hexdigest()
    return service.replace(
        connection_id=CONNECTION_ID,
        scope_id=SCOPE_ID,
        hostname="example.test",
        username="alice",
        port=22,
        path=path,
        payload=payload,
        expected_revision=expected_revision,
        backup=backup,
    )


def test_passwordless_read_success():
    def script(_argv, _data):
        return 0, b"root content\n", b""

    service, calls, broker = _service(script)
    result = _read(service)

    assert result.content == b"root content\n"
    assert result.exists is True
    assert result.mode is None
    assert broker.created == []
    assert len(calls) == 1
    assert "sudo -n -- cat" in calls[0][0][-1]


def test_read_missing_file_reports_not_exists():
    def script(_argv, _data):
        return 1, b"", b"cat: /etc/missing: No such file or directory"

    service, _, _ = _service(script)
    result = _read(service, "/etc/missing")
    assert result.exists is False
    assert result.content == b""


def test_passwordless_denied_raises_permission_denied():
    def script(_argv, _data):
        return 1, b"", b"alice is not in the sudoers file"

    service, _, _ = _service(script)
    with pytest.raises(SshPilotError) as raised:
        _read(service)
    assert raised.value.code is ErrorCode.REMOTE_PERMISSION_DENIED


def test_ssh_exit_255_is_typed_failure():
    def script(_argv, _data):
        return 255, b"", b"ssh: Connection to example.test closed"

    service, _, _ = _service(script)
    with pytest.raises(SshPilotError) as raised:
        _read(service)
    assert raised.value.code is ErrorCode.REMOTE_COMMAND_FAILED


def test_stored_password_read_succeeds_and_secret_stays_off_wire():
    seen_stdin = []
    seen_argv_env = []

    def script(argv, data):
        seen_argv_env.append(" ".join(argv))
        seen_stdin.append(data)
        if "sudo -n" in " ".join(argv):
            return 1, b"", b"sudo: a password is required"
        return 0, b"root content\n", b""

    cleared = []

    def clear(_host, _user):
        cleared.append(1)
        return True

    service, calls, broker = _service(script, lookup=lambda _h, _u: "storedpw", clear=clear)
    result = _read(service)

    assert result.content == b"root content\n"
    assert seen_stdin == [None, b"storedpw\n"]
    assert cleared == []
    assert broker.created == []
    assert not any("storedpw" in part for call in calls for part in call[0])


def test_wrong_stored_password_is_cleared_and_reprompted():
    attempts = {"badpw": 0}

    def script(argv, data):
        joined = " ".join(argv)
        if "sudo -n" in joined:
            return 1, b"", b"sudo: a password is required"
        if attempts["badpw"] == 0:
            attempts["badpw"] = 1
            assert data == b"badpw\n"
            return 1, b"", b"Sorry, try again."
        return 0, b"root content\n", b""

    cleared = []

    def clear(_host, _user):
        cleared.append(1)
        return True

    broker = _Broker(
        [
            _BrokerResult(secret=b"goodpw", remember_policy=RememberPolicy.DO_NOT_STORE),
        ]
    )
    service, _, _ = _service(script, broker=broker, lookup=lambda _h, _u: "badpw", clear=clear)
    result = _read(service)

    assert result.content == b"root content\n"
    assert cleared == [1]
    assert len(broker.created) == 1
    assert broker.created[0]["interaction_type"] is InteractionType.PASSWORD


def test_cancelled_prompt_raises_operation_cancelled():
    def script(argv, data):
        if "sudo -n" in " ".join(argv):
            return 1, b"", b"sudo: a password is required"
        raise AssertionError("a password attempt should not run after cancellation")

    broker = _Broker([_BrokerResult(decision=SecretDecision.CANCEL)])
    service, _, _ = _service(script, broker=broker)
    with pytest.raises(SshPilotError) as raised:
        _read(service)
    assert raised.value.code is ErrorCode.OPERATION_CANCELLED


def test_prompt_without_secret_is_treated_as_cancelled():
    def script(argv, data):
        if "sudo -n" in " ".join(argv):
            return 1, b"", b"sudo: a password is required"
        raise AssertionError("a password attempt should not run")

    broker = _Broker([_BrokerResult(secret=None)])
    service, _, _ = _service(script, broker=broker)
    with pytest.raises(SshPilotError) as raised:
        _read(service)
    assert raised.value.code is ErrorCode.OPERATION_CANCELLED


def test_attempts_exhausted_after_max_prompts():
    def script(argv, data):
        if "sudo -n" in " ".join(argv):
            return 1, b"", b"sudo: a password is required"
        return 1, b"", b"Sorry, try again."

    broker = _Broker([_BrokerResult(secret=b"pw") for _ in range(3)])
    service, _, broker_used = _service(script, broker=broker, max_attempts=3)
    with pytest.raises(SshPilotError) as raised:
        _read(service)
    assert raised.value.code is ErrorCode.AUTHENTICATION_ATTEMPTS_EXHAUSTED
    assert len(broker_used.created) == 3


def test_remember_after_success_stores_password():
    def script(argv, data):
        if "sudo -n" in " ".join(argv):
            return 1, b"", b"sudo: a password is required"
        return 0, b"root content\n", b""

    stored = []

    def store(_host, _user, password):
        stored.append(password)
        return True

    broker = _Broker(
        [_BrokerResult(secret=b"supersecret", remember_policy=RememberPolicy.STORE_AFTER_SUCCESS)]
    )
    service, _, _ = _service(script, broker=broker, store=store)
    _read(service)
    assert stored == ["supersecret"]


def test_replace_rejects_stale_revision_before_write():
    wrote = []

    def script(argv, data):
        if "sudo -n -- cat" in " ".join(argv):
            return 0, b"changed-elsewhere\n", b""
        wrote.append((argv, data))
        return 0, b"", b""

    service, _, _ = _service(script)
    with pytest.raises(SshPilotError) as raised:
        _replace(service, expected_revision=hashlib.sha256(b"old\n").hexdigest())
    assert raised.value.code is ErrorCode.FILE_REVISION_CONFLICT
    assert wrote == []


def test_replace_success_preserves_revision_size_and_backup():
    payload = b"root-wrote\n"

    def script(argv, data):
        joined = " ".join(argv)
        if "sudo -n -- cat" in joined:
            return 0, b"old\n", b""
        if "cp -a" in joined:
            return 0, b"", b""
        if "-- tee" in joined:
            assert data == payload
            return 0, b"", b""
        raise AssertionError(f"unexpected command: {joined}")

    service, calls, broker = _service(script)
    result = _replace(service, payload=payload)

    assert result.revision == hashlib.sha256(payload).hexdigest()
    assert result.size == len(payload)
    assert result.backup_path and result.backup_path.startswith("/etc/something.bak-")
    assert broker.created == []
    assert len(calls) == 3


def test_replace_without_backup_skips_backup_command():
    payload = b"root-wrote\n"

    def script(argv, data):
        joined = " ".join(argv)
        if "sudo -n -- cat" in joined:
            return 0, b"old\n", b""
        if "-- tee" in joined:
            return 0, b"", b""
        raise AssertionError(f"unexpected command: {joined}")

    service, calls, _ = _service(script)
    result = _replace(service, payload=payload, backup=False)
    assert result.backup_path is None
    assert len(calls) == 2


def test_replace_missing_remote_file_refuses():
    def script(argv, data):
        joined = " ".join(argv)
        if "sudo -n -- cat" in joined:
            return 1, b"", b"cat: /etc/something: No such file or directory"
        raise AssertionError("a write should not run for a missing file")

    service, _, _ = _service(script)
    with pytest.raises(SshPilotError) as raised:
        _replace(service, expected_revision="absent")
    assert raised.value.code is ErrorCode.FILE_REVISION_CONFLICT


def test_command_timeout_raises_operation_timed_out():
    class _HangingProcess(_Process):
        def communicate(self, data=None, timeout=None):
            del data, timeout
            raise __import__("subprocess").TimeoutExpired("ssh", 30.0)

    def popen(argv, **kwargs):
        return _HangingProcess(argv, kwargs, lambda _a, _d: (0, b"", b""))

    service = PrivilegedFileService(
        _Provider(),
        _Broker(),
        popen=popen,
        environ={"PATH": "/usr/bin"},
        command_timeout=30.0,
        secret_lookup=lambda _h, _u: "",
        secret_store=lambda _h, _u, _p: True,
        secret_clear=lambda _h, _u: True,
    )
    with pytest.raises(SshPilotError) as raised:
        _read(service)
    assert raised.value.code is ErrorCode.OPERATION_TIMED_OUT


def test_popen_failure_is_typed_startup_error():
    def popen(argv, **kwargs):
        raise OSError("no such binary")

    service = PrivilegedFileService(
        _Provider(),
        _Broker(),
        popen=popen,
        environ={"PATH": "/usr/bin"},
        secret_lookup=lambda _h, _u: "",
        secret_store=lambda _h, _u, _p: True,
        secret_clear=lambda _h, _u: True,
    )
    with pytest.raises(SshPilotError) as raised:
        _read(service)
    assert raised.value.code is ErrorCode.SESSION_STARTUP_FAILED


def test_constructor_rejects_missing_launch_provider_or_broker():
    with pytest.raises(TypeError):
        PrivilegedFileService(None, _Broker())
    with pytest.raises(TypeError):
        PrivilegedFileService(_Provider(), None)


def test_constructor_rejects_nonpositive_timeout_or_attempts():
    with pytest.raises(ValueError):
        PrivilegedFileService(_Provider(), _Broker(), command_timeout=0)
    with pytest.raises(ValueError):
        PrivilegedFileService(_Provider(), _Broker(), max_password_attempts=0)


def test_username_and_hostname_reach_prompt_and_launch():
    def script(argv, data):
        if "sudo -n" in " ".join(argv):
            return 1, b"", b"sudo: a password is required"
        return 0, b"ok\n", b""

    broker = _Broker([_BrokerResult(secret=b"pw")])
    service, _, broker_used = _service(script, broker=broker)
    service.read(
        connection_id=CONNECTION_ID,
        scope_id=SCOPE_ID,
        hostname="host.example",
        username="bob",
        port=2222,
        path="/etc/file",
    )
    created = broker_used.created[0]
    assert created["prompt"].hostname == "host.example"
    assert created["prompt"].username == "bob"
    assert created["prompt"].port == 2222
    assert broker_used.created[0]["attempt"] == 1
