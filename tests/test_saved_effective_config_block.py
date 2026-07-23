from types import SimpleNamespace

from sshpilot.effective_config_dialog import (
    EffectiveConfigDialog,
    _diff_rows,
    saved_connection_block,
)


class _Manager:
    def __init__(self, details=None):
        self.details = details
        self.request = None
        self.formatted = None

    def get_host_block_details(self, host, source):
        self.request = (host, source)
        return self.details

    def format_ssh_config_entry(self, data):
        self.formatted = data
        return "generated"


def test_saved_connection_block_reads_authored_stanza():
    manager = _Manager({
        "lines": [
            "Host US",
            "    HostName 107.175.36.82",
            "    IdentityFile /home/mahdi/.ssh/kwp4",
        ],
    })
    connection = SimpleNamespace(nickname="US", source="/tmp/hosts.conf")

    block = saved_connection_block(manager, connection)

    assert block == (
        "Host US\n"
        "    HostName 107.175.36.82\n"
        "    IdentityFile /home/mahdi/.ssh/kwp4"
    )
    assert manager.request == ("US", "/tmp/hosts.conf")
    assert manager.formatted is None


def test_saved_connection_block_falls_back_to_submitted_data():
    manager = _Manager()
    connection = SimpleNamespace(nickname="US", source="/tmp/hosts.conf")
    submitted = {"nickname": "US", "hostname": "107.175.36.82"}

    block = saved_connection_block(
        manager, connection, host="US", fallback_data=submitted)

    assert block == "generated"
    assert manager.formatted is submitted


def test_for_connection_uses_connection_nickname():
    manager = _Manager({"lines": ["Host US", "    User root"]})
    connection = SimpleNamespace(
        nickname="US",
        source="/tmp/hosts.conf",
        _resolve_config_override_path=lambda: "/tmp/config",
    )

    class _Dialog:
        def __init__(self, parent, **kwargs):
            self.parent = parent
            self.kwargs = kwargs
            self.presented = False

        def present(self):
            self.presented = True

    dialog = EffectiveConfigDialog.for_connection.__func__(
        _Dialog, "parent", connection, manager)

    assert dialog.kwargs["host"] == "US"
    assert dialog.kwargs["own_block"] == "Host US\n    User root"
    assert dialog.presented is True


def test_changes_view_keeps_equal_values_of_changed_multi_value_setting():
    own = [
        "hostname 107.175.36.82",
        "identityfile /home/mahdi/.ssh/kwp4",
        "user root",
    ]
    effective = [
        "hostname 107.175.36.82",
        "identityfile /home/mahdi/.ssh/id_ed25519",
        "identityfile /home/mahdi/.ssh/id_rsa",
        "identityfile /home/mahdi/.ssh/kwp4",
        "user root",
    ]

    rows = _diff_rows(own, effective, full_mode=False)

    assert rows == [
        ("", "identityfile /home/mahdi/.ssh/id_ed25519", "insert"),
        ("", "identityfile /home/mahdi/.ssh/id_rsa", "insert"),
        (
            "identityfile /home/mahdi/.ssh/kwp4",
            "identityfile /home/mahdi/.ssh/kwp4",
            "equal",
        ),
    ]
