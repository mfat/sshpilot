"""``@Gtk.Template(...)`` must decorate a widget class, never a function.

A module-level helper inserted between the decorator and its class silently
retargets the decorator at the helper. Nothing in the headless suite notices:
tests/conftest.py stubs ``gi`` with a no-op ``Gtk.Template``, so the module
still imports and every test stays green — but the real application dies at
startup with "Can only use @Gtk.Template on Widgets", which is how this was
found (window.py, `_accelerator_label` promoted above MainWindow).

A source scan catches it without needing real GTK or a display.
``Gtk.Template.Callback()`` is excluded: decorating methods is its whole job.
"""

import ast
import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parent.parent / 'src' / 'sshpilot'


def _template_decorators(node):
    """Yield unparsed ``@Gtk.Template(...)`` decorators, excluding Callback."""
    for decorator in getattr(node, 'decorator_list', ()):
        text = ast.unparse(decorator)
        if 'Gtk.Template' not in text:
            continue
        if 'Gtk.Template.Callback' in text:
            continue
        yield text


def _python_sources():
    return sorted(p for p in SRC.rglob('*.py') if 'vendor' not in p.parts)


def test_sources_parse():
    for path in _python_sources():
        try:
            ast.parse(path.read_text(encoding='utf-8'))
        except SyntaxError as exc:  # pragma: no cover - failure output
            pytest.fail(f'{path.relative_to(SRC.parent.parent)}: {exc}')


def test_gtk_template_never_decorates_a_function():
    offenders = []
    for path in _python_sources():
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for text in _template_decorators(node):
                offenders.append(
                    f'{path.relative_to(SRC.parent.parent)}:{node.lineno} '
                    f'@{text} decorates function {node.name}()'
                )
    assert not offenders, (
        'Gtk.Template must decorate the widget class. A definition slipped '
        'between the decorator and its class:\n  ' + '\n  '.join(offenders)
    )


def test_window_template_still_targets_main_window():
    """The specific pairing the app's startup depends on."""
    tree = ast.parse((SRC / 'window.py').read_text(encoding='utf-8'))
    decorated = {
        node.name: list(_template_decorators(node))
        for node in tree.body
        if isinstance(node, ast.ClassDef)
    }
    assert any(
        'window.ui' in text for text in decorated.get('MainWindow', [])
    ), 'MainWindow lost its @Gtk.Template(window.ui) decorator'
