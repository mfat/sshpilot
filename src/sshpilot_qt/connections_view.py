"""Qt-based connection list with filtering, grouping, and context menus."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from PyQt6.QtCore import QAbstractListModel, QModelIndex, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QListView,
    QLineEdit,
    QMenu,
    QSizePolicy,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from sshpilot.connection_manager import Connection


@dataclass
class _ConnectionRow:
    connection: Optional[Connection]
    group: str = ""
    is_header: bool = False


class ConnectionListModel(QAbstractListModel):
    """List model that supports grouping and search filtering."""

    ConnectionRole = Qt.ItemDataRole.UserRole + 1
    GroupRole = Qt.ItemDataRole.UserRole + 2
    HeaderRole = Qt.ItemDataRole.UserRole + 3

    def __init__(self, connections: Optional[List[Connection]] = None):
        super().__init__()
        self._all_connections: List[Connection] = connections or []
        self._rows: List[_ConnectionRow] = []
        self._filter_text: str = ""
        self._grouping_enabled = False
        self._default_group_name = "Ungrouped"
        self._refresh_rows()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        if parent.isValid():
            return 0
        return len(self._rows)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):  # noqa: D401, N802
        if not index.isValid() or index.row() >= len(self._rows):
            return None

        row = self._rows[index.row()]
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            if row.is_header:
                return row.group
            if row.connection is None:
                return ""
            nickname = getattr(row.connection, "nickname", "")
            host = row.connection.get_effective_host() if hasattr(row.connection, "get_effective_host") else ""
            username = getattr(row.connection, "username", "")
            if username and host:
                return f"{nickname} ({username}@{host})"
            if host:
                return f"{nickname} ({host})"
            return nickname
        if role == Qt.ItemDataRole.ToolTipRole and row.connection is not None:
            host = row.connection.get_effective_host() if hasattr(row.connection, "get_effective_host") else ""
            return f"{row.connection.nickname}\nHost: {host}\nPort: {getattr(row.connection, 'port', '')}"
        if role == self.ConnectionRole:
            return row.connection
        if role == self.GroupRole:
            return row.group
        if role == self.HeaderRole:
            return row.is_header
        return None

    def flags(self, index: QModelIndex):  # noqa: D401
        base = super().flags(index)
        if not index.isValid():
            return base
        if self._rows[index.row()].is_header:
            return Qt.ItemFlag.ItemIsEnabled
        return base | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled

    def set_connections(self, connections: List[Connection]):
        self.beginResetModel()
        self._all_connections = connections
        self._refresh_rows()
        self.endResetModel()

    def set_filter_text(self, text: str):
        normalized = text.lower().strip()
        if normalized == self._filter_text:
            return
        self.beginResetModel()
        self._filter_text = normalized
        self._refresh_rows()
        self.endResetModel()

    def set_grouping_enabled(self, enabled: bool):
        if self._grouping_enabled == enabled:
            return
        self.beginResetModel()
        self._grouping_enabled = enabled
        self._refresh_rows()
        self.endResetModel()

    def connection_at(self, index: QModelIndex) -> Optional[Connection]:
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        return None if row.is_header else row.connection

    def _refresh_rows(self):
        self._rows = []
        filtered = [conn for conn in self._all_connections if self._accepts_connection(conn)]
        if not self._grouping_enabled:
            self._rows = [_ConnectionRow(connection=conn, group="") for conn in filtered]
            return

        grouped: Dict[str, List[Connection]] = {}
        for conn in filtered:
            meta = getattr(conn, "data", {}) or {}
            group_name = meta.get("group") or self._default_group_name
            grouped.setdefault(group_name, []).append(conn)

        for group in sorted(grouped.keys(), key=lambda name: name.lower()):
            self._rows.append(_ConnectionRow(connection=None, group=group, is_header=True))
            for conn in sorted(grouped[group], key=lambda c: getattr(c, "nickname", "").lower()):
                self._rows.append(_ConnectionRow(connection=conn, group=group, is_header=False))

    def _accepts_connection(self, connection: Connection) -> bool:
        if not self._filter_text:
            return True
        haystack = " ".join(
            [
                getattr(connection, "nickname", ""),
                getattr(connection, "hostname", ""),
                getattr(connection, "host", ""),
                getattr(connection, "username", ""),
            ]
        ).lower()
        return self._filter_text in haystack


class ConnectionsView(QWidget):
    """Composite widget wrapping search, list/tree view, and context menus."""

    connection_requested = pyqtSignal(Connection)
    edit_requested = pyqtSignal(Connection)
    duplicate_requested = pyqtSignal(Connection)
    delete_requested = pyqtSignal(Connection)

    def __init__(self, *, use_tree: bool = True, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.model = ConnectionListModel([])
        self.search = QLineEdit(self)
        self.search.setPlaceholderText("Search connections…")
        self.search.textChanged.connect(self.model.set_filter_text)

        self.view = QTreeView(self) if use_tree else QListView(self)
        self.view.setModel(self.model)
        self.view.setUniformRowHeights(True)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._open_context_menu)
        self.view.setEditTriggers(QTreeView.EditTrigger.NoEditTriggers)
        self.view.setHeaderHidden(True)
        self.view.setRootIsDecorated(False)
        self.view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        layout = QVBoxLayout(self)
        layout.addWidget(self.search)
        layout.addWidget(self.view)
        self.setLayout(layout)

    def bind_connections(self, connections: List[Connection]):
        self.model.set_connections(connections)

    def enable_grouping(self, enabled: bool):
        self.model.set_grouping_enabled(enabled)
        if isinstance(self.view, QTreeView):
            self.view.expandAll()

    def refresh(self):
        self.model.layoutChanged.emit()

    def _open_context_menu(self, point):
        index = self.view.indexAt(point)
        connection = self.model.connection_at(index)
        if connection is None:
            return

        menu = QMenu(self)
        menu.addAction("Connect", lambda: self.connection_requested.emit(connection))
        menu.addAction("Edit", lambda: self.edit_requested.emit(connection))
        menu.addAction("Duplicate", lambda: self.duplicate_requested.emit(connection))
        menu.addSeparator()
        menu.addAction("Delete", lambda: self.delete_requested.emit(connection))
        menu.exec(self.view.viewport().mapToGlobal(point))

    def select_connection(self, connection: Connection):
        for row in range(self.model.rowCount()):
            index = self.model.index(row, 0)
            if self.model.connection_at(index) == connection:
                self.view.setCurrentIndex(index)
                break

    def set_context_handlers(
        self,
        *,
        on_connect: Optional[Callable[[Connection], None]] = None,
        on_edit: Optional[Callable[[Connection], None]] = None,
        on_duplicate: Optional[Callable[[Connection], None]] = None,
        on_delete: Optional[Callable[[Connection], None]] = None,
    ):
        if on_connect:
            self.connection_requested.connect(on_connect)
        if on_edit:
            self.edit_requested.connect(on_edit)
        if on_duplicate:
            self.duplicate_requested.connect(on_duplicate)
        if on_delete:
            self.delete_requested.connect(on_delete)
