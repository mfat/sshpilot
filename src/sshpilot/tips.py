"""Usage-tip loading for the window banner.

Tips live in ``resources/tips.md`` (and optional ``tips.<lang>.md`` translations)
as plain data so they can be edited without touching application code or running
gettext extraction. This module stays GTK/GI-free so tip parsing is unit-testable
from a headless environment.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Iterable, List, Optional, Sequence

# Leading ``[tag]`` gates a tip to a capability that may be unavailable
# (Flatpak, macOS without a third-party terminal, missing file-manager support).
TIP_TAG_FILE_MANAGER = "file-manager"
TIP_TAG_EXTERNAL_TERMINAL = "external-terminal"

_TIP_TAG_RE = re.compile(r"^\[([a-z0-9-]+)\]\s*(.*)$")
_PRIMARY_PLACEHOLDER = "{primary}"


def tips_primary_modifier(lang_code: Optional[str] = None) -> str:
    """Return the primary-modifier label for tip text.

    macOS always uses the ⌘ symbol. Elsewhere English/French tips use ``Ctrl``
    and German tips use ``Strg``.
    """
    if sys.platform == "darwin":
        return "\u2318"
    if lang_code and (lang_code == "de" or lang_code.startswith("de_")):
        return "Strg"
    return "Ctrl"


def parse_tip_lines(
    lines: Iterable[str],
    *,
    primary_modifier: str,
    include_file_manager: bool = True,
    include_external_terminal: bool = True,
) -> List[str]:
    """Parse tip markdown lines into display strings.

    Blank lines and ``#`` comments are skipped. A leading ``[tag]`` is stripped
    and used to drop tips the current platform cannot offer. ``{primary}`` is
    replaced with *primary_modifier*.
    """
    allowed = {
        TIP_TAG_FILE_MANAGER: include_file_manager,
        TIP_TAG_EXTERNAL_TERMINAL: include_external_terminal,
    }
    tips: List[str] = []
    for raw in lines:
        text = raw.strip()
        if not text or text.startswith("#"):
            continue
        tag = None
        match = _TIP_TAG_RE.match(text)
        if match:
            tag, text = match.group(1), match.group(2).strip()
            if not text:
                continue
            if tag in allowed and not allowed[tag]:
                continue
            # Unknown tags are kept (stripped) so a future tag does not hide
            # the tip on older code paths that ignore the gate.
        tips.append(text.replace(_PRIMARY_PLACEHOLDER, primary_modifier))
    return tips


def load_window_tips(
    resources_dir: str,
    language_codes: Sequence[str],
    *,
    include_file_manager: bool = True,
    include_external_terminal: bool = True,
) -> List[str]:
    """Load tips from the first readable ``tips.<lang>.md`` or ``tips.md``.

    Returns an empty list when no tip file can be read.
    """
    candidates: List[tuple[Optional[str], str]] = [
        (code, os.path.join(resources_dir, f"tips.{code}.md"))
        for code in language_codes
    ]
    candidates.append((None, os.path.join(resources_dir, "tips.md")))

    for lang_code, path in candidates:
        try:
            with open(path, encoding="utf-8") as fh:
                raw_lines = fh.readlines()
        except OSError:
            continue
        # Prefer the file's own language code (tips.de.md → Strg) even when the
        # UI language was a regional variant like de_DE.
        return parse_tip_lines(
            raw_lines,
            primary_modifier=tips_primary_modifier(lang_code),
            include_file_manager=include_file_manager,
            include_external_terminal=include_external_terminal,
        )
    return []
