"""Inherited values in the connection editor, and adopting them.

OpenSSH resolves ``ssh <alias>`` with first-obtained-value-wins, so a global
block can supply any directive this Host block did not author. The editor shows
that resolved value in the row, dimmed, so the form states what the session will
actually use — and editing the row adopts the value for this host.

Adoption cannot be detected by comparing text: the row already holds the
inherited value, so pinning it (``Port 22`` under a global ``Port 2222``)
changes nothing. It is tracked from the edit itself.
"""

import types

import pytest

from sshpilot.connection_dialog import ConnectionDialog


def _real_gtk_available():
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw
        Adw.init()
        return type(Adw.PreferencesGroup()).__name__ == "PreferencesGroup"
    except Exception:
        return False


REAL_GTK = _real_gtk_available()
needs_adw = pytest.mark.skipif(
    not REAL_GTK, reason="needs real libadwaita (gi is stubbed under the suite)"
)


def _entry_row(title):
    from gi.repository import Adw

    return Adw.EntryRow(title=title)


def _dialog(**rows):
    """A stand-in carrying only what the inherited-value helpers touch."""
    dialog = types.SimpleNamespace(
        _inherited_row_handlers={},
        _adopted_inherited_fields=set(),
        _applying_inherited_value=False,
        _loading_connection_data=False,
        _INHERITED_ROW_FIELDS=ConnectionDialog._INHERITED_ROW_FIELDS,
        **rows,
    )
    # The `changed` handler calls back through self; bind it so the tests
    # exercise the real signal path rather than only the helpers.
    dialog._on_inherited_row_edited = (
        lambda name: ConnectionDialog._on_inherited_row_edited(dialog, name)
    )
    return dialog


@needs_adw
def test_inherited_value_is_shown_in_the_row_and_dimmed():
    row = _entry_row("Port")
    dialog = _dialog(port_row=row)

    ConnectionDialog._show_inherited_value(dialog, "port_row", row, "2222")

    assert row.get_text() == "2222"
    assert "dim-label" in row.get_css_classes()
    assert row.get_tooltip_text()
    # Filling the row must not itself count as the user adopting the value.
    assert dialog._adopted_inherited_fields == set()


@needs_adw
def test_editing_an_inherited_row_adopts_it_and_drops_the_dim_style():
    row = _entry_row("Port")
    dialog = _dialog(port_row=row)
    ConnectionDialog._show_inherited_value(dialog, "port_row", row, "2222")

    ConnectionDialog._on_inherited_row_edited(dialog, "port_row")

    assert dialog._adopted_inherited_fields == {"port"}
    assert "dim-label" not in row.get_css_classes()
    assert row.get_tooltip_text() is None


def test_adopted_field_is_saved_even_though_its_text_did_not_change():
    """The reported case: typing 22 where 22 was already shown as inherited."""
    dialog = _dialog()
    dialog._editor_delta_baseline = {"port": 22, "username": "alice"}
    dialog._adopted_inherited_fields = {"port"}

    changed = ConnectionDialog._changed_editor_fields(
        dialog, {"port": 22, "username": "alice"}
    )

    assert "port" in changed
    assert "username" not in changed


def test_untouched_inherited_value_is_not_written_back():
    dialog = _dialog()
    dialog._editor_delta_baseline = {"port": 2222, "username": "tom"}
    dialog._adopted_inherited_fields = set()

    changed = ConnectionDialog._changed_editor_fields(
        dialog, {"port": 2222, "username": "tom"}
    )

    assert changed == ()


@needs_adw
def test_clearing_inherited_row_state_restores_the_row():
    row = _entry_row("Port")
    dialog = _dialog(port_row=row)
    ConnectionDialog._show_inherited_value(dialog, "port_row", row, "2222")

    ConnectionDialog._clear_inherited_row_state(dialog)

    assert "dim-label" not in row.get_css_classes()
    assert dialog._inherited_row_handlers == {}


@needs_adw
def test_pinning_the_identical_inherited_value_still_adopts_it():
    """`Port 22` under a global `Port 2222` changes no text — it must still count."""
    row = _entry_row("Port")
    dialog = _dialog(port_row=row)
    ConnectionDialog._show_inherited_value(dialog, "port_row", row, "2222")

    row.set_text("2222")  # the user retypes the value shown, pinning it

    assert dialog._adopted_inherited_fields == {"port"}
    assert "dim-label" not in row.get_css_classes()


@needs_adw
def test_user_edit_via_the_signal_adopts_the_field():
    row = _entry_row("Port")
    dialog = _dialog(port_row=row)
    ConnectionDialog._show_inherited_value(dialog, "port_row", row, "2222")

    row.set_text("22")

    assert dialog._adopted_inherited_fields == {"port"}
