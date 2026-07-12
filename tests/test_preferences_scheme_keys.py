"""Guards the single source of truth for terminal color schemes.

Preferences derives scheme names AND preview colors from Config.terminal_themes;
SCHEME_KEYS only fixes picker order/selection. If someone adds a scheme to the
picker but not to config (or misspells a key), the model, the index lookup, and
the preview all silently break — this test fails first instead.
"""
from sshpilot.preferences import SCHEME_KEYS
from sshpilot.config import Config


def test_scheme_keys_exist_in_config_themes():
    themes = Config.load_builtin_themes(None)  # method ignores self
    for key in SCHEME_KEYS:
        assert key in themes, f"SCHEME_KEYS entry {key!r} missing from Config.terminal_themes"
        assert themes[key].get('name'), f"theme {key!r} has no display name"
        assert themes[key].get('background'), f"theme {key!r} has no background color"


def test_scheme_keys_have_no_duplicates():
    assert len(set(SCHEME_KEYS)) == len(SCHEME_KEYS)
