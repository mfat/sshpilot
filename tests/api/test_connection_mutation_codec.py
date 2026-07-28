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
    )
    update = UpdateConnectionRequest(username="", port=22)
    delete = DeleteConnectionRequest(
        connection_id=ConnectionId("connection:v1:demo")
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
            {"connection_id": "connection:v1:demo", "deleted": 1},
        ),
    ],
)
def test_mutation_codec_rejects_malformed_or_secret_bearing_payloads(
    decoder,
    payload,
):
    with pytest.raises((TypeError, ValueError)):
        decoder(payload)
