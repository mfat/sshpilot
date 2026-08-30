"""The seam between the connection editor's vocabulary and each backend's.

Every other protocol test sits on one side of this join.  The per-plugin
``build_spawn`` tests hand-build ``connection.data`` already in the shape the
backend wants, so they prove argv assembly but not that anything *produces*
that shape; ``test_non_ssh_persistence`` and the daemon mutation tests prove
data persists but never reach a backend.  Nothing checked that a value typed
into the editor is the value ``build_spawn`` later reads — which is where
plugin fields blanking on reopen came from, and why a daemon round-trip test
can persist a "serial" connection keyed ``baudrate`` (no such FieldSpec) that
could never launch.

So this walks the whole chain per protocol, deriving the payload from the
backend's own ``connection_fields()``:

    editor payload -> DaemonConnectionServices -> ConnectionApplicationService
      -> state file -> fresh ConnectionRepository (a daemon restart)
      -> DaemonConnectionLaunchProvider -> argv

then reopens the editor the way the window does (``get_connection`` ->
``_editor_details_to_connection`` -> ``load_connection_data``), saves what the
form now holds, and asserts the argv is unchanged.

``CASES`` is asserted to cover every declared FieldSpec, so adding or renaming
a field fails here until the payload is updated — the drift guard the rest of
the suite lacks.  No external binary is needed: only the resolver is stubbed,
so this runs in CI, unlike the pexpect/docker/socat integration tests.
"""

from __future__ import annotations

import shutil

import pytest

from sshpilot.connection_dialog import ConnectionDialog, _editor_details_to_connection
from sshpilot.core.connection_application_service import ConnectionApplicationService
from sshpilot.core.connections.repository import ConnectionRepository
from sshpilot.core.connections.ssh_config_store import SshConfigStore
from sshpilot.daemon.connection_launch_provider import DaemonConnectionLaunchProvider
from sshpilot.gtk.daemon_connection_services import DaemonConnectionServices
from sshpilot.plugins import registry as registry_mod
from sshpilot.plugins.loader import ensure_builtin_protocols

BIN = "/fake/bin"

# Values a user types into the editor, keyed exactly like the FieldSpecs.
CASES = {
    "telnet": {
        "fields": {"host": "10.0.0.5", "port": 2323},
        "argv": [f"{BIN}/telnet", "10.0.0.5", "2323"],
    },
    "serial": {
        "fields": {
            "device": "/dev/ttyUSB0", "baud": "9600", "flow": "hard",
            "databits": "7", "parity": "even", "stopbits": "2",
        },
        "argv": [
            f"{BIN}/picocom", "-b", "9600", "-f", "h",
            "--databits", "7", "--parity", "e", "--stopbits", "2",
            "/dev/ttyUSB0",
        ],
    },
    "docker": {
        "fields": {
            "container": "web", "command": "bash", "runtime": "podman",
            "docker_host": "ssh://u@h", "user": "root", "workdir": "/srv",
        },
        "argv": [
            f"{BIN}/podman", "-H", "ssh://u@h", "exec", "-it",
            "-u", "root", "-w", "/srv", "web", "bash",
        ],
    },
    "k8s": {
        "fields": {
            "pod": "api-0", "container": "app", "namespace": "prod",
            "kube_context": "staging", "kubeconfig": "/tmp/kc", "command": "bash",
        },
        "argv": [
            f"{BIN}/kubectl", "--kubeconfig", "/tmp/kc", "--context", "staging",
            "-n", "prod", "exec", "-it", "api-0", "-c", "app", "--", "bash",
        ],
    },
    "mosh": {
        "fields": {
            "host": "shell.example", "username": "alice", "port": 2222,
            "keyfile": "", "extra_ssh_opts": "", "predict": "never",
            "mosh_port": "60000:60010",
        },
        # mosh wraps the shared SSH command builder, whose flags depend on the
        # host's ssh config, so the fixed prefix is asserted and the --ssh=
        # value is checked for the fields that came from the editor.
        "argv_prefix": [
            f"{BIN}/mosh", "--predict=never", "--port", "60000:60010",
        ],
        "ssh_contains": ["-p", "2222", "-l", "alice"],
        "argv_last": "shell.example",
    },
}


@pytest.fixture
def registry(monkeypatch, tmp_path):
    """A registry of freshly activated built-ins with stubbed binaries."""
    monkeypatch.setattr(registry_mod, "_registry", None)
    import sshpilot.plugins.loader as loader_mod

    monkeypatch.setattr(loader_mod, "_builtins_ensured_for", None)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))

    def _which(name, *args, **kwargs):
        return name if str(name).startswith(BIN) else f"{BIN}/{name}"

    # Patching the module object patches ``shutil`` for the backends that call
    # it directly (mosh) and for the launch provider's final resolve.
    monkeypatch.setattr(shutil, "which", _which)
    import sshpilot.plugins.builtin._flatpak as flatpak

    monkeypatch.setattr(
        flatpak, "resolve_host_binary", lambda name: [f"{BIN}/{name}"]
    )
    ensure_builtin_protocols()
    return registry_mod.protocol_registry()


class _Projection:
    """The read-only half the GTK services facade wraps."""

    def __init__(self):
        self.items = {}

    @property
    def connections(self):
        return tuple(self.items.values())

    def get_connections(self):
        return self.connections

    def find_connection_by_nickname(self, nickname):
        return self.items.get(nickname)

    get_connection_by_id = find_connection_by_nickname


def _repository(tmp_path):
    root = tmp_path / "ssh_config"
    root.write_text("# empty\n", encoding="utf-8")
    return ConnectionRepository(
        ssh_store=SshConfigStore(root),
        state_path=tmp_path / "connections.json",
        legacy_config_path=tmp_path / "config.json",
        isolated=False,
    )


def _launch(repository, nickname):
    provider = DaemonConnectionLaunchProvider(
        repository.get_record, secret_provider=None, app_config=None
    )
    argv, _env = provider.prepare_terminal_launch(
        nickname, interaction_policy="none"
    )
    return list(argv)


def _reopen(details, specs):
    """Run the editor's real field loader against the daemon's DTO.

    Mirrors ``_hydrate_plugin_connection_editor``: the form is repointed at the
    daemon snapshot, then ``load_connection_data`` fills the plugin rows.  The
    fake widgets start empty, like a real ``Gtk.Entry`` whose setter never
    fires.
    """

    class _Form:
        pass

    form = _Form()
    form.connection = _editor_details_to_connection(details)
    values = {spec.key: "" for spec in specs}
    form._plugin_field_widgets = {
        spec.key: (
            spec,
            None,
            (lambda key=spec.key: values.get(key)),
            (lambda value, key=spec.key: values.__setitem__(key, value)),
        )
        for spec in specs
    }
    ConnectionDialog._load_plugin_field_values(form)
    return values


def _assert_argv(protocol, case, argv):
    if "argv" in case:
        assert argv == case["argv"]
        return
    assert argv[: len(case["argv_prefix"])] == case["argv_prefix"]
    assert argv[-1] == case["argv_last"]
    ssh_value = next(a for a in argv if a.startswith("--ssh="))
    for token in case["ssh_contains"]:
        assert token in ssh_value.split(), (protocol, token, ssh_value)


@pytest.mark.parametrize("protocol", sorted(CASES))
def test_editor_fields_reach_the_launch_command(protocol, registry, tmp_path):
    """A value typed into the editor is the value ``build_spawn`` reads."""
    backend = registry.get_or_none(protocol)
    assert backend is not None, f"{protocol} backend did not register"
    specs = backend.connection_fields()
    case = CASES[protocol]

    # The drift guard: cover every declared field, so a renamed or added
    # FieldSpec fails here rather than silently going untested.
    assert set(case["fields"]) == {spec.key for spec in specs}

    repository = _repository(tmp_path)
    core = ConnectionApplicationService(
        repository, client_name="seam", allow_cross_thread_commands=True
    )
    facade = DaemonConnectionServices(_Projection())
    facade.attach_client(core)
    try:
        # Exactly what ConnectionDialog._save_plugin_connection emits.
        payload = {
            "nickname": f"{protocol}-demo",
            "protocol": protocol,
            **case["fields"],
        }
        assert backend.validate(payload) == []
        facade.add_connection_from_data(dict(payload))

        # A daemon restart: nothing is served from memory.
        reloaded = _repository(tmp_path)
        _assert_argv(protocol, case, _launch(reloaded, f"{protocol}-demo"))

        # Reopen the editor, save what the form now holds, and land on the
        # same command — the round trip that blank FieldSpecs used to break.
        details = core.get_connection(f"{protocol}-demo")
        reopened = _reopen(details, specs)
        for key, entered in case["fields"].items():
            assert reopened[key] == entered, (protocol, key)

        resave = {"nickname": f"{protocol}-demo", "protocol": protocol, **reopened}
        assert backend.validate(resave) == []
        facade.update_connection(details, resave)

        reloaded = _repository(tmp_path)
        _assert_argv(protocol, case, _launch(reloaded, f"{protocol}-demo"))
    finally:
        core.close()


@pytest.mark.parametrize("protocol", sorted(CASES))
def test_required_fields_block_saving_and_launching(protocol, registry, tmp_path):
    """A required field left empty is refused in the editor and at spawn."""
    backend = registry.get_or_none(protocol)
    required = [spec.key for spec in backend.connection_fields() if spec.required]
    assert required, f"{protocol} declares no required field"

    for key in required:
        blanked = {**CASES[protocol]["fields"], key: ""}
        payload = {"nickname": f"{protocol}-demo", "protocol": protocol, **blanked}
        assert backend.validate(payload), (protocol, key)


@pytest.mark.parametrize(
    "protocol,field",
    [("docker", "command"), ("k8s", "command"), ("mosh", "extra_ssh_opts")],
)
def test_unparsable_shell_field_is_reported_not_raised(
    protocol, field, registry, tmp_path
):
    """A stray quote is a validation error, never an untyped ValueError.

    ``_prepare_protocol_launch`` converts only ``ProtocolError`` into a
    reportable failure, so a bare ``ValueError`` out of ``shlex.split`` would
    surface as an unexpected internal error for what is ordinary bad input.
    """
    from sshpilot.plugins.api import ProtocolError

    backend = registry.get_or_none(protocol)
    broken = {**CASES[protocol]["fields"], field: 'sh -c "echo hi'}

    problems = backend.validate(
        {"nickname": f"{protocol}-demo", "protocol": protocol, **broken}
    )
    assert any("could not be parsed" in problem for problem in problems)

    # And if such a connection is reached some other way (the plugin API, an
    # imported backup), the spawn refuses it as a ProtocolError.
    repository = _repository(tmp_path)
    core = ConnectionApplicationService(
        repository, client_name="seam", allow_cross_thread_commands=True
    )
    facade = DaemonConnectionServices(_Projection())
    facade.attach_client(core)
    try:
        facade.add_connection_from_data(
            {"nickname": f"{protocol}-demo", "protocol": protocol, **broken}
        )
        record = repository.get_record(f"{protocol}-demo")
        from sshpilot.daemon.connection_launch_provider import HeadlessConnectionView

        with pytest.raises(ProtocolError):
            backend.build_spawn(HeadlessConnectionView(record), _spawn_ctx(registry))
    finally:
        core.close()


def _spawn_ctx(registry):
    from sshpilot.plugins.api import PluginContext

    return PluginContext.for_spawn(
        plugin_id="seam",
        app_config=None,
        connection_manager=None,
        protocol_registry=registry,
    )
