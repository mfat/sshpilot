from types import SimpleNamespace
from unittest.mock import MagicMock

from sshpilot.effective_config_dialog import (
    EffectiveConfigDialog,
    _diff_rows,
)


def test_for_result_reuses_precomputed_diff():
    result = {
        "has_diff": True,
        "changes": [{"key": "identityfile"}],
        "own": ["identityfile /keys/host"],
        "full": ["identityfile /keys/global", "identityfile /keys/host"],
    }

    class _Dialog:
        def __init__(self, parent, **kwargs):
            self.parent = parent
            self.kwargs = kwargs
            self.presented = False

        def present(self):
            self.presented = True

    dialog = EffectiveConfigDialog.for_result.__func__(
        _Dialog, "parent", "US", result)

    assert dialog.kwargs == {"host": "US", "result": result}
    assert dialog.presented is True


def test_full_view_toggle_offers_return_to_differences():
    dialog = SimpleNamespace(_render=MagicMock())
    button = MagicMock()
    button.get_active.return_value = True

    EffectiveConfigDialog._on_view_toggled(dialog, button)

    button.set_label.assert_called_once_with("Show differences only")
    dialog._render.assert_called_once_with()

    button.reset_mock()
    dialog._render.reset_mock()
    button.get_active.return_value = False

    EffectiveConfigDialog._on_view_toggled(dialog, button)

    button.set_label.assert_called_once_with("Show full configuration")
    dialog._render.assert_called_once_with()


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
