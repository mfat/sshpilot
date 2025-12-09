"""Qt dialog for creating and editing SSH connections."""

from __future__ import annotations

from typing import Dict, Optional
from PyQt6.QtGui import QIntValidator
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from sshpilot.connection_manager import Connection, ConnectionManager


class ConnectionEditorDialog(QDialog):
    """Edit or create connections using Qt widgets."""

    def __init__(
        self,
        manager: ConnectionManager,
        *,
        connection: Optional[Connection] = None,
        parent=None,
    ):
        super().__init__(parent)
        self.manager = manager
        self.connection = connection
        self.setWindowTitle("Edit Connection" if connection else "New Connection")
        self.setModal(True)

        self.nickname = QLineEdit(self)
        self.hostname = QLineEdit(self)
        self.username = QLineEdit(self)
        self.port = QLineEdit(self)
        self.port.setValidator(QIntValidator(1, 65535, self))
        self.private_key = QLineEdit(self)
        self.certificate = QLineEdit(self)
        self.quick_command = QLineEdit(self)
        self.password = QLineEdit(self)
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.group = QLineEdit(self)
        self.forward_agent = QCheckBox("Forward SSH agent", self)

        self._build_form()
        self._populate_from_connection()

    def _build_form(self):
        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.addRow("Nickname", self.nickname)
        form.addRow("Hostname", self.hostname)
        form.addRow("Username", self.username)
        form.addRow("Port", self.port)
        form.addRow("Group", self.group)

        key_row = QHBoxLayout()
        key_row.addWidget(self.private_key)
        key_button = QPushButton("Browse…", self)
        key_button.clicked.connect(self._select_keyfile)
        key_row.addWidget(key_button)
        form.addRow("Private key", key_row)

        cert_row = QHBoxLayout()
        cert_row.addWidget(self.certificate)
        cert_button = QPushButton("Browse…", self)
        cert_button.clicked.connect(self._select_certificate)
        cert_row.addWidget(cert_button)
        form.addRow("Certificate", cert_row)

        form.addRow("Quick connect", self.quick_command)
        form.addRow("Password", self.password)
        form.addRow("Agent forwarding", self.forward_agent)
        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.setLayout(layout)

    def _populate_from_connection(self):
        if not self.connection:
            return
        self.nickname.setText(getattr(self.connection, "nickname", ""))
        self.hostname.setText(getattr(self.connection, "hostname", ""))
        self.username.setText(getattr(self.connection, "username", ""))
        self.port.setText(str(getattr(self.connection, "port", 22)))
        self.private_key.setText(getattr(self.connection, "keyfile", ""))
        self.certificate.setText(getattr(self.connection, "certificate", ""))
        self.quick_command.setText(getattr(self.connection, "quick_connect_command", ""))
        self.forward_agent.setChecked(bool(getattr(self.connection, "forward_agent", False)))
        data = getattr(self.connection, "data", {}) or {}
        if isinstance(data, dict):
            self.group.setText(str(data.get("group", "")))
            self.password.setText(str(data.get("password", "")))

    def _select_keyfile(self):
        filename, _ = QFileDialog.getOpenFileName(self, "Select private key")
        if filename:
            self.private_key.setText(filename)

    def _select_certificate(self):
        filename, _ = QFileDialog.getOpenFileName(self, "Select certificate")
        if filename:
            self.certificate.setText(filename)

    def _validate(self) -> Optional[str]:
        if not self.nickname.text().strip():
            return "Nickname is required."
        if not self.hostname.text().strip():
            return "Hostname is required."
        if self.port.text().strip() and not self.port.hasAcceptableInput():
            return "Port must be between 1 and 65535."
        return None

    def _collect_payload(self) -> Dict[str, object]:
        payload: Dict[str, object] = {
            "nickname": self.nickname.text().strip(),
            "hostname": self.hostname.text().strip(),
            "username": self.username.text().strip(),
            "port": int(self.port.text()) if self.port.text().strip() else 22,
            "keyfile": self.private_key.text().strip(),
            "certificate": self.certificate.text().strip(),
            "quick_connect_command": self.quick_command.text().strip(),
            "forward_agent": self.forward_agent.isChecked(),
        }
        password = self.password.text()
        group_name = self.group.text().strip()
        if password:
            payload["password"] = password
        if group_name:
            payload.setdefault("group", group_name)
        return payload

    def _on_accept(self):
        error = self._validate()
        if error:
            QMessageBox.critical(self, "Invalid connection", error)
            return

        payload = self._collect_payload()
        if self.connection:
            self.manager.update_connection(self.connection, payload)
        else:
            new_connection = Connection(payload)
            self.manager.connections.append(new_connection)
            self.manager.update_ssh_config_file(new_connection, payload, payload.get("nickname"))
            self.manager.emit("connection-added", new_connection)
        self.accept()
