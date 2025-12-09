"""Qt main window shell for the sshPilot preview UI."""
from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import (
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QSizePolicy,
    QStatusBar,
    QVBoxLayout,
    QWidget,
    QHBoxLayout,
    QTextEdit,
)

from .resources import load_icon, load_stylesheet


class MainWindow(QMainWindow):
    """Placeholder Qt main window with provisional panes."""

    def __init__(self, config: Optional[object] = None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("sshPilot (Qt Preview)")
        self.setMinimumSize(900, 600)

        app_icon = load_icon("sshpilot.png")
        if app_icon:
            self.setWindowIcon(app_icon)

        self._build_layout()
        self._apply_style_overrides()

    def _build_layout(self) -> None:
        central = QWidget(self)
        root_layout = QHBoxLayout(central)
        root_layout.setContentsMargins(12, 12, 12, 12)
        root_layout.setSpacing(12)

        # Connections list placeholder
        self.connections = QListWidget(central)
        self.connections.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.connections.setMaximumWidth(280)
        self.connections.addItem(QListWidgetItem("Example Host"))
        self.connections.addItem(QListWidgetItem("Staging Bastion"))
        self.connections.addItem(QListWidgetItem("Production"))
        root_layout.addWidget(self.connections)

        # Details / terminal placeholder
        detail_container = QWidget(central)
        detail_layout = QVBoxLayout(detail_container)
        detail_layout.setSpacing(8)

        self.detail_label = QLabel("Connection details and terminal preview", detail_container)
        self.detail_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        detail_layout.addWidget(self.detail_label)

        self.terminal_placeholder = QTextEdit(detail_container)
        self.terminal_placeholder.setReadOnly(True)
        self.terminal_placeholder.setPlaceholderText("Terminal output will appear here")
        self.terminal_placeholder.setMinimumHeight(200)
        detail_layout.addWidget(self.terminal_placeholder, stretch=1)

        root_layout.addWidget(detail_container, stretch=1)
        self.setCentralWidget(central)

        status_bar = QStatusBar(self)
        status_bar.showMessage("Disconnected")
        self.setStatusBar(status_bar)

    def _apply_style_overrides(self) -> None:
        stylesheet = load_stylesheet("qt-preview.css")
        if stylesheet:
            self.setStyleSheet(stylesheet)

    def closeEvent(self, event: QCloseEvent) -> None:  # pragma: no cover - UI hook
        event.accept()
        super().closeEvent(event)
