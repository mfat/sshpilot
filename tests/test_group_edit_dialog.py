"""Regression tests for editing group metadata from the GTK dialog."""

from types import SimpleNamespace

from sshpilot.window_dialogs import WindowConfigDialogsMixin


class _Client:
    def __init__(self):
        self.calls = []

    def rename_group(self, group_id, name):
        self.calls.append(("rename_group", group_id, name))
        return True

    def set_group_color(self, request):
        self.calls.append(("set_group_color", request))
        return True


class _Controller:
    def __init__(self):
        self.client = _Client()

    def run_sequence(self, steps, *, on_success, on_error):
        try:
            result = None
            for step in steps:
                result = step(result)
        except Exception as error:
            on_error(error)
        else:
            on_success(result)


class _Host(WindowConfigDialogsMixin):
    def __init__(self):
        self.controller = _Controller()
        self.group_manager = SimpleNamespace(
            controller=self.controller,
            groups={"group-1": {"name": "Production", "color": "#ff0000"}},
        )
        self._context_menu_group_row = SimpleNamespace(group_id="group-1")
        self.confirm_edit = None
        self.rebuilds = 0

    def _group_form_dialog(self, **kwargs):
        self.confirm_edit = kwargs["on_confirm"]

    def rebuild_connection_list(self):
        self.rebuilds += 1


def test_edit_group_clear_color_sends_empty_color():
    host = _Host()
    host.on_edit_group_action(None)

    host.confirm_edit("Production", None)

    assert host.controller.client.calls[0] == (
        "rename_group",
        "group-1",
        "Production",
    )
    method, request = host.controller.client.calls[1]
    assert method == "set_group_color"
    assert request.group_id == "group-1"
    assert request.color == ""
    assert host.rebuilds == 1
