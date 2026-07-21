"""Static guards for the Preferences dialog Blueprint/UI template.

These do not need a display: they pin the OverlaySplitView title-button
wiring that #1085 regressed when Preferences became an Adw.Dialog.
"""

from pathlib import Path

import pytest

_UI_DIR = Path(__file__).resolve().parents[1] / 'src' / 'sshpilot' / 'resources' / 'ui'
_BLP = _UI_DIR / 'preferences_window.blp'
_UI = _UI_DIR / 'preferences_window.ui'


def _sidebar_header_block(text: str, *, blp: bool) -> str:
    """Return the sidebar HeaderBar declaration from a .blp or .ui file."""
    if blp:
        marker = 'Adw.HeaderBar sidebar_header_bar {'
        start = text.index(marker)
        # The matching closing brace of this HeaderBar block.
        depth = 0
        for i, ch in enumerate(text[start:], start):
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        raise AssertionError('unclosed sidebar_header_bar block in .blp')

    marker = 'id="sidebar_header_bar"'
    start = text.index(marker)
    # Walk back to the opening <object ...> and forward to </object>.
    obj_start = text.rindex('<object', 0, start)
    obj_end = text.index('</object>', start) + len('</object>')
    return text[obj_start:obj_end]


@pytest.mark.parametrize('path,blp', [(_BLP, True), (_UI, False)])
def test_sidebar_header_exposes_title_buttons_on_both_sides(path, blp):
    """Both edges must advertise title buttons so OverlaySplitView can show
    the platform's close button — start-side on macOS, end-side on GNOME.
    """
    text = path.read_text(encoding='utf-8')
    block = _sidebar_header_block(text, blp=blp)
    if blp:
        assert 'show-start-title-buttons: true;' in block
        assert 'show-end-title-buttons: true;' in block
        assert 'show-start-title-buttons: false;' not in block
        assert 'show-end-title-buttons: false;' not in block
    else:
        assert '<property name="show-start-title-buttons">true</property>' in block
        assert '<property name="show-end-title-buttons">true</property>' in block
        assert '<property name="show-start-title-buttons">false</property>' not in block
        assert '<property name="show-end-title-buttons">false</property>' not in block
