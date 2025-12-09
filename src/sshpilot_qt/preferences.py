"""Preferences dialog implemented with Qt widgets."""

from __future__ import annotations

from typing import Dict

from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from sshpilot.config import Config


class _TerminalTab(QFormLayout):
    def __init__(self, config: Config):
        super().__init__()
        self.config = config
        terminal_settings = config.get_setting("terminal", {}) or {}

        self.theme = QComboBox()
        for key in sorted(config.terminal_themes.keys()):
            self.theme.addItem(config.terminal_themes[key]["name"], key)
        current_theme = terminal_settings.get("theme", "default")
        idx = self.theme.findData(current_theme)
        if idx >= 0:
            self.theme.setCurrentIndex(idx)

        self.font = QLineEdit(str(terminal_settings.get("font", "Monospace 12")))
        self.scrollback = QSpinBox()
        self.scrollback.setRange(1000, 500000)
        self.scrollback.setValue(int(terminal_settings.get("scrollback_lines", 10000)))
        self.cursor_blink = QCheckBox("Blinking cursor")
        self.cursor_blink.setChecked(bool(terminal_settings.get("cursor_blink", True)))
        self.audible_bell = QCheckBox("Audible bell")
        self.audible_bell.setChecked(bool(terminal_settings.get("audible_bell", False)))

        self.addRow("Theme", self.theme)
        self.addRow("Font", self.font)
        self.addRow("Scrollback", self.scrollback)
        self.addRow("Cursor blink", self.cursor_blink)
        self.addRow("Audible bell", self.audible_bell)

    def serialize(self) -> Dict[str, object]:
        return {
            "theme": self.theme.currentData(),
            "font": self.font.text(),
            "scrollback_lines": self.scrollback.value(),
            "cursor_blink": self.cursor_blink.isChecked(),
            "audible_bell": self.audible_bell.isChecked(),
        }


class _UiTab(QFormLayout):
    def __init__(self, config: Config):
        super().__init__()
        ui_settings = config.get_setting("ui", {}) or {}
        self.show_hostname = QCheckBox("Show hostname in tabs")
        self.show_hostname.setChecked(bool(ui_settings.get("show_hostname", True)))
        self.auto_focus = QCheckBox("Focus terminal on connect")
        self.auto_focus.setChecked(bool(ui_settings.get("auto_focus_terminal", True)))
        self.confirm_close = QCheckBox("Confirm before closing tabs")
        self.confirm_close.setChecked(bool(ui_settings.get("confirm_close_tabs", True)))
        self.remember_size = QCheckBox("Remember window size")
        self.remember_size.setChecked(bool(ui_settings.get("remember_window_size", True)))

        self.addRow(self.show_hostname)
        self.addRow(self.auto_focus)
        self.addRow(self.confirm_close)
        self.addRow(self.remember_size)

    def serialize(self) -> Dict[str, object]:
        return {
            "show_hostname": self.show_hostname.isChecked(),
            "auto_focus_terminal": self.auto_focus.isChecked(),
            "confirm_close_tabs": self.confirm_close.isChecked(),
            "remember_window_size": self.remember_size.isChecked(),
        }


class _SshTab(QFormLayout):
    def __init__(self, config: Config):
        super().__init__()
        ssh_settings = config.get_setting("ssh", {}) or {}
        self.compression = QCheckBox("Enable compression (-C)")
        self.compression.setChecked(bool(ssh_settings.get("compression", False)))
        self.auto_add = QCheckBox("Automatically add host keys")
        self.auto_add.setChecked(bool(ssh_settings.get("auto_add_host_keys", True)))
        self.batch_mode = QCheckBox("Batch mode")
        self.batch_mode.setChecked(bool(ssh_settings.get("batch_mode", False)))
        self.verbosity = QSpinBox()
        self.verbosity.setRange(0, 3)
        self.verbosity.setValue(int(ssh_settings.get("verbosity", 0)))

        self.addRow(self.compression)
        self.addRow(self.auto_add)
        self.addRow(self.batch_mode)
        self.addRow("Verbosity", self.verbosity)

    def serialize(self) -> Dict[str, object]:
        return {
            "compression": self.compression.isChecked(),
            "auto_add_host_keys": self.auto_add.isChecked(),
            "batch_mode": self.batch_mode.isChecked(),
            "verbosity": self.verbosity.value(),
        }


class PreferencesDialog(QDialog):
    """Qt preferences dialog that writes to the existing Config backend."""

    def __init__(self, *, parent=None):
        super().__init__(parent)
        self.config = Config()
        self.setWindowTitle("Preferences")
        self.tabs = QTabWidget(self)
        self.terminal_tab = _TerminalTab(self.config)
        self.ui_tab = _UiTab(self.config)
        self.ssh_tab = _SshTab(self.config)
        self._build_ui()

    def _build_ui(self):
        terminal_widget = QWidget()
        terminal_widget.setLayout(self.terminal_tab)

        ui_widget = QWidget()
        ui_widget.setLayout(self.ui_tab)

        ssh_widget = QWidget()
        ssh_widget.setLayout(self.ssh_tab)

        self.tabs.addTab(terminal_widget, "Terminal")
        self.tabs.addTab(ui_widget, "Interface")
        self.tabs.addTab(ssh_widget, "SSH")

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.accepted.connect(self._save_and_close)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs)
        layout.addWidget(buttons)
        self.setLayout(layout)

    def _save_and_close(self):
        self._persist("terminal", self.terminal_tab.serialize())
        self._persist("ui", self.ui_tab.serialize())
        self._persist("ssh", self.ssh_tab.serialize())
        self.accept()

    def _persist(self, section: str, values: Dict[str, object]):
        existing = self.config.get_setting(section, {}) or {}
        merged = {**existing, **values}
        self.config.set_setting(section, merged)

    def set_initial_tab(self, name: str):
        tab_map = {"terminal": 0, "interface": 1, "ssh": 2}
        if name.lower() in tab_map:
            self.tabs.setCurrentIndex(tab_map[name.lower()])
