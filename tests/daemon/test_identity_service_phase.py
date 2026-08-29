from __future__ import annotations

import io
from types import SimpleNamespace
import time

import pytest

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.identity import (
    DeployKeyRequest,
    ListAuthorizedKeysRequest,
)
from sshpilot.api.models.keys import KeyStoreScope, KeySummary, KeyList
from sshpilot.daemon.identity_service import DaemonIdentityService
from sshpilot.daemon.identity_service import (
    _deploy_failure_code,
    _identity_launch_failure_code,
)
from sshpilot.api.models.operations import IdentityFailureCode
from sshpilot.daemon.operation_runtime import OperationRuntime
from sshpilot.core.identity_service import IdentityStateService


class _Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = io.StringIO(stdout)
        self.stderr = stderr

    def communicate(self, *_args, **_kwargs):
        return self.stdout.getvalue(), self.stderr

    def wait(self, *_args, **_kwargs):
        return self.returncode

    def terminate(self):
        return None

    def kill(self):
        return None


@pytest.mark.parametrize(
    ("diagnostic", "code"),
    (
        ("Permission denied (publickey)", IdentityFailureCode.AUTHENTICATION_FAILED),
        ("connect: Connection refused", IdentityFailureCode.CONNECTION_REFUSED),
        ("ssh: No route to host", IdentityFailureCode.SERVER_UNREACHABLE),
        ("ssh: Name or service not known", IdentityFailureCode.SERVER_UNREACHABLE),
        ("Connection timed out", IdentityFailureCode.CONNECTION_TIMED_OUT),
        ("vendor exit status 23", IdentityFailureCode.INSTALLATION_FAILED),
    ),
)
def test_ssh_copy_id_diagnostic_classification_is_structured(diagnostic, code):
    assert _deploy_failure_code(diagnostic) is code


@pytest.mark.parametrize(
    ("error_code", "failure_code"),
    (
        (ErrorCode.CONNECTION_NOT_FOUND, IdentityFailureCode.CONNECTION_NOT_FOUND),
        (
            ErrorCode.UNSUPPORTED_SESSION_PROTOCOL,
            IdentityFailureCode.SSH_CONNECTION_REQUIRED,
        ),
        (ErrorCode.VALIDATION_FAILED, IdentityFailureCode.HOST_IDENTIFIER_MISSING),
        (
            ErrorCode.UNSUPPORTED_CAPABILITY,
            IdentityFailureCode.SSH_COPY_ID_UNAVAILABLE,
        ),
        (
            ErrorCode.INTERNAL_ERROR,
            IdentityFailureCode.LAUNCH_PREPARATION_UNAVAILABLE,
        ),
    ),
)
def test_ssh_copy_id_launch_errors_use_stable_error_codes(
    error_code, failure_code
):
    assert _identity_launch_failure_code(error_code) is failure_code


class _KeyService:
    def list_keys(self, _request):
        return KeyList(
            keys=(
                KeySummary(
                    key_id="key:one",
                    name="id_ed25519",
                    private_path="/home/test/.ssh/id_ed25519",
                    public_path="/home/test/.ssh/id_ed25519.pub",
                    public_key_available=True,
                ),
            )
        )

    def read_public_key(self, request):
        assert request.key_id == "key:one"
        return SimpleNamespace(text="ssh-ed25519 AAAA test\n")


class _Provider:
    def __init__(self):
        self.calls = []

    def prepare_copy_id_launch(self, connection_id, public_path, *, force=False):
        self.calls.append((connection_id, public_path, force))
        return ["ssh-copy-id", "-i", public_path, "HostAlias"], {"PATH": "/usr/bin"}

    def prepare_remote_command_launch(self, connection_id, command):
        return ["ssh", connection_id, command], {"PATH": "/usr/bin"}


def _service(tmp_path, popen, *, state_environ=None, base_environ=None):
    state = IdentityStateService(
        tmp_path / "identity.json",
        environ={"PATH": "/usr/bin", **(state_environ or {})},
        ssh_copy_id_probe=lambda: True,
    )
    return DaemonIdentityService(
        state,
        _KeyService(),
        OperationRuntime(),
        launch_provider=_Provider(),
        popen=popen,
        environ={"PATH": "/usr/bin", **(base_environ or {})},
    )


def test_agent_empty_is_not_unavailable(tmp_path):
    calls = []

    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return _Completed(returncode=1)

    service = _service(tmp_path, popen)
    result = service.list_agent_keys()
    assert result.keys == ()
    assert calls[0][0] == ["ssh-add", "-l"]
    assert calls[0][1]["env"]["PATH"] == "/usr/bin"


def test_agent_failure_is_typed(tmp_path):
    service = _service(tmp_path, lambda *_args, **_kwargs: _Completed(returncode=2))
    with pytest.raises(SshPilotError) as raised:
        service.list_agent_keys()
    assert raised.value.details["code"] == "agent_unavailable"


def test_provider_agent_keys_scope_to_named_provider_not_selection(tmp_path):
    """A provider-scoped listing runs ``ssh-add -l`` against the *named*
    provider's agent environment, not the currently selected one."""
    calls = []

    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return _Completed(
            returncode=0,
            stdout="256 SHA256:abc123 ed25519 user@host (ED25519)\n",
        )

    service = _service(
        tmp_path,
        popen,
        state_environ={"SSH_AUTH_SOCK": "/run/user/1000/agent.sock"},
    )
    result = service.list_provider_agent_keys("auto")
    assert len(result.keys) == 1
    assert result.keys[0].fingerprint == "SHA256:abc123"
    assert calls[0][0] == ["ssh-add", "-l"]
    # The system-agent provider forwards the daemon's inherited SSH_AUTH_SOCK.
    assert calls[0][1]["env"]["SSH_AUTH_SOCK"] == "/run/user/1000/agent.sock"


def test_provider_agent_keys_use_fixed_socket_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", "/home/test")
    calls = []

    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return _Completed(returncode=0, stdout="")

    service = _service(tmp_path, popen)
    result = service.list_provider_agent_keys("onepassword")
    assert result.keys == ()
    assert calls[0][0] == ["ssh-add", "-l"]
    assert calls[0][1]["env"]["SSH_AUTH_SOCK"] == "/home/test/.1password/agent.sock"
    assert "SSH_AGENT_PID" not in calls[0][1]["env"]


def test_provider_agent_keys_unavailable_is_typed(tmp_path):
    service = _service(tmp_path, lambda *_args, **_kwargs: _Completed(returncode=2))
    with pytest.raises(SshPilotError) as raised:
        service.list_provider_agent_keys("auto")
    assert raised.value.details["code"] == "agent_unavailable"


def test_provider_agent_keys_requires_provider_id(tmp_path):
    service = _service(tmp_path, lambda *_args, **_kwargs: _Completed())
    with pytest.raises(TypeError):
        service.list_provider_agent_keys("")


def test_ssh_copy_id_deployment_registers_operation(tmp_path):
    calls = []

    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return _Completed(returncode=0, stdout="Number of key(s) added: 1\n")

    service = _service(tmp_path, popen)
    summary = service.deploy_key(
        DeployKeyRequest("HostAlias", "key:one", KeyStoreScope.DEFAULT, force=True)
    )
    assert summary.operation_id
    for _ in range(100):
        if calls:
            break
        time.sleep(0.01)
    operation = service._operations.get_operation(summary.operation_id)
    assert operation.kind.value == "key_deployment"
    assert calls
    assert calls[0][0][0] == "ssh-copy-id"
    assert "-i" in calls[0][0]
    assert calls[0][1]["start_new_session"] is True
    service._operations.shutdown()


def test_authorized_key_request_keeps_host_alias_opaque(tmp_path):
    _service(tmp_path, lambda *_args, **_kwargs: _Completed())
    request = ListAuthorizedKeysRequest("host;touch /tmp/unsafe")
    assert request.connection_id == "host;touch /tmp/unsafe"
    from sshpilot.daemon import identity_service
    assert "cat" in identity_service._REMOTE_READ_COMMAND
