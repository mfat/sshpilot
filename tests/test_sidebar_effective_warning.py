from unittest.mock import MagicMock

from sshpilot.sidebar import ConnectionRow


class _Row:
    _is_hovering = False
    _effective_warning_differs = False
    _update_effective_warning_reveal = (
        ConnectionRow._update_effective_warning_reveal
    )
    set_effective_warning = ConnectionRow.set_effective_warning

    def __init__(self):
        self.effective_warning_icon = MagicMock()


def test_warning_is_visible_only_while_differing_row_is_hovered():
    row = _Row()

    row.set_effective_warning(True)
    row.effective_warning_icon.set_visible.assert_called_with(True)
    row.effective_warning_icon.set_opacity.assert_called_with(0.0)

    row._is_hovering = True
    row._update_effective_warning_reveal()
    row.effective_warning_icon.set_opacity.assert_called_with(1.0)

    row._is_hovering = False
    row._update_effective_warning_reveal()
    row.effective_warning_icon.set_opacity.assert_called_with(0.0)


def test_clearing_difference_hides_warning_even_on_hover():
    row = _Row()
    row._is_hovering = True

    row.set_effective_warning(False)

    row.effective_warning_icon.set_visible.assert_called_with(False)
    row.effective_warning_icon.set_opacity.assert_called_with(0.0)
