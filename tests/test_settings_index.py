"""The settings index omni-search uses to offer individual settings.

Whether it matches the real Settings rows needs the window, so that check is
a GUI test (tests/test_gui_settings_search.py).
"""

from sshpilot.settings_index import NOT_INDEXED, PAGE_TITLES, SETTINGS, entry_for


def test_entries_are_unique_and_on_known_pages():
    keys = [(entry.page_id, entry.title) for entry in SETTINGS]
    assert len(keys) == len(set(keys))
    assert {entry.page_id for entry in SETTINGS} <= set(PAGE_TITLES)
    assert {page for page, _title in NOT_INDEXED} <= set(PAGE_TITLES)
    assert not set(keys) & set(NOT_INDEXED)


def test_keywords_include_the_english_terms():
    entry = entry_for('ssh-options', 'Enable SSH connection multiplexing')
    assert entry is not None
    assert 'controlmaster' in entry.keyword_list()
    assert entry_for('terminal', 'No such row') is None


def test_omni_settings_pages_are_real_pages():
    from sshpilot.omni_search import _SETTINGS_PAGES

    assert {page_id for page_id, _title, _keywords in _SETTINGS_PAGES} <= set(PAGE_TITLES)
