from types import SimpleNamespace

import pytest

from tests._gui_harness import requires_gui

requires_gui()
pytestmark = pytest.mark.gui


def test_group_context_markup_is_safe_and_keeps_multiple_memberships():
    from sshpilot.search_groups import make_group_context_label

    manager = SimpleNamespace(
        get_connection_groups=lambda conn: ["a", "b"],
        groups={
            "a": {"name": "<Production> & API", "color": "#ff0000"},
            "b": {"name": "Shared", "color": "invalid"},
        },
    )
    label = make_group_context_label(manager, object())
    assert label.get_text() == "● <Production> & API · Shared"
    assert 'foreground="#ff0000"' in label.get_label()
    assert label.get_tooltip_text() == "<Production> & API\nShared"


def test_omni_connection_row_displays_group_context():
    from sshpilot.omni_search import OmniResult, OmniSearchController

    manager = SimpleNamespace(
        get_connection_groups=lambda conn: ["prod"],
        groups={"prod": {"name": "Production", "color": "#00ff00"}},
    )
    controller = SimpleNamespace(window=SimpleNamespace(group_manager=manager))
    result = OmniResult("connection", "web", "user@host",
                        "network-server-symbolic", 100, object())
    row = OmniSearchController._make_row(controller, result)
    labels = row.get_child().get_last_child()
    assert labels.get_last_child().get_text() == "● Production"
