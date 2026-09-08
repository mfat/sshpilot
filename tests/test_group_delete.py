"""Deleting groups: multi-selection, and the group-only vs cascade choice.

Groups nest, so "delete this group" is ambiguous the moment a group holds a
subgroup. The prompt therefore offers two outcomes and these tests pin both:

* ``move`` deletes only the groups the user picked. Anything inside — a
  subgroup or a connection — survives and moves up to the parent. This is
  what deleting a group has always done.
* ``delete_all`` deletes the whole subtree: the picked groups, every
  descendant group, and every connection anywhere inside.

The planner (:func:`plan_group_delete`) is a pure function over the
``GroupManager`` projection and is tested directly; the action is driven
through a fake window with recording ``Adw``/``Gtk`` stand-ins, so no display
is required.
"""

import types

import pytest

from sshpilot.actions import (
    GroupDeletePlan,
    WindowActions,
    _describe_group_contents,
    plan_group_delete,
)


# --- fixtures -------------------------------------------------------------


def _tree(*rows):
    """Build a GroupManager-shaped projection from ``(id, parent, conns)``."""
    return {
        group_id: {
            "id": group_id,
            "name": group_id.upper(),
            "parent_id": parent,
            "connections": list(connections),
        }
        for group_id, parent, connections in rows
    }


# --- planner --------------------------------------------------------------


class TestPlanGroupDelete:
    def test_lone_empty_group_plans_only_itself(self):
        plan = plan_group_delete(_tree(("a", None, [])), ["a"])

        assert plan == GroupDeletePlan(
            selected=("a",), subtree=("a",), subgroups=(), connections=(),
        )

    def test_subtree_gathers_descendants_at_every_depth(self):
        groups = _tree(
            ("a", None, []),
            ("b", "a", []),
            ("c", "b", []),
            ("d", "a", []),
            ("z", None, []),
        )

        plan = plan_group_delete(groups, ["a"])

        assert plan.selected == ("a",)
        assert set(plan.subtree) == {"a", "b", "c", "d"}
        assert set(plan.subgroups) == {"b", "c", "d"}
        assert "z" not in plan.subtree

    def test_subtree_is_ordered_deepest_first(self):
        """Deleting a child before its parent keeps every parent_id valid."""
        groups = _tree(("a", None, []), ("b", "a", []), ("c", "b", []))

        plan = plan_group_delete(groups, ["a"])

        assert plan.subtree == ("c", "b", "a")

    def test_selected_groups_are_ordered_deepest_first_too(self):
        groups = _tree(("a", None, []), ("b", "a", []), ("c", "b", []))

        plan = plan_group_delete(groups, ["a", "c", "b"])

        assert plan.selected == ("c", "b", "a")

    def test_nested_selection_is_absorbed_not_deleted_twice(self):
        """A marquee selection routinely picks a group and its own subgroup."""
        groups = _tree(("a", None, []), ("b", "a", []))

        plan = plan_group_delete(groups, ["a", "b"])

        assert plan.subtree == ("b", "a")
        # Both were picked, so a cascade adds nothing the user did not choose.
        assert plan.subgroups == ()

    def test_connections_are_collected_across_the_whole_subtree(self):
        groups = _tree(
            ("a", None, ["one"]),
            ("b", "a", ["two"]),
            ("c", "b", ["three"]),
        )

        plan = plan_group_delete(groups, ["a"])

        assert set(plan.connections) == {"one", "two", "three"}

    def test_a_connection_in_two_deleted_groups_is_listed_once(self):
        groups = _tree(("a", None, ["shared"]), ("b", "a", ["shared"]))

        plan = plan_group_delete(groups, ["a"])

        assert plan.connections == ("shared",)

    def test_unknown_connections_are_dropped_from_the_plan(self):
        """Stale membership must not inflate the count or the delete steps."""
        groups = _tree(("a", None, ["live", "ghost"]))

        plan = plan_group_delete(groups, ["a"], {"live"})

        assert plan.connections == ("live",)

    def test_unknown_group_ids_are_ignored(self):
        plan = plan_group_delete(_tree(("a", None, [])), ["a", "gone"])

        assert plan.selected == ("a",)

    def test_duplicate_selection_ids_collapse(self):
        plan = plan_group_delete(_tree(("a", None, [])), ["a", "a"])

        assert plan.selected == ("a",)

    def test_multiple_unrelated_roots_are_planned_together(self):
        groups = _tree(
            ("a", None, ["one"]),
            ("b", "a", []),
            ("x", None, ["two"]),
            ("y", "x", ["three"]),
            ("untouched", None, ["four"]),
        )

        plan = plan_group_delete(groups, ["a", "x"])

        assert set(plan.selected) == {"a", "x"}
        assert set(plan.subtree) == {"a", "b", "x", "y"}
        assert set(plan.connections) == {"one", "two", "three"}
        assert "four" not in plan.connections

    def test_a_parent_id_cycle_does_not_hang(self):
        """A corrupt snapshot must not spin the delete path forever."""
        groups = _tree(("a", "b", []), ("b", "a", []))

        plan = plan_group_delete(groups, ["a"])

        assert set(plan.subtree) == {"a", "b"}


class TestDescribeGroupContents:
    def test_connections_only(self):
        assert _describe_group_contents(2, 0) == "2 connection(s)"

    def test_subgroups_only(self):
        assert _describe_group_contents(0, 3) == "3 subgroup(s)"

    def test_both_are_joined(self):
        assert _describe_group_contents(2, 3) == "2 connection(s) and 3 subgroup(s)"


# --- action ---------------------------------------------------------------


class RecordingDialog:
    """Records how one delete prompt was built, and replays a response."""

    instances = []

    def __init__(self, *args, **kwargs):
        self.heading = kwargs.get("heading")
        self.body = kwargs.get("body")
        self.responses = []
        self.labels = {}
        self.destructive = []
        self.default_response = None
        self.handler = None
        self.presented = False
        RecordingDialog.instances.append(self)

    def add_response(self, response_id, label):
        self.responses.append(response_id)
        self.labels[response_id] = label

    def set_response_appearance(self, response_id, _appearance):
        self.destructive.append(response_id)

    def set_default_response(self, response_id):
        self.default_response = response_id

    def connect(self, signal, handler):
        assert signal == "response"
        self.handler = handler

    def present(self):
        self.presented = True

    def destroy(self):
        pass

    def respond(self, response_id):
        self.handler(self, response_id)


class FakeRow:
    def __init__(self, group_id, is_tag_group=False):
        self.group_id = group_id
        self.is_tag_group = is_tag_group


class FakeConnection:
    def __init__(self, nickname, connection_id=None):
        self.nickname = nickname
        self.id = connection_id or nickname


class FakeClient:
    def __init__(self):
        self.calls = []

    def delete_group(self, group_id):
        self.calls.append(("delete_group", group_id))
        return True

    def delete_connection(self, request):
        self.calls.append(("delete_connection", str(request.connection_id)))
        return True


class FakeController:
    """Runs a submitted sequence inline so the test can read the call order."""

    def __init__(self):
        self.client = FakeClient()
        self.sequences = []

    def run_sequence(self, steps, on_success=None, on_error=None):
        self.sequences.append(steps)
        result = None
        try:
            for step in steps:
                result = step(result)
        except Exception as error:  # pragma: no cover - defensive
            if on_error:
                on_error(error)
            return
        if on_success:
            on_success(result)


class FakeGroupManager:
    def __init__(self, groups, controller):
        self.groups = groups
        self.controller = controller


class FakeConnectionManager:
    def __init__(self, connections):
        self._connections = connections

    def get_connections(self):
        return list(self._connections)


class FakeWindow(WindowActions):
    """Enough of MainWindow for the delete action to run headless."""

    def __init__(self, groups, rows, connections, controller=None):
        self.group_manager = FakeGroupManager(groups, controller)
        self.connection_manager = FakeConnectionManager(connections)
        self._rows = rows
        self._context_menu_group_row = None
        self._context_menu_group_rows = None
        self.rebuilt = 0
        self.simple_dialogs = []

    # Selection plumbing lives on MainWindow; the action only needs this much.
    def _get_target_group_rows(self, prefer_context=False):
        return list(self._rows)

    def rebuild_connection_list(self):
        self.rebuilt += 1

    def _simple_dialog(self, heading, body):
        self.simple_dialogs.append((heading, body))


@pytest.fixture(autouse=True)
def stub_adw(monkeypatch):
    """Replace Adw.MessageDialog with a recorder and silence focus marking."""
    RecordingDialog.instances = []
    import sshpilot.actions as actions

    fake_adw = types.SimpleNamespace(
        MessageDialog=RecordingDialog,
        ResponseAppearance=types.SimpleNamespace(DESTRUCTIVE="destructive"),
    )
    monkeypatch.setattr(actions, "Adw", fake_adw)
    monkeypatch.setattr(actions, "mark_default_response_visible", lambda _w: None)
    yield
    RecordingDialog.instances = []


def only_dialog():
    assert len(RecordingDialog.instances) == 1, (
        f"expected exactly one dialog, got {len(RecordingDialog.instances)}"
    )
    return RecordingDialog.instances[0]


def _window(groups, selected, connections=(), controller=None):
    controller = controller if controller is not None else FakeController()
    rows = [FakeRow(group_id) for group_id in selected]
    return FakeWindow(groups, rows, list(connections), controller), controller


class TestDeleteGroupPrompt:
    def test_empty_group_gets_a_plain_confirmation(self):
        window, controller = _window(_tree(("a", None, [])), ["a"])

        window.on_delete_group_action(None)

        dialog = only_dialog()
        assert dialog.heading == "Delete Group"
        assert "empty group 'A'" in dialog.body
        assert dialog.responses == ["cancel", "delete"]
        assert dialog.default_response == "cancel"

        dialog.respond("delete")
        assert controller.client.calls == [("delete_group", "a")]

    def test_cancelling_an_empty_group_deletes_nothing(self):
        window, controller = _window(_tree(("a", None, [])), ["a"])

        window.on_delete_group_action(None)
        only_dialog().respond("cancel")

        assert controller.client.calls == []

    def test_several_empty_groups_are_confirmed_together(self):
        groups = _tree(("a", None, []), ("b", None, []))
        window, controller = _window(groups, ["a", "b"])

        window.on_delete_group_action(None)

        dialog = only_dialog()
        assert dialog.heading == "Delete Groups"
        assert "2 selected empty groups" in dialog.body

        dialog.respond("delete")
        assert {call[1] for call in controller.client.calls} == {"a", "b"}

    def test_a_subgroup_alone_still_forces_the_choice(self):
        """An empty group holding an empty subgroup is not an empty delete."""
        groups = _tree(("a", None, []), ("b", "a", []))
        window, _controller = _window(groups, ["a"])

        window.on_delete_group_action(None)

        dialog = only_dialog()
        assert dialog.responses == ["cancel", "move", "delete_all"]
        assert "1 subgroup(s)" in dialog.body
        assert "connection(s)" not in dialog.body

    def test_the_choice_prompt_counts_connections_and_subgroups(self):
        groups = _tree(("a", None, ["one"]), ("b", "a", ["two"]))
        window, _controller = _window(
            groups, ["a"], [FakeConnection("one"), FakeConnection("two")],
        )

        window.on_delete_group_action(None)

        dialog = only_dialog()
        assert "2 connection(s) and 1 subgroup(s)" in dialog.body
        assert dialog.labels["move"] == "Delete Group Only"
        assert dialog.labels["delete_all"] == "Delete Group and Contents"
        assert dialog.destructive == ["delete_all"]
        assert dialog.default_response == "move"

    def test_the_prompt_is_pluralised_for_a_multi_selection(self):
        groups = _tree(("a", None, ["one"]), ("b", None, ["two"]))
        window, _controller = _window(
            groups, ["a", "b"], [FakeConnection("one"), FakeConnection("two")],
        )

        window.on_delete_group_action(None)

        dialog = only_dialog()
        assert dialog.heading == "Delete Groups"
        assert "The 2 selected groups contain" in dialog.body
        assert dialog.labels["move"] == "Delete Groups Only"
        assert dialog.labels["delete_all"] == "Delete Groups and Contents"

    def test_stale_membership_is_left_out_of_the_count(self):
        groups = _tree(("a", None, ["live", "ghost"]), ("b", "a", []))
        window, _controller = _window(groups, ["a"], [FakeConnection("live")])

        window.on_delete_group_action(None)

        assert "1 connection(s)" in only_dialog().body


class TestDeleteGroupOnly:
    def test_only_the_chosen_group_is_deleted(self):
        groups = _tree(("a", None, ["one"]), ("b", "a", ["two"]))
        window, controller = _window(
            groups, ["a"], [FakeConnection("one"), FakeConnection("two")],
        )

        window.on_delete_group_action(None)
        only_dialog().respond("move")

        assert controller.client.calls == [("delete_group", "a")]
        assert window.rebuilt == 1

    def test_every_selected_group_is_deleted_deepest_first(self):
        groups = _tree(("a", None, []), ("b", "a", []), ("c", "b", ["one"]))
        window, controller = _window(groups, ["a", "b"], [FakeConnection("one")])

        window.on_delete_group_action(None)
        only_dialog().respond("move")

        assert controller.client.calls == [
            ("delete_group", "b"),
            ("delete_group", "a"),
        ]

    def test_cancelling_the_choice_deletes_nothing(self):
        groups = _tree(("a", None, ["one"]), ("b", "a", []))
        window, controller = _window(groups, ["a"], [FakeConnection("one")])

        window.on_delete_group_action(None)
        only_dialog().respond("cancel")

        assert controller.client.calls == []
        assert window.rebuilt == 0


class TestDeleteGroupAndContents:
    def test_connections_go_before_groups(self):
        """A connection owns an ssh config block: delete it through its own API."""
        groups = _tree(("a", None, ["one"]), ("b", "a", ["two"]))
        window, controller = _window(
            groups, ["a"], [FakeConnection("one"), FakeConnection("two")],
        )

        window.on_delete_group_action(None)
        only_dialog().respond("delete_all")

        kinds = [call[0] for call in controller.client.calls]
        assert kinds == ["delete_connection"] * 2 + ["delete_group"] * 2
        assert {call[1] for call in controller.client.calls[:2]} == {"one", "two"}
        assert [call[1] for call in controller.client.calls[2:]] == ["b", "a"]

    def test_the_whole_subtree_is_removed(self):
        groups = _tree(
            ("a", None, []),
            ("b", "a", []),
            ("c", "b", ["deep"]),
            ("z", None, ["safe"]),
        )
        window, controller = _window(
            groups, ["a"], [FakeConnection("deep"), FakeConnection("safe")],
        )

        window.on_delete_group_action(None)
        only_dialog().respond("delete_all")

        assert controller.client.calls == [
            ("delete_connection", "deep"),
            ("delete_group", "c"),
            ("delete_group", "b"),
            ("delete_group", "a"),
        ]

    def test_a_nested_pick_is_not_deleted_twice(self):
        groups = _tree(("a", None, []), ("b", "a", ["one"]))
        window, controller = _window(groups, ["a", "b"], [FakeConnection("one")])

        window.on_delete_group_action(None)
        only_dialog().respond("delete_all")

        assert controller.client.calls == [
            ("delete_connection", "one"),
            ("delete_group", "b"),
            ("delete_group", "a"),
        ]

    def test_membership_recorded_by_id_resolves_to_the_nickname(self):
        groups = _tree(("a", None, ["conn-7"]))
        window, controller = _window(
            groups, ["a"], [FakeConnection("server", connection_id="conn-7")],
        )

        window.on_delete_group_action(None)
        only_dialog().respond("delete_all")

        assert controller.client.calls == [
            ("delete_connection", "server"),
            ("delete_group", "a"),
        ]

    def test_a_shared_connection_is_deleted_once(self):
        groups = _tree(("a", None, ["shared"]), ("b", "a", ["shared"]))
        window, controller = _window(groups, ["a"], [FakeConnection("shared")])

        window.on_delete_group_action(None)
        only_dialog().respond("delete_all")

        assert controller.client.calls.count(("delete_connection", "shared")) == 1


class TestDeleteGroupGuards:
    def test_no_target_rows_shows_no_dialog(self):
        window, controller = _window(_tree(("a", None, [])), [])

        window.on_delete_group_action(None)

        assert RecordingDialog.instances == []
        assert controller.client.calls == []

    def test_rows_for_vanished_groups_show_no_dialog(self):
        window, controller = _window(_tree(("a", None, [])), ["gone"])

        window.on_delete_group_action(None)

        assert RecordingDialog.instances == []
        assert controller.client.calls == []

    def test_without_a_daemon_controller_the_user_is_told(self):
        groups = _tree(("a", None, []))
        window = FakeWindow(groups, [FakeRow("a")], [], controller=None)
        window.group_manager.controller = None

        window.on_delete_group_action(None)

        assert RecordingDialog.instances == []
        assert window.simple_dialogs == [
            (
                "Service unavailable",
                "Connect to the SSH Pilot daemon before deleting groups.",
            )
        ]

    def test_a_failing_step_reports_and_stops(self):
        controller = FakeController()

        def _boom(group_id):
            raise RuntimeError("daemon said no")

        controller.client.delete_group = _boom
        groups = _tree(("a", None, []))
        window, _ = _window(groups, ["a"], controller=controller)

        window.on_delete_group_action(None)
        only_dialog().respond("delete")

        assert window.rebuilt == 0
        assert window.simple_dialogs == [
            ("Error", "Failed to delete group: daemon said no")
        ]
