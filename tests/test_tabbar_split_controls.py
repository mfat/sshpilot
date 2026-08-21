"""Split layout controls belong to terminal tab context menus."""


def test_tab_bar_end_action_contains_only_tab_overview_button():
    source = ("src/sshpilot/window.py")
    with open(source, encoding="utf-8") as handle:
        text = handle.read()

    assert "set_end_action_widget(self.tab_button)" in text
    assert "create_layout_toggle_buttons" not in text
    assert "layout_end_box" not in text
