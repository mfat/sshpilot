"""Creating connections: several names on one Host line, and never a pattern.

A regular ssh_config user types what they would write after ``Host``. Several
plain names are several connections that share a block; a pattern is a rule
for many hosts, not a connection, and saving one as a connection used to
write a stray ``Host *`` block that showed up nowhere.
"""

import pytest

from sshpilot.api.errors import ErrorCode, SshPilotError, ValidationReason
from sshpilot.api.models.connections import CreateConnectionRequest
from sshpilot.api.transport.codec import (
    create_connection_request_from_wire,
    create_connection_request_to_wire,
)
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


def test_several_names_are_written_on_one_host_line_and_each_is_a_connection(repo):
    repository, root = repo
    record = repository.create_connection({
        "nickname": "web1", "shared_nicknames": ["web2", "web3"],
        "hostname": "", "username": "deploy", "port": 22, "protocol": "ssh",
    })
    assert record.id == "web1"
    assert "Host web1 web2 web3\n" in root.read_text()
    assert _ids(repository) == ["existing", "web1", "web2", "web3"]
    assert {repository.get_record(n).username for n in ("web1", "web2", "web3")} == {"deploy"}


def test_the_display_name_goes_to_the_first_name_only(repo):
    repository, _root = repo
    repository.create_connection({
        "nickname": "web1", "shared_nicknames": ["web2"], "display_name": "Front",
        "hostname": "", "username": "", "port": 22, "protocol": "ssh",
    })
    names = {
        summary.nickname: summary.display_name
        for summary in repository.snapshot().connections
    }
    assert names["web1"] == "Front"
    assert names["web2"] == "web2"


@pytest.mark.parametrize("shared", [["existing"], ["web1"], ["web2", "web2"], ["two words"], [""]])
def test_a_shared_name_that_is_taken_repeated_or_not_one_word_is_refused(repo, shared):
    repository, root = repo
    before = root.read_text()
    with pytest.raises(CoreError):
        repository.create_connection({
            "nickname": "web1", "shared_nicknames": shared,
            "hostname": "", "username": "", "port": 22, "protocol": "ssh",
        })
    assert root.read_text() == before
    assert _ids(repository) == ["existing"]


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


def test_a_pattern_among_the_shared_names_is_refused(repo):
    repository, root = repo
    before = root.read_text()
    with pytest.raises(CoreError) as refused:
        repository.create_connection({
            "nickname": "edge", "shared_nicknames": ["*.prod"],
            "hostname": "", "username": "", "port": 22, "protocol": "ssh",
        })
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


def test_the_request_carries_shared_names_over_the_wire():
    request = CreateConnectionRequest(
        nickname="web1", hostname="", shared_nicknames=["web2", "web3"],
    )
    assert request.shared_nicknames == ("web2", "web3")
    wire = create_connection_request_to_wire(request)
    assert wire["shared_nicknames"] == ["web2", "web3"]
    assert create_connection_request_from_wire(wire) == request
    alone = create_connection_request_to_wire(CreateConnectionRequest(nickname="a", hostname=""))
    assert "shared_nicknames" not in alone


@pytest.mark.parametrize("kwargs", [
    {"shared_nicknames": ["web1"]},
    {"shared_nicknames": ["web2", "web2"]},
    {"shared_nicknames": ["two words"]},
    {"shared_nicknames": "web2"},
    {"shared_nicknames": ["web2"], "protocol": "telnet"},
])
def test_the_request_refuses_shared_names_that_cannot_be_written(kwargs):
    with pytest.raises((ValueError, TypeError)):
        CreateConnectionRequest(nickname="web1", hostname="", **kwargs)
