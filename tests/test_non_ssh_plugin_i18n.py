"""Frontend rendering checks for built-in non-SSH plugin fields."""

from types import SimpleNamespace

from sshpilot import connection_dialog
from sshpilot.connection_dialog import ConnectionDialog
from sshpilot.plugins.api import FieldSpec


def test_plugin_advanced_group_title_is_localized_without_changing_key(monkeypatch):
    class _Group:
        def __init__(self, *, title):
            self.title = title
            self.rows = []

        def add(self, row):
            self.rows.append(row)

    class _SwitchRow:
        def set_title(self, title):
            self.title = title

        def set_active(self, active):
            self.active = active

    backend = SimpleNamespace(
        protocol_id="demo",
        display_name="Demo",
        connection_fields=lambda: [
            FieldSpec(
                key="option",
                label="Option",
                kind="switch",
                default=False,
                group="advanced",
            )
        ],
    )
    dialog = SimpleNamespace(_plugin_field_widgets={})
    monkeypatch.setattr(
        connection_dialog.Adw, "PreferencesGroup", _Group, raising=False
    )
    monkeypatch.setattr(
        connection_dialog.Adw, "SwitchRow", _SwitchRow, raising=False
    )
    monkeypatch.setattr(
        connection_dialog, "_", lambda msgid: f"translated:{msgid}"
    )

    groups = ConnectionDialog._build_plugin_field_rows(dialog, backend)

    assert groups[0].title == "translated:Advanced"
    spec = dialog._plugin_field_widgets["option"][0]
    assert spec.group == "advanced"
