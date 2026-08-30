"""ConnectionDetails.plugin_data wire round-trip."""

from sshpilot.api.models.connections import (
    AuthenticationMethod,
    ConnectionDetails,
    ConnectionHealth,
)
from sshpilot.api.models.common import ConnectionId
from sshpilot.api.transport.codec import (
    connection_details_from_wire,
    connection_details_to_wire,
)


def test_connection_details_plugin_data_round_trip():
    details = ConnectionDetails(
        id=ConnectionId("board"),
        nickname="board",
        host="board",
        hostname="",
        username="",
        port=22,
        protocol="serial",
        health=ConnectionHealth.UNKNOWN,
        authentication_method=AuthenticationMethod.KEY,
        plugin_data={"device": "/dev/ttyUSB0", "baud": "115200"},
    )
    wire = connection_details_to_wire(details)
    assert wire["plugin_data"] == {"device": "/dev/ttyUSB0", "baud": "115200"}
    restored = connection_details_from_wire(wire)
    assert restored.plugin_data == details.plugin_data


def test_connection_details_accepts_missing_plugin_data():
    wire = connection_details_to_wire(
        ConnectionDetails(
            id=ConnectionId("web"),
            nickname="web",
            host="web",
            hostname="example.com",
            username="alice",
            port=22,
        )
    )
    wire.pop("plugin_data")
    restored = connection_details_from_wire(wire)
    assert restored.plugin_data == {}
