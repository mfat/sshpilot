from sshpilot.api.models import ConnectionMutationResult, ConnectionSummary
from sshpilot.gtk.daemon_connection_services import DaemonConnectionServices


class Projection:
    def __init__(self):
        self.connection = ConnectionSummary(
            id="demo",
            nickname="demo",
            host="demo.example",
            hostname="demo.example",
            username="alice",
            port=22,
        )

    @property
    def connections(self):
        return (self.connection,)

    def get_connections(self):
        return self.connections

    def find_connection_by_nickname(self, nickname):
        return self.connection if nickname == "demo" else None

    get_connection_by_id = find_connection_by_nickname


class Client:
    def __init__(self):
        self.created = None
        self.updated = None
        self.secrets = {}

    def create_connection(self, request):
        self.created = request
        return ConnectionMutationResult("new", request.nickname, 0)

    def get_connection(self, connection_id):
        return ConnectionSummary(
            id=connection_id,
            nickname=self.created.nickname,
            host=self.created.hostname,
            hostname=self.created.hostname,
            username=self.created.username,
            port=self.created.port,
            protocol=self.created.protocol,
        )

    def update_connection(self, connection_id, request):
        self.updated = (connection_id, request)
        return ConnectionMutationResult(connection_id, request.nickname, 1)

    def store_plugin_secret(self, plugin_id, key, value):
        self.secrets[(plugin_id, key)] = value
        return True

    def get_plugin_secret(self, plugin_id, key):
        return self.secrets.get((plugin_id, key))

    def delete_plugin_secret(self, plugin_id, key):
        return self.secrets.pop((plugin_id, key), None) is not None


def services():
    facade = DaemonConnectionServices(Projection())
    client = Client()
    facade.attach_client(client)
    return facade, client


def test_plugin_secrets_route_to_attached_daemon_client():
    facade, client = services()

    assert facade.store_plugin_secret("plugin", "token", "value") is True
    assert facade.get_plugin_secret("plugin", "token") == "value"
    assert facade.delete_plugin_secret("plugin", "token") is True
    assert client.secrets == {}


def test_plugin_connection_data_routes_through_plugin_payload():
    facade, client = services()

    result = facade.add_connection_from_data(
        {
            "nickname": "serial-demo",
            "hostname": "/dev/ttyUSB0",
            "protocol": "serial",
            "baudrate": 115200,
            "api_token": "must-not-be-persisted",
        }
    )

    assert result.nickname == "serial-demo"
    assert client.created.protocol == "serial"
    assert client.created.config_patch == {}
    assert client.created.plugin_data == {"baudrate": 115200}


def test_ssh_plugin_provisioning_uses_config_patch_not_plugin_data():
    facade, client = services()

    facade.add_connection_from_data(
        {
            "nickname": "new",
            "hostname": "new.example",
            "username": "root",
            "protocol": "ssh",
            "remote_command": "uptime",
        }
    )

    assert client.created.plugin_data == {}
    assert client.created.config_patch == {"remote_command": "uptime"}


def test_update_routes_through_daemon_client():
    facade, client = services()

    assert facade.update_connection(
        facade.find_connection_by_nickname("demo"),
        {"nickname": "demo", "hostname": "changed.example"},
    ) is True
    connection_id, request = client.updated
    assert connection_id == "demo"
    assert request.hostname == "changed.example"


def test_plugin_payload_filter_matches_the_daemon_projection():
    """The write filter must accept exactly what the read projection returns.

    ``extract_plugin_data`` strips core columns and bookkeeping keys when it
    builds ``ConnectionDetails.plugin_data``.  If the write side were more
    permissive, such a key would be persisted into ``record.data`` — colliding
    with the record's own bookkeeping — and then stripped on read, so the
    editor would reopen it blank and save it back empty.
    """
    from sshpilot.api.models.connections import extract_plugin_data

    facade, client = services()
    payload = {
        "nickname": "serial-demo",
        "hostname": "/dev/ttyUSB0",
        "protocol": "serial",
        "device": "/dev/ttyUSB0",
        "baud": "115200",
        # Core columns and record bookkeeping must never reach plugin_data.
        "display_name": "Serial demo",
        "source": "~/.ssh/config",
        "uuid": "0f0f",
        "generation": 3,
        "order": 2,
        "aliases": ["other"],
        "authored_directives": ["Host"],
        # Secret-like names are filtered on both sides too.
        "api_token": "must-not-be-persisted",
    }

    facade.add_connection_from_data(dict(payload))
    assert client.created.plugin_data == extract_plugin_data("serial", payload)
    assert client.created.plugin_data == {"device": "/dev/ttyUSB0", "baud": "115200"}

    facade.update_connection(
        facade.find_connection_by_nickname("demo"), dict(payload)
    )
    _connection_id, request = client.updated
    assert request.plugin_data == extract_plugin_data("serial", payload)
    # display_name still rides its own core column rather than plugin_data.
    assert request.display_name == "Serial demo"
