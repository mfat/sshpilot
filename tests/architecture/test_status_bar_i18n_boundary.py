"""Status bar and notification wording lives in the curation modules only.

The runtime modules route codes; the curation modules
(``gtk/status_bar_messages.py`` and ``gtk/notification_messages.py``) own every
sentence a code can produce. The model and the news fetcher translate nothing.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src" / "sshpilot"


def test_curation_and_view_modules_are_in_potfiles():
    potfiles = set((ROOT / "po" / "POTFILES").read_text().splitlines())
    for relative in (
        "src/sshpilot/gtk/status_bar_messages.py",
        "src/sshpilot/gtk/notification_messages.py",
        "src/sshpilot/status_bar.py",
        "src/sshpilot/notification_center.py",
    ):
        assert relative in potfiles, relative


def test_model_and_feed_translate_nothing():
    for relative in ("notification_store.py", "news_feed.py"):
        source = (SOURCE / relative).read_text()
        assert "gettext" not in source, relative
        assert not re.search(r"\b_\(", source), relative


def test_runtime_routes_codes_not_sentences():
    # The controller renders only through the catalogue (or plugin-supplied
    # text via show_text); it never builds a sentence of its own.
    source = (SOURCE / "status_bar.py").read_text()
    assert "format_status_message" in source
    assert "show_message(f" not in source
