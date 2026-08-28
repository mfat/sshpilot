"""SSH overrides controller tests (fake daemon-backed client)."""

import threading

from sshpilot.api.models.settings import (
    GlobalSshOverrides,
    UpdateGlobalSshOverridesRequest,
)
from sshpilot.core.settings import compose_ssh_overrides
from sshpilot.core.settings.ssh_overrides import (
    DEFAULT_SSH_OVERRIDES,
    compute_ssh_overrides_revision,
)
from sshpilot.gtk.ssh_overrides_controller import SshOverridesController
from sshpilot.ssh_connection_builder import build_native_command


def _defaults(**overrides):
    values = dict(DEFAULT_SSH_OVERRIDES)
    values.update(overrides)
    return GlobalSshOverrides(
        revision=compute_ssh_overrides_revision(values),
        **values,
    )


class RecordingOverridesClient:
    """Fake ``SshPilotClient`` implementing the three overrides methods."""

    def __init__(self, initial=None):
        self.snapshot = initial or _defaults()
        self.calls = []

    def get_global_ssh_overrides(self):
        self.calls.append("get")
        return self.snapshot

    def update_global_ssh_overrides(self, request):
        assert type(request) is UpdateGlobalSshOverridesRequest
        self.calls.append(("update", dict(request.patch), request.expected_revision))
        values = dict(DEFAULT_SSH_OVERRIDES)
        for field, value in request.patch.items():
            values[field] = value
        self.snapshot = GlobalSshOverrides(
            revision=compute_ssh_overrides_revision(values), **values
        )
        return self.snapshot

    def reset_global_ssh_overrides(self, expected_revision=None):
        self.calls.append(("reset", expected_revision))
        self.snapshot = _defaults()
        return self.snapshot


class BlockingOverridesClient(RecordingOverridesClient):
    """A client that blocks inside ``get`` until released."""

    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def get_global_ssh_overrides(self):
        self.calls.append("get")
        self.entered.set()
        assert self.release.wait(5)
        return self.snapshot


def _controller(client):
    return SshOverridesController(client)


def test_load_reads_from_client_and_caches_snapshot():
    client = RecordingOverridesClient()
    controller = _controller(client)

    snapshot = controller.load()
    assert snapshot is client.snapshot
    assert controller.snapshot() is snapshot
    assert client.calls == ["get"]


def test_update_uses_loaded_revision_for_conflict_checking():
    client = RecordingOverridesClient(initial=_defaults(connect_timeout=1))
    controller = _controller(client)
    loaded = controller.load()

    updated = controller.update({"connect_timeout": 30})
    assert client.calls[-1] == (
        "update",
        {"connect_timeout": 30},
        loaded.revision,
    )
    assert updated.connect_timeout == 30
    assert updated.revision != loaded.revision


def test_update_without_load_uses_explicit_revision():
    client = RecordingOverridesClient()
    controller = _controller(client)

    updated = controller.update(
        {"batch_mode": True}, expected_revision="explicit-rev"
    )
    assert client.calls[-1] == ("update", {"batch_mode": True}, "explicit-rev")
    assert updated.batch_mode is True


def test_reset_calls_client_reset_with_loaded_revision():
    client = RecordingOverridesClient(initial=_defaults(verbosity=2))
    controller = _controller(client)
    loaded = controller.load()

    reset = controller.reset()
    assert client.calls[-1] == ("reset", loaded.revision)
    assert reset.verbosity == 0
    assert reset.connect_timeout == 0


def test_reset_without_load_uses_explicit_revision():
    client = RecordingOverridesClient()
    controller = _controller(client)

    controller.reset(expected_revision="explicit-rev")
    assert client.calls[-1] == ("reset", "explicit-rev")


def test_overlapping_operations_rejected():
    client = BlockingOverridesClient()
    controller = _controller(client)

    result = {}

    def _load():
        result["snapshot"] = controller.load()

    thread = threading.Thread(target=_load)
    thread.start()
    assert client.entered.wait(2)

    # A second operation while the first is in flight is rejected.
    try:
        controller.update({"connect_timeout": 5})
        raised = False
    except RuntimeError:
        raised = True
    assert raised

    client.release.set()
    thread.join(timeout=5)
    assert "snapshot" in result


def test_update_updates_controller_snapshot_after_success():
    client = RecordingOverridesClient()
    controller = _controller(client)
    controller.load()

    updated = controller.update({"connect_timeout": 9})
    assert controller.snapshot() is updated
    assert controller.snapshot().connect_timeout == 9


# ---------------------------------------------------------------------------
# SSH / SCP argv equivalence from the derived override list
# ---------------------------------------------------------------------------
class _Connection:
    id = "example"
    nickname = "example"
    host = "example"
    hostname = "example.com"
    username = ""
    port = 22


def _service_config():
    ssh = {
        "batch_mode": True,
        "connection_timeout": 12,
        "connection_attempts": 4,
        "keepalive_interval": 45,
        "keepalive_count_max": 6,
        "strict_host_key_checking": "accept-new",
        "compression": True,
        "verbosity": 2,
        "debug_enabled": False,
        "controlmaster": False,
    }
    ssh["ssh_overrides"] = compose_ssh_overrides(ssh)
    return type(
        "AppConfig",
        (),
        {"get_ssh_config": lambda self: ssh, "get_setting": lambda self, k, d=None: d},
    )()


def test_ssh_and_scp_argv_carry_identical_override_fragment():
    connection = _Connection()
    ssh_cmd = build_native_command(connection, _service_config(), command_type="ssh")
    scp_cmd = build_native_command(connection, _service_config(), command_type="scp")

    # Same binary-prefixed prefix: overrides land verbatim ahead of the host.
    assert ssh_cmd[0] == "ssh"
    assert scp_cmd[0] == "scp"
    assert ssh_cmd[1:-1] == scp_cmd[1:-1]
    assert ssh_cmd[-1] == scp_cmd[-1] == "example"

    joined = " ".join(ssh_cmd)
    for fragment in (
        "BatchMode=yes",
        "ConnectTimeout=12",
        "ConnectionAttempts=4",
        "ServerAliveInterval=45",
        "ServerAliveCountMax=6",
        "StrictHostKeyChecking=accept-new",
        "Compression=yes",
        "LogLevel=DEBUG2",
    ):
        assert fragment in joined
    # No default keepalive arguments are invented.
    assert not any("default_keepalive" in token for token in ssh_cmd)
    assert ssh_cmd.index("ConnectTimeout=12") < ssh_cmd.index("example")


def test_derived_overrides_override_config_values():
    # The same fragment the controller's daemon composes is what the builder
    # consumes, so Preferences no longer needs to write ssh_overrides itself.
    config = _service_config()
    fragment = config.get_ssh_config()["ssh_overrides"]
    assert "-o" in fragment
    assert fragment[0] == "-o"
