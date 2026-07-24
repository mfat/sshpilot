from types import SimpleNamespace

from sshpilot.shortcut_editor import ShortcutsPreferencesPage


def test_find_conflict_compares_accelerators_semantically(monkeypatch):
    parsed = {
        '<Primary>q': (True, 113, 4),
        '<Control>q': (True, 113, 4),
    }
    monkeypatch.setattr(
        'sshpilot.shortcut_editor.Gtk.accelerator_parse',
        lambda accelerator: parsed[accelerator],
        raising=False,
    )

    page = SimpleNamespace(
        _action_names=['quit', 'search'],
        _get_effective_shortcuts=lambda action: {
            'quit': ['<Control>q'],
            'search': ['<Primary>f'],
        }[action],
        _accelerators_equal=ShortcutsPreferencesPage._accelerators_equal,
    )

    conflict = ShortcutsPreferencesPage._find_conflict(
        page, 'search', '<Primary>q'
    )

    assert conflict == 'quit'


def test_replace_conflicting_shortcut_reassigns_accelerator(monkeypatch):
    parsed = {
        '<Primary>q': (True, 113, 4),
        '<Control>q': (True, 113, 4),
        '<Primary>w': (True, 119, 4),
    }
    monkeypatch.setattr(
        'sshpilot.shortcut_editor.Gtk.accelerator_parse',
        lambda accelerator: parsed[accelerator],
        raising=False,
    )
    effective = {
        'quit': ['<Control>q', '<Primary>w'],
        'search': ['<Primary>f'],
    }
    changes = []

    def set_override(action, accelerators):
        effective[action] = accelerators
        changes.append((action, accelerators))

    page = SimpleNamespace(
        _get_effective_shortcuts=lambda action: effective[action],
        _accelerators_equal=ShortcutsPreferencesPage._accelerators_equal,
        _attempt_set_override=set_override,
    )

    ShortcutsPreferencesPage._replace_conflicting_shortcut(
        page, 'search', '<Primary>q', 'quit'
    )

    assert changes == [
        ('quit', ['<Primary>w']),
        ('search', ['<Primary>q']),
    ]
