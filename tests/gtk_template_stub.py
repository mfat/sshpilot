"""Shared no-op ``Gtk.Template`` stub for the gi-stubbed test environment.

Blueprint-migrated widgets use ``@Gtk.Template`` / ``Gtk.Template.Child`` /
``@Gtk.Template.Callback``. Under the stubbed ``gi`` (no real GTK) these must
behave as no-ops so the classes still import: the decorators return the
class/function unchanged and ``Child()`` yields a placeholder. Real GTK
(SSHPILOT_GUI_TESTS=1) uses the genuine Template machinery instead.

Both ``conftest.py`` and the handful of tests that build their own gi stub call
``install_template_stub`` so there is a single definition to maintain.
"""


class _TemplateStub:
    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, cls):
        return cls

    class Child:
        def __init__(self, *args, **kwargs):
            pass

    @staticmethod
    def Callback(*args, **kwargs):
        if len(args) == 1 and callable(args[0]) and not kwargs:
            return args[0]  # bare @Gtk.Template.Callback

        def _decorator(func):
            return func

        return _decorator  # @Gtk.Template.Callback(...)


def install_template_stub(gtk_module):
    """Attach the no-op ``Template`` stub to a stubbed ``Gtk`` module."""
    gtk_module.Template = _TemplateStub
    return _TemplateStub
