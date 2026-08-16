from types import SimpleNamespace

import sshpilot.window as window_module
from sshpilot.window import MainWindow


class _Connection:
    def __init__(self, nickname):
        self.nickname = nickname


def test_bulk_delete_yields_between_connections_and_reloads_once(monkeypatch):
    window = MainWindow.__new__(MainWindow)
    removed = []
    window.connection_manager = SimpleNamespace(
        remove_connection=lambda connection, **kwargs: removed.append(
            (connection.nickname, kwargs)
        ),
    )
    errors = []
    window._error_dialog = lambda title, message: errors.append((title, message))
    window._is_quitting = False
    window.client_bridge = None

    connections = [_Connection("one"), _Connection("two"), _Connection("three")]
    window.on_delete_connection_response(
        None,
        "delete",
        {"connections": connections},
    )

    assert removed == []
    assert errors
    assert window._deleting_connections_batch is False


def test_refresh_group_rows_updates_normal_and_tag_counts(monkeypatch):
    removed = _Connection("gone")
    remaining = _Connection("keep")

    class _Member:
        def __init__(self, connection):
            self.connection = connection

    class _GroupRow:
        def __init__(self, group_id, connections, *, is_tag_group=False):
            self.group_id = group_id
            self.group_info = {"connections": list(connections)}
            self.connections_dict = {
                "gone": removed,
                "keep": remaining,
            }
            self.is_tag_group = is_tag_group
            self._member_rows = [_Member(removed), _Member(remaining)]
            self.next_row = None
            self.count = None

        def _update_display(self):
            self.count = len([
                nickname
                for nickname in self.group_info["connections"]
                if nickname in self.connections_dict
            ])

        def get_next_sibling(self):
            return self.next_row

    normal_row = _GroupRow("group-1", ["gone", "keep"])
    tag_row = _GroupRow("tag-1", ["gone"], is_tag_group=True)
    normal_row.next_row = tag_row

    monkeypatch.setattr(window_module, "GroupRow", _GroupRow)

    window = MainWindow.__new__(MainWindow)
    window.connection_manager = SimpleNamespace(
        get_connections=lambda: [remaining]
    )
    window.group_manager = SimpleNamespace(
        groups={
            "group-1": {
                "connections": ["keep"],
            }
        }
    )
    window.connection_list = SimpleNamespace(
        get_first_child=lambda: normal_row
    )

    window._refresh_group_rows_after_connection_removed(removed)

    assert normal_row.group_info["connections"] == ["keep"]
    assert normal_row.count == 1
    assert [member.connection for member in normal_row._member_rows] == [remaining]
    assert tag_row.group_info["connections"] == []
    assert tag_row.count == 0
