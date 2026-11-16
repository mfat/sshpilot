"""Legacy character encodings the terminal backends can offer besides UTF-8.

Shared by Preferences (the encoding list) and the backends that convert
(PyXterm.js through luit, gtkghostty in the widget). Pure Python, no ``gi``.
"""

from __future__ import annotations

from typing import Optional

#: (code, description) in the order Preferences lists them.
LEGACY_ENCODINGS = (
    ('ISO-8859-1', 'Latin-1 (ISO-8859-1)'),
    ('ISO-8859-15', 'Latin-9 (ISO-8859-15)'),
    ('Windows-1252', 'Western European (Windows-1252)'),
    ('GB2312', 'Simplified Chinese (GB2312)'),
    ('GBK', 'Chinese (GBK)'),
    ('GB18030', 'Chinese (GB18030)'),
    ('Big5', 'Traditional Chinese (Big5)'),
    ('Shift_JIS', 'Japanese (Shift_JIS)'),
    ('EUC-JP', 'Japanese (EUC-JP)'),
    ('EUC-KR', 'Korean (EUC-KR)'),
    ('KOI8-R', 'Cyrillic (KOI8-R)'),
    ('KOI8-U', 'Ukrainian (KOI8-U)'),
)

#: Encodings whose multi-byte characters can contain ASCII bytes (a trail
#: byte such as "@", "F" or "\"). Anything that edits the raw byte stream by
#: ASCII patterns, like output highlighting, could split a character.
ASCII_INSIDE_CHARACTERS = frozenset({'gbk', 'gb18030', 'big5', 'shift_jis'})


def is_utf8(encoding: Optional[str]) -> bool:
    return not encoding or encoding.strip().lower().replace('_', '-') in ('utf-8', 'utf8')


def python_codec(encoding: str) -> str:
    """The Python codec matching what the widget converts with.

    Shift_JIS is Windows' CP932 there (iconv's SHIFT_JIS turns the backslash
    into a yen sign), so it is here too.
    """
    lowered = encoding.strip().lower()
    if lowered in ('shift_jis', 'shift-jis', 'sjis'):
        return 'cp932'
    return lowered
