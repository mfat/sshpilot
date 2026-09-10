"""Connection-row status lock lives in ``status_box``, like forwarding badges
in ``indicator_box``: the box is shown only when there is a real status to
draw, and hidden while idle / preference-off / compact.
"""

import importlib
from types import SimpleNamespace
from unittest.mock import MagicMock

from sshpilot.connection_model import ConnectionState


def _row(*, state=ConnectionState.UNKNOWN, show_status=True, compact=False):
    mod = importlib.import_module('sshpilot.sidebar')
    row = mod.ConnectionRow.__new__(mod.ConnectionRow)
    row._compact = compact
    row.config = SimpleNamespace(
        get_setting=lambda key, default=None: (
            show_status if key == 'ui.sidebar_show_connection_status' else default
        )
    )
    row.connection = SimpleNamespace(nickname='host')
    row.status_box = MagicMock(name='status_box')
    row.status_icon = MagicMock(name='status_icon')
    row._resolve_status = lambda: (state, '')
    row._install_status_css = lambda: None
    row._apply_group_color_style = MagicMock()
    row._refresh_compact_status = MagicMock()
    return row, mod


def test_idle_hides_the_status_box():
    row, mod = _row(state=ConnectionState.UNKNOWN)

    mod.ConnectionRow.update_status(row)

    row.status_box.set_visible.assert_called_with(False)


def test_connected_shows_the_status_box():
    row, mod = _row(state=ConnectionState.CONNECTED)

    mod.ConnectionRow.update_status(row)

    row.status_box.set_visible.assert_called_with(True)


def test_status_pref_off_hides_the_status_box():
    row, mod = _row(state=ConnectionState.CONNECTED, show_status=False)

    mod.ConnectionRow.update_status(row)

    row.status_box.set_visible.assert_called_with(False)


def test_compact_strip_hides_the_status_box():
    row, mod = _row(state=ConnectionState.CONNECTED, compact=True)

    mod.ConnectionRow.update_status(row)

    row.status_box.set_visible.assert_called_with(False)
    row._refresh_compact_status.assert_called()
