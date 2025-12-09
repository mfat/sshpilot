"""Qt terminal pane supporting QTermWidget when available."""

from __future__ import annotations

import importlib.util
import os
import pty
import fcntl
import termios
import struct
import subprocess
from typing import Dict, Optional

from PyQt6.QtCore import QEvent, QSocketNotifier, Qt
from PyQt6.QtGui import QAction, QColor, QFont
from PyQt6.QtWidgets import QPlainTextEdit, QVBoxLayout, QWidget

_QTERM_SPEC = importlib.util.find_spec("qtermwidget")
if _QTERM_SPEC and _QTERM_SPEC.loader:
    _qterm_module = importlib.util.module_from_spec(_QTERM_SPEC)
    _QTERM_SPEC.loader.exec_module(_qterm_module)
    QTermWidget = getattr(_qterm_module, "QTermWidget", None)
else:
    QTermWidget = None


class _PtyTerminal(QWidget):
    """Fallback terminal using a PTY and QPlainTextEdit."""

    def __init__(self, *, command: Optional[str] = None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.command = command or os.environ.get("SHELL", "/bin/bash")
        self.text = QPlainTextEdit(self)
        self.text.setReadOnly(False)
        self.text.setUndoRedoEnabled(False)
        self.text.setTabChangesFocus(False)
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)

        layout = QVBoxLayout(self)
        layout.addWidget(self.text)
        self.setLayout(layout)

        self.master_fd, self.slave_fd = pty.openpty()
        self.notifier = QSocketNotifier(self.master_fd, QSocketNotifier.Type.Read, self)
        self.notifier.activated.connect(self._read_from_pty)
        self.process = subprocess.Popen(
            self.command,
            stdin=self.slave_fd,
            stdout=self.slave_fd,
            stderr=self.slave_fd,
            shell=True,
            close_fds=True,
        )
        self.text.installEventFilter(self)
        self._apply_initial_size()

    def _apply_initial_size(self):
        columns, rows = 120, 32
        self._resize_pty(rows, columns)

    def _resize_pty(self, rows: int, columns: int):
        winsize = struct.pack("HHHH", rows, columns, 0, 0)
        fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)

    def resizeEvent(self, event):  # noqa: D401, N802
        super().resizeEvent(event)
        char_width = self.text.fontMetrics().averageCharWidth() or 9
        char_height = self.text.fontMetrics().height() or 18
        columns = max(20, int(self.text.viewport().width() / char_width))
        rows = max(2, int(self.text.viewport().height() / char_height))
        self._resize_pty(rows, columns)

    def _read_from_pty(self):
        try:
            data = os.read(self.master_fd, 4096)
            if data:
                self.text.moveCursor(self.text.textCursor().End)
                self.text.insertPlainText(data.decode(errors="ignore"))
                self.text.moveCursor(self.text.textCursor().End)
        except OSError:
            self.notifier.setEnabled(False)

    def eventFilter(self, obj, event):  # noqa: D401, N802
        if obj is self.text and event.type() == QEvent.Type.KeyPress:
            text = event.text()
            if text:
                os.write(self.master_fd, text.encode())
                return True
        return super().eventFilter(obj, event)

    def send_text(self, text: str):
        os.write(self.master_fd, text.encode())

    def copy(self):  # noqa: A003
        self.text.copy()

    def paste(self):
        self.text.paste()

    def set_color_scheme(self, scheme: Dict[str, str]):
        palette = self.text.palette()
        if "foreground" in scheme:
            palette.setColor(self.text.foregroundRole(), QColor(scheme["foreground"]))
        if "background" in scheme:
            palette.setColor(self.text.backgroundRole(), QColor(scheme["background"]))
        self.text.setPalette(palette)


class TerminalPane(QWidget):
    """Container that picks the most capable terminal backend available."""

    def __init__(self, *, color_scheme: Optional[Dict[str, str]] = None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.color_scheme = color_scheme or {}
        self.backend: QWidget
        self._init_backend()
        self._install_actions()
        if self.color_scheme:
            self.set_color_scheme(self.color_scheme)

    def _init_backend(self):
        if QTermWidget is not None:
            self.backend = QTermWidget(self)
            self.backend.setColorScheme(0)
            font = QFont("Monospace", 11)
            self.backend.setTerminalFont(font)
        else:
            self.backend = _PtyTerminal(parent=self)

        layout = QVBoxLayout(self)
        layout.addWidget(self.backend)
        layout.setContentsMargins(0, 0, 0, 0)
        self.setLayout(layout)

    def _install_actions(self):
        copy_action = QAction("Copy", self)
        copy_action.setShortcut(Qt.Modifier.CTRL | Qt.Key.Key_C)
        copy_action.triggered.connect(self.copy)
        paste_action = QAction("Paste", self)
        paste_action.setShortcut(Qt.Modifier.CTRL | Qt.Key.Key_V)
        paste_action.triggered.connect(self.paste)
        self.addAction(copy_action)
        self.addAction(paste_action)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.ActionsContextMenu)

    def send_text(self, text: str):
        if isinstance(self.backend, _PtyTerminal):
            self.backend.send_text(text)
        elif hasattr(self.backend, "sendText"):
            self.backend.sendText(text)

    def copy(self):  # noqa: A003
        if hasattr(self.backend, "copyClipboard"):
            self.backend.copyClipboard()
        elif hasattr(self.backend, "copy"):
            self.backend.copy()

    def paste(self):
        if hasattr(self.backend, "pasteClipboard"):
            self.backend.pasteClipboard()
        elif hasattr(self.backend, "paste"):
            self.backend.paste()

    def set_color_scheme(self, scheme: Dict[str, str]):
        self.color_scheme = scheme
        if isinstance(self.backend, _PtyTerminal):
            self.backend.set_color_scheme(scheme)
        elif hasattr(self.backend, "setColorScheme"):
            self.backend.setColorScheme(0)
            if "background" in scheme:
                self.backend.setColorSchemeProperty("background", QColor(scheme["background"]))
            if "foreground" in scheme:
                self.backend.setColorSchemeProperty("foreground", QColor(scheme["foreground"]))

    def resizeEvent(self, event):  # noqa: D401, N802
        super().resizeEvent(event)
        if hasattr(self.backend, "setTerminalSize"):
            columns = max(20, int(self.width() / 8))
            rows = max(2, int(self.height() / 16))
            self.backend.setTerminalSize(columns, rows)
