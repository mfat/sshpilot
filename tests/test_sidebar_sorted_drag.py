"""A drag in a sorted sidebar saves the sorted order first (issue #1312).

The drop target is computed against the order on screen. In a sorted view
that is not the manual order the daemon stores, so ``_run_dnd_mutation``
saves the sorted order as the manual order and then applies the drop on top
of it, built for the generation the save returned.
"""

import types

import sshpilot.sidebar as sidebar_module


class _Controller:
    """Runs steps synchronously, like GroupMutationController but inline."""

    def __init__(self):
        self.runs = []
        self.sequences = []

    def run(self, operation, *, on_success, on_error):
        self.runs.append(operation)
        try:
            result = operation()
        except Exception as error:
            on_error(error)
        else:
            on_success(result)

    def run_sequence(self, steps, *, on_success, on_error):
        self.sequences.append(steps)
        result = None
        try:
            for step in steps:
                result = step(result)
        except Exception as error:
            on_error(error)
        else:
            on_success(result)


class _Window(types.SimpleNamespace):
    def __init__(self, plan, *, layout_error=None):
        super().__init__()
        self.group_manager = types.SimpleNamespace(controller=_Controller())
        self.plan = plan
        self.layout_error = layout_error
        self.begin_calls = []
        self.layouts = []
        self.undo_offers = []
        self.save_failures = []
        self.errors = []
        self.show_error = self.errors.append
        self.client = types.SimpleNamespace(set_connection_layout=self._set_layout)

    def _set_layout(self, request):
        if self.layout_error is not None:
            raise self.layout_error
        self.layouts.append(request)
        return 42

    def begin_saving_sorted_view(self, expected_generation=None):
        self.begin_calls.append(expected_generation)
        return self.plan

    def offer_sorted_view_undo(self, undo, preset):
        self.undo_offers.append((undo, preset))

    def sorted_view_save_failed(self, preset):
        self.save_failures.append(preset)


def _placement(calls, error=None):
    def make_operation(generation):
        def operation():
            calls.append(generation)
            if error is not None:
                raise error
            return True
        return operation
    return make_operation


def test_manual_order_runs_the_drop_alone():
    window = _Window(plan=None)
    calls = []

    assert sidebar_module._run_dnd_mutation(window, _placement(calls), 7) is True

    assert calls == [7]
    assert window.layouts == []
    assert window.undo_offers == []


def test_sorted_view_is_saved_then_the_drop_applies_on_top():
    window = _Window(plan=("layout", "undo", "name-asc"))
    calls = []

    assert sidebar_module._run_dnd_mutation(window, _placement(calls), 7) is True

    # The save is checked against the drag's generation; the drop then uses
    # the generation the save produced.
    assert window.begin_calls == [7]
    assert window.layouts == ["layout"]
    assert calls == [42]
    assert window.undo_offers == [("undo", "name-asc")]
    assert window.errors == []


def test_a_failed_save_restores_the_sorted_view_and_skips_the_drop():
    window = _Window(plan=("layout", "undo", "name-asc"), layout_error=RuntimeError("stale"))
    calls = []

    sidebar_module._run_dnd_mutation(window, _placement(calls), 7)

    assert calls == []
    assert window.save_failures == ["name-asc"]
    assert window.undo_offers == []
    assert len(window.errors) == 1


def test_a_failed_drop_after_the_save_still_offers_undo():
    window = _Window(plan=("layout", "undo", "name-desc"))
    calls = []

    sidebar_module._run_dnd_mutation(
        window, _placement(calls, error=RuntimeError("stale")), 7
    )

    assert calls == [42]
    assert window.save_failures == []
    assert window.undo_offers == [("undo", "name-desc")]
    assert len(window.errors) == 1


def test_move_group_to_top_level_does_not_save_the_sorted_view():
    window = _Window(plan=("layout", "undo", "name-asc"))
    window.client.place_group = lambda request: True

    sidebar_module._submit_group_dnd_place(
        window, "g1", None, 3, expected_generation=7, save_sorted_view=False
    )

    assert window.begin_calls == []
    assert window.layouts == []
