import pytest

from sshpilot.api.models import (
    CreateConnectionRequest,
    DeleteConnectionRequest,
    DeleteConnectionResult,
    UpdateConnectionRequest,
)
from sshpilot.api.models.common import ConnectionId
from sshpilot.api.transport.codec import (
    create_connection_request_from_wire,
    create_connection_request_to_wire,
    delete_connection_request_from_wire,
    delete_connection_request_to_wire,
    delete_connection_result_from_wire,
    delete_connection_result_to_wire,
    update_connection_request_from_wire,
    update_connection_request_to_wire,
)


def test_mutation_codec_round_trips_all_public_models():
    create = CreateConnectionRequest(
        nickname="demo",
        hostname="demo.example",
        username="alice",
        port=2202,
        plugin_data={"baudrate": 115200},
    )
    update = UpdateConnectionRequest(
        username="", port=22, plugin_data={"baudrate": 9600}
    )
    delete = DeleteConnectionRequest(
        connection_id=ConnectionId("demo")
    )
    result = DeleteConnectionResult(
        connection_id=delete.connection_id,
        deleted=True,
    )

    assert create_connection_request_from_wire(
        create_connection_request_to_wire(create)
    ) == create
    assert update_connection_request_from_wire(
        update_connection_request_to_wire(update)
    ) == update
    assert delete_connection_request_from_wire(
        delete_connection_request_to_wire(delete)
    ) == delete
    assert delete_connection_result_from_wire(
        delete_connection_result_to_wire(result)
    ) == result


def test_update_expected_generation_none_omitted_but_zero_preserved():
    """None (no generation supplied) must not collide with a real zero
    generation, either on the wire or after a decode."""
    none_request = UpdateConnectionRequest(
        config_patch={"remote_command": "echo hi"},
        expected_generation=None,
    )
    zero_request = UpdateConnectionRequest(
        config_patch={"remote_command": "echo hi"},
        expected_generation=0,
    )

    none_wire = update_connection_request_to_wire(none_request)
    zero_wire = update_connection_request_to_wire(zero_request)

    assert "expected_generation" not in none_wire
    assert zero_wire["expected_generation"] == 0
    assert update_connection_request_from_wire(none_wire).expected_generation is None
    assert update_connection_request_from_wire(zero_wire).expected_generation == 0
    assert update_connection_request_from_wire(
        update_connection_request_to_wire(none_request)
    ) == none_request
    assert update_connection_request_from_wire(
        update_connection_request_to_wire(zero_request)
    ) == zero_request



@pytest.mark.parametrize(
    "decoder,payload",
    [
        (
            create_connection_request_from_wire,
            {
                "nickname": "demo",
                "hostname": "demo.example",
                "username": "",
                "port": 22,
            },
        ),
        (
            create_connection_request_from_wire,
            {
                "nickname": "demo",
                "hostname": "demo.example",
                "username": "",
                "port": 22,
                "protocol": "ssh",
                "password": "must-not-cross",
            },
        ),
        (
            create_connection_request_from_wire,
            {
                "nickname": "demo",
                "hostname": "demo.example",
                "username": "",
                "port": "22",
                "protocol": "ssh",
            },
        ),
        (
            update_connection_request_from_wire,
            {
                "nickname": None,
                "hostname": None,
                "username": None,
                "port": None,
            },
        ),
        (
            update_connection_request_from_wire,
            {
                "nickname": None,
                "hostname": None,
                "username": None,
                "port": 70000,
            },
        ),
        (
            delete_connection_request_from_wire,
            {"connection_id": "", "token": "must-not-cross"},
        ),
        (
            delete_connection_result_from_wire,
            {"connection_id": "demo", "deleted": 1},
        ),
    ],
)
def test_mutation_codec_rejects_malformed_or_secret_bearing_payloads(
    decoder,
    payload,
):
    with pytest.raises((TypeError, ValueError)):
        decoder(payload)
