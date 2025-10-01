import importlib

from sshpilot.connection_manager import Connection


class DummyRowBase:
    def __init__(self):
        self._parent_list = None

    def _set_parent_list(self, parent):
        self._parent_list = parent

    def get_next_sibling(self):
        if not self._parent_list:
            return None
        try:
            index = self._parent_list.children.index(self)
        except ValueError:
            return None
        next_index = index + 1
        if next_index < len(self._parent_list.children):
            return self._parent_list.children[next_index]
        return None


class DummyListBox:
    def __init__(self):
        self.children = []

    def get_first_child(self):
        return self.children[0] if self.children else None

    def remove(self, child):
        if child in self.children:
            self.children.remove(child)
        if hasattr(child, "_set_parent_list"):
            child._set_parent_list(None)

    def append(self, child):
        self.children.append(child)
        if hasattr(child, "_set_parent_list"):
            child._set_parent_list(self)

    def __iter__(self):
        return iter(self.children)


class DummySearchEntry:
    def __init__(self, text):
        self._text = text

    def get_text(self):
        return self._text


class DummyConnectionManager:
    def __init__(self, connections):
        self._connections = list(connections)

    def get_connections(self):
        return list(self._connections)


class DummyConfig:
    def get_setting(self, key, default=None):
        return default


class DummyGroupManager:
    def __init__(self, groups):
        self.groups = groups

    def get_all_groups(self):
        return [
            {
                "id": group_id,
                "name": info.get("name", ""),
            }
            for group_id, info in self.groups.items()
        ]


class StubGroupRow(DummyRowBase):
    def __init__(self, group_info, group_manager, connections_dict):
        super().__init__()
        self.group_info = group_info
        self.group_manager = group_manager
        self.group_id = group_info["id"]
        self.connections_dict = connections_dict
        self.connected_signals = []

    def connect(self, signal_name, callback):
        self.connected_signals.append((signal_name, callback))


class StubConnectionRow(DummyRowBase):
    def __init__(self, connection, group_manager=None, config=None):
        super().__init__()
        self.connection = connection
        self.indentation = 0
        self.hide_hosts = None
        
    def set_indentation(self, level):
        self.indentation = level

    def apply_hide_hosts(self, value):
        self.hide_hosts = value


def test_search_results_include_matching_groups(monkeypatch):
    window_module = importlib.import_module("sshpilot.window")
    window_module = importlib.reload(window_module)

    monkeypatch.setattr(window_module, "GroupRow", StubGroupRow)
    monkeypatch.setattr(window_module, "ConnectionRow", StubConnectionRow)

    added_connections = []
    original_add_connection_row = window_module.MainWindow.add_connection_row

    def tracked_add_connection_row(self, connection, indent_level: int = 0):
        added_connections.append((connection.nickname, indent_level))
        return original_add_connection_row(self, connection, indent_level)

    monkeypatch.setattr(window_module.MainWindow, "add_connection_row", tracked_add_connection_row)

    original_matcher = window_module.connection_matches
    match_calls = []

    def recording_match(connection, query):
        fields = [
            getattr(connection, "nickname", ""),
            getattr(connection, "host", ""),
        ]
        result = original_matcher(connection, query)
        match_calls.append((connection.nickname, query, fields, result))
        return result

    monkeypatch.setattr(window_module, "connection_matches", recording_match)

    group_id = "group-1"
    group_connections = ["prod-server"]
    group_data = {
        group_id: {
            "id": group_id,
            "name": "Production",
            "connections": group_connections,
            "children": [],
            "expanded": True,
        }
    }

    grouped_connection = Connection({"nickname": "prod-server", "host": "prod-01"})
    direct_connection = Connection({"nickname": "prod-bastion", "host": "bastion"})

    test_window = window_module.MainWindow.__new__(window_module.MainWindow)
    test_window.connection_list = DummyListBox()
    test_window.connection_rows = {}
    test_window.connection_scrolled = None
    test_window.connection_manager = DummyConnectionManager([grouped_connection, direct_connection])
    test_window.group_manager = DummyGroupManager(group_data)
    test_window.config = DummyConfig()
    test_window.search_entry = DummySearchEntry("prod")
    test_window._hide_hosts = False

    assert original_matcher(grouped_connection, "prod")
    assert original_matcher(direct_connection, "prod")

    test_window.rebuild_connection_list()

    children = test_window.connection_list.children
    group_rows = [row for row in children if isinstance(row, StubGroupRow)]
    connection_rows = [row for row in children if isinstance(row, StubConnectionRow)]

    assert len(group_rows) == 1
    assert group_rows[0].group_info["name"] == "Production"
    assert match_calls == [
        ("prod-server", "prod", ["prod-server", "prod-01"], True),
        ("prod-bastion", "prod", ["prod-bastion", "bastion"], True),
    ]
    assert added_connections == [("prod-server", 1), ("prod-bastion", 0)]

    grouped_rows = [row for row in connection_rows if row.connection.nickname == "prod-server"]
    assert len(grouped_rows) == 1
    assert grouped_rows[0].indentation == 1

    direct_rows = [row for row in connection_rows if row.connection.nickname == "prod-bastion"]
    assert len(direct_rows) == 1
    assert direct_rows[0].indentation == 0

