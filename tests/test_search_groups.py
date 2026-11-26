from types import SimpleNamespace

from sshpilot.search_groups import connection_group_context


def test_nested_multiple_memberships_keep_names_and_topmost_colors():
    manager = SimpleNamespace(
        groups={
            "root": {"name": "Production", "color": "#ff0000"},
            "child": {"name": "Web & API", "parent_id": "root", "color": "#00ff00"},
            "other": {"name": "Shared", "color": "#0000ff"},
        },
        get_connection_groups=lambda connection: ["child", "other"],
    )
    assert connection_group_context(manager, object()) == (
        ("Production › Web & API", "#ff0000"), ("Shared", "#0000ff")
    )


def test_missing_and_cyclic_groups_terminate():
    manager = SimpleNamespace(
        groups={"a": {"name": "A", "parent_id": "a"}},
        get_connection_groups=lambda connection: ["missing", "a"],
    )
    assert connection_group_context(manager, object()) == (("A", None),)
    assert connection_group_context(None, object()) == ()
