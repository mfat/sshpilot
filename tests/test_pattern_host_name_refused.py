"""A pattern is never saved as a connection.

A pattern typed where a connection's name goes is a rule for many hosts, not
a connection: saving one used to write a stray ``Host *`` block that showed
up nowhere. The store refuses it on every route.
"""

import pytest

from sshpilot.api.errors import ErrorCode, SshPilotError, ValidationReason
from sshpilot.api.models.connections import CreateConnectionRequest
from sshpilot.api.transport.codec import create_connection_request_from_wire
from sshpilot.core.connection_application_service import _map_core_error
from sshpilot.core.connections.repository import ConnectionRepository
from sshpilot.core.connections.ssh_config_store import SshConfigStore
from sshpilot.core.errors import CoreError
from sshpilot.core.errors import ErrorCode as CoreErrorCode


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "ssh_config"
    root.write_text("Host existing\n    HostName 10.0.0.1\n")
    repository = ConnectionRepository(
        ssh_store=SshConfigStore(root),
        state_path=tmp_path / "connections.json",
        legacy_config_path=tmp_path / "config.json",
        isolated=False,
    )
    return repository, root


def _ids(repository):
    return sorted(record.id for record in repository.list_records())


@pytest.mark.parametrize("name", ["*", "*.prod", "web?", "!bad"])
def test_a_pattern_is_refused_as_a_connection_name_and_nothing_is_written(repo, name):
    repository, root = repo
    before = root.read_text()
    with pytest.raises(CoreError) as refused:
        repository.create_connection({
            "nickname": name, "hostname": "192.168.1.100", "username": "ubuntu",
            "port": 2222, "protocol": "ssh",
        })
    assert refused.value.code is CoreErrorCode.VALIDATION_ERROR
    assert refused.value.details == {"reason": "host_pattern"}
    assert root.read_text() == before


def test_renaming_a_connection_to_a_pattern_is_refused(repo):
    repository, root = repo
    before = root.read_text()
    record = repository.get_record("existing")
    with pytest.raises(CoreError) as refused:
        repository.update_connection(
            "existing", {"nickname": "*.prod", "protocol": "ssh"},
            expected_generation=record.generation,
        )
    assert refused.value.details == {"reason": "host_pattern"}
    assert root.read_text() == before


def test_the_refusal_reaches_the_frontend_as_a_code():
    error = _map_core_error(CoreError(
        CoreErrorCode.VALIDATION_ERROR, "x", details={"reason": "host_pattern"},
    ))
    assert isinstance(error, SshPilotError)
    assert error.code is ErrorCode.VALIDATION_FAILED
    assert error.details == {"reason": ValidationReason.HOST_PATTERN.value}
    plain = _map_core_error(CoreError(CoreErrorCode.VALIDATION_ERROR, "x"))
    assert not plain.details


def test_a_host_line_of_several_names_is_not_a_create_request():
    """Several hosts on one line are authored in Advanced Configuration."""
    with pytest.raises(ValueError):
        create_connection_request_from_wire({
            "nickname": "web1", "hostname": "", "username": "", "port": 22,
            "protocol": "ssh", "shared_nicknames": ["web2"],
        })
    with pytest.raises(TypeError):
        CreateConnectionRequest(nickname="web1", hostname="", shared_nicknames=("web2",))
