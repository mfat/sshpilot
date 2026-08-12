from __future__ import annotations

import hashlib
import io
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.common import ConnectionId, SessionId
from sshpilot.api.models.interactions import (
    InteractionType,
    RememberPolicy,
    SecretDecision,
)
from sshpilot.daemon.privileged_file_service import (
    MAX_READ_BYTES,
    PrivilegedFileService,
)

CONNECTION_ID = ConnectionId("demo")
SCOPE_ID = SessionId("sftp:demo")


class _StdinBuffer(io.BytesIO):
    def close(self):
        self._final = self.getvalue()
        super().close()

    def getvalue(self):
        if self.closed:
            return getattr(self, "_final", b"")
        return super().getvalue()


class _Process:
    def __init__(self, argv, kwargs, script):
        self.argv = list(argv)
        self.kwargs = kwargs
        self.script = script
        self.returncode = 1
        self.stdin = _StdinBuffer()
        self._stdout = None
        self._stderr = None
        self._finished = False

    def _finish(self):
        if self._finished:
            return
        data = self.stdin.getvalue() or None
        returncode, stdout, stderr = self.script(list(self.argv), data)
        self.returncode = returncode
        self._stdout = io.BytesIO(stdout)
        self._stderr = io.BytesIO(stderr)
        self._finished = True

    @property
    def stdout(self):
        self._finish()
        return self._stdout

    @property
    def stderr(self):
        self._finish()
        return self._stderr

    def communicate(self, data=None, timeout=None):
        del timeout
        if data is not None:
            self.stdin.write(data)
        return self.stdout.read(), self.stderr.read()

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


def test_password_host_backup_uses_password_flow():
    payload = b"root-wrote\n"
    backup_argv = []

    def script(argv, data):
        joined = " ".join(argv)
        if "sudo -n -- cat" in joined:
            return 0, b"old\n", b""
        if "sudo -n -- sh" in joined:
            return 1, b"", b"sudo: a password is required"
        if "sudo -S -p '' -- sh" in joined and "cp -a" in joined:
            backup_argv.append(joined)
            assert data == b"storedpw\n"
            return 0, b"", b""
        if "-- tee" in joined:
            return 0, b"", b""
        raise AssertionError(f"unexpected command: {joined}")

    service, _, broker = _service(script, lookup=lambda _h, _u: "storedpw")
    result = _replace(service, payload=payload)

    assert result.backup_path and result.backup_path.startswith("/etc/something.bak-")
    assert broker.created == []
    assert len(backup_argv) == 1
    assert "sudo -S -p '' -- sh -c 'cp -a" in backup_argv[0]


def test_backup_failure_is_not_swallowed_and_save_does_not_proceed():
    payload = b"root-wrote\n"
    tee_argv = []

    def script(argv, data):
        joined = " ".join(argv)
        if "sudo -n -- cat" in joined:
            return 0, b"old\n", b""
        if "sudo -n -- sh" in joined:
            return 255, b"", b"ssh: remote command aborted"
        if "-- tee" in joined:
            tee_argv.append(joined)
            return 0, b"", b""
        raise AssertionError(f"unexpected command: {joined}")

    service, _, _ = _service(script)
    with pytest.raises(SshPilotError) as raised:
        _replace(service, payload=payload)
    assert raised.value.code is ErrorCode.REMOTE_COMMAND_FAILED
    assert tee_argv == []


def test_backup_command_failure_is_not_classified_as_password_problem():
    """A genuine remote command failure (e.g. disk full) must not enter the
    sudo password flow: the password is not the problem."""
    def script(argv, data):
        joined = " ".join(argv)
        if "sudo -n -- cat" in joined:
            return 0, b"old\n", b""
        if "sudo -n -- sh" in joined:
            return 1, b"", b"cp: error writing '/etc/something.bak-1': No space left on device"
        raise AssertionError(f"unexpected command: {joined}")

    broker = _Broker([_BrokerResult(secret=b"pw")])
    service, _, broker_used = _service(script, broker=broker)
    with pytest.raises(SshPilotError) as raised:
        _replace(service, payload=b"new\n")
    assert raised.value.code is ErrorCode.REMOTE_COMMAND_FAILED
    assert broker_used.created == []


def test_password_auth_command_failure_is_not_retried_as_wrong_password():
    """When a password-authenticated sudo command itself fails (disk full),
    the failure must be reported once and not retried as a wrong password.

    The backup command resolves the sudo password once and the write reuses
    it through the operation-scoped context, so only a single prompt happens."""
    def script(argv, data):
        joined = " ".join(argv)
        if "sudo -n -- cat" in joined:
            return 0, b"old\n", b""
        if "sudo -n -- sh" in joined:
            return 1, b"", b"sudo: a password is required"
        if "sudo -S -p '' -- sh" in joined:
            assert data == b"pw\n"
            return 0, b"", b""
        if "sudo -n -- tee" in joined:
            return 1, b"", b"sudo: a password is required"
        if "-- tee" in joined:
            assert data == b"pw\nnew\n"
            return 1, b"", b"tee: /etc/something: No space left on device"
        raise AssertionError(f"unexpected command: {joined}")

    broker = _Broker([_BrokerResult(secret=b"pw")])
    service, _, broker_used = _service(script, broker=broker)
    with pytest.raises(SshPilotError) as raised:
        _replace(service, payload=b"new\n")
    assert raised.value.code is ErrorCode.REMOTE_COMMAND_FAILED
    assert len(broker_used.created) == 1


def test_oversized_privileged_read_raises_typed_error_and_kills_process():
    killed = []

    def script(_argv, _data):
        return 0, b"x" * (MAX_READ_BYTES + 1), b""

    class _KillTrackedProcess(_Process):
        def kill(self):
            killed.append(1)
            return super().kill()

    def popen(argv, **kwargs):
        return _KillTrackedProcess(argv, kwargs, script)

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
    assert raised.value.code is ErrorCode.FILE_CONTENT_TOO_LARGE
    assert killed == [1]


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
        def __init__(self, argv, kwargs, script):
            super().__init__(argv, kwargs, script)
            self._hung = False

        @property
        def stdout(self):
            if not self._hung:
                self._hung = True
                raise __import__("subprocess").TimeoutExpired("ssh", 30.0)
            return self._stdout

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


def test_real_subprocess_with_idle_open_stdout_times_out():
    """A child that holds stdout open without producing output must be
    terminated at the deadline instead of blocking the read forever."""
    service = PrivilegedFileService(
        _Provider(),
        _Broker(),
        command_timeout=0.5,
        secret_lookup=lambda _h, _u: "",
        secret_store=lambda _h, _u, _p: True,
        secret_clear=lambda _h, _u: True,
    )
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        service._read_bounded(process, None, MAX_READ_BYTES, CONNECTION_ID)
    assert time.monotonic() - started < 10.0
    assert process.poll() is not None


def test_real_subprocess_filling_stderr_does_not_deadlock():
    """A child that floods stderr while leaving stdout open must not deadlock
    the parent: stdout and stderr are drained concurrently."""
    service = PrivilegedFileService(
        _Provider(),
        _Broker(),
        command_timeout=0.5,
        secret_lookup=lambda _h, _u: "",
        secret_store=lambda _h, _u, _p: True,
        secret_clear=lambda _h, _u: True,
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys,time; sys.stderr.write('x'*131072); sys.stderr.flush(); "
            "time.sleep(60)",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        service._read_bounded(process, None, MAX_READ_BYTES, CONNECTION_ID)
    assert time.monotonic() - started < 10.0
    assert process.poll() is not None


@pytest.mark.parametrize(
    "returncode,stderr,expected",
    [
        (127, b"sh: sudo: command not found", True),
        (127, b"sh: 1: sudo: not found", True),
        (1, b"sudo: sorry, you must have a tty to run sudo", True),
        (1, b"sudo: a password is required", False),
        (1, b"sudo: command not found", True),
        (0, b"", False),
    ],
)
def test_is_sudo_unavailable(returncode, stderr, expected):
    from sshpilot.daemon.privileged_file_service import _is_sudo_unavailable

    assert _is_sudo_unavailable(returncode, stderr) is expected


def test_sudo_missing_read_reports_unsupported_not_missing():
    """'sudo: command not found' (exit 127) must not classify a read as an
    absent file — that would surface an empty editor instead of an error."""
    def script(_argv, _data):
        return 127, b"", b"sh: sudo: command not found"

    service, _, _ = _service(script)
    with pytest.raises(SshPilotError) as raised:
        _read(service)
    assert raised.value.code is ErrorCode.UNSUPPORTED_CAPABILITY


def test_sudo_missing_write_reports_unsupported():
    def script(argv, data):
        joined = " ".join(argv)
        if "sudo -n -- cat" in joined:
            return 0, b"old\n", b""
        if "sudo -n -- tee" in joined:
            return 127, b"", b"sh: sudo: command not found"
        raise AssertionError(f"unexpected command: {joined}")

    service, _, _ = _service(script)
    with pytest.raises(SshPilotError) as raised:
        _replace(service, backup=False)
    assert raised.value.code is ErrorCode.UNSUPPORTED_CAPABILITY


def test_sudo_commands_force_c_locale():
    def script(_argv, _data):
        return 0, b"root content\n", b""

    service, calls, _ = _service(script)
    _read(service)
    assert len(calls) == 1
    command = calls[0][0][-1]
    assert command.startswith("env LC_ALL=C sudo -n")
    assert "cat -- /etc/something" in command


def test_password_host_save_prompts_once_for_read_backup_write():
    """The three sudo invocations of one replace() share one in-memory
    password context, so a password host prompts exactly once per save."""
    stdin_sets = []
    created_broker = _Broker(
        [_BrokerResult(secret=b"pw", remember_policy=RememberPolicy.DO_NOT_STORE)]
    )

    def script(argv, data):
        joined = " ".join(argv)
        if "sudo -n -- cat" in joined:
            return 1, b"", b"sudo: a password is required"
        if "sudo -S -p '' -- cat" in joined:
            assert data == b"pw\n"
            return 0, b"old\n", b""
        if "sudo -n -- sh" in joined:
            return 1, b"", b"sudo: a password is required"
        if "sudo -S -p '' -- sh" in joined:
            stdin_sets.append(data)
            assert data == b"pw\n"
            return 0, b"", b""
        if "sudo -n -- tee" in joined:
            return 1, b"", b"sudo: a password is required"
        if "sudo -S -p '' -- tee" in joined:
            stdin_sets.append(data)
            assert data == b"pw\nnew\n"
            return 0, b"", b""
        raise AssertionError(f"unexpected command: {joined}")

    service, calls, _ = _service(script, broker=created_broker)
    result = _replace(service, payload=b"new\n")

    assert len(created_broker.created) == 1
    assert result.revision == hashlib.sha256(b"new\n").hexdigest()
    assert len(calls) == 6  # 3 operations × (passwordless + password attempt)


def test_password_host_save_with_stored_password_skips_reprompt():
    """A stored sudo password resolves once in the re-read and is then reused
    for the backup/write without extra keyring lookups or prompts."""
    lookups = []

    def script(argv, data):
        joined = " ".join(argv)
        if "sudo -n -- cat" in joined:
            return 1, b"", b"sudo: a password is required"
        if "sudo -S -p '' -- cat" in joined:
            assert data == b"storedpw\n"
            return 0, b"old\n", b""
        if "sudo -n -- sh" in joined:
            return 1, b"", b"sudo: a password is required"
        if "sudo -S -p '' -- sh" in joined:
            assert data == b"storedpw\n"
            return 0, b"", b""
        if "sudo -n -- tee" in joined:
            return 1, b"", b"sudo: a password is required"
        if "sudo -S -p '' -- tee" in joined:
            assert data == b"storedpw\nnew\n"
            return 0, b"", b""
        raise AssertionError(f"unexpected command: {joined}")

    broker = _Broker()
    service, _, broker_used = _service(
        script, broker=broker, lookup=lambda _h, _u: (lookups.append(1) or "storedpw")
    )
    result = _replace(service, payload=b"new\n")

    assert len(lookups) == 1
    assert broker_used.created == []
    assert result.size == len(b"new\n")


def test_wrong_context_password_is_cleared_and_reprompted():
    """A context-cached password that suddenly fails (e.g. changed on the
    host) is evicted from the context and the user re-prompted, never looped."""
    backup_passwordless = {"hit": False}
    backup_stdin = []

    def script(argv, data):
        joined = " ".join(argv)
        if "sudo -n -- cat" in joined:
            return 1, b"", b"sudo: a password is required"
        if "sudo -S -p '' -- cat" in joined:
            assert data == b"pw1\n"
            return 0, b"old\n", b""
        if "sudo -n -- sh" in joined:
            backup_passwordless["hit"] = True
            return 1, b"", b"sudo: a password is required"
        if "sudo -S -p '' -- sh" in joined:
            backup_stdin.append(data)
            if len(backup_stdin) == 1:
                assert data == b"pw1\n"
                return 1, b"", b"Sorry, try again."
            assert data == b"pw2\n"
            return 0, b"", b""
        if "sudo -n -- tee" in joined:
            return 1, b"", b"sudo: a password is required"
        if "sudo -S -p '' -- tee" in joined:
            assert data == b"pw2\nnew\n"
            return 0, b"", b""
        raise AssertionError(f"unexpected command: {joined}")

    broker = _Broker(
        [
            _BrokerResult(secret=b"pw1"),
            _BrokerResult(secret=b"pw2"),
        ]
    )
    service, _, broker_used = _service(script, broker=broker)
    result = _replace(service, payload=b"new\n")

    assert result.revision == hashlib.sha256(b"new\n").hexdigest()
    assert len(broker_used.created) == 2
    assert backup_passwordless["hit"] is True
