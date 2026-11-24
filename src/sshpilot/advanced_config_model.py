"""The draft behind Advanced Configuration, and every edit to it (GTK-free).

:class:`DraftEditor` owns the configuration being edited: the blocks, the
text they serialize to, where each block and option sits in that text, and
the undo history. The window's widgets never change the draft themselves;
they call an edit here and then show the result, so every rule about what
an edit may do lives in one place and is tested without a display.
"""
from __future__ import annotations

from enum import Enum
from typing import List, Optional, Sequence

from .ssh_config_blocks import (
    GLOBAL,
    HOST,
    Block,
    ConfigDraft,
    LineMap,
    Option,
    parse,
    serialize,
)

HISTORY_LIMIT = 100


class Refusal(str, Enum):
    """Why an edit was not made."""

    #: The order would put global options under a block that is on.
    GLOBAL_ORDER = "global_order"


def can_place(blocks: Sequence[Block]) -> bool:
    """Whether *blocks* is an order that means what it shows.

    OpenSSH has no line that ends a block: options written after a ``Host``
    line belong to that host. The global options therefore come before
    every block that is switched on. And nothing at all comes before global
    options that are on: a switched-off block is comments, and an active
    option under those comments reads back as part of whatever is above.
    """
    seen_any = seen_active = False
    for block in blocks:
        if block.kind == GLOBAL:
            if seen_active or (seen_any and not block.disabled):
                return False
        elif not block.disabled:
            seen_active = True
        seen_any = True
    return True


def first_host_name(block: Optional[Block]) -> str:
    """The host a block is about: the first name on its ``Host`` line that
    is a plain name, not a pattern. Empty for ``Match``, global, switched-off
    and pattern-only blocks, which name no single host."""
    if block is None or block.kind != HOST or block.disabled:
        return ""
    for token in block.patterns.split():
        if token and not any(char in token for char in '*?!"'):
            return token
    return ""


class DraftEditor:
    """A configuration draft, its text, and its undoable edits.

    Edits return what they changed (a block, an option, ``True``), ``None``
    or ``False`` when there was nothing to do, or a :class:`Refusal`. An
    edit that changes the draft's *shape* (blocks or options added, removed
    or moved) records an undo step at once; typing into a field is recorded
    by :meth:`commit_typing`, which the window calls when typing pauses, so
    one undo steps over a whole edit and not a keystroke.
    """

    def __init__(self) -> None:
        self.draft = ConfigDraft()
        self.loaded_text = ""
        self.text = ""
        self.lines = LineMap({}, {})
        #: Grows with every change of the text: tells answers about an older
        #: draft from answers about this one.
        self.serial = 0
        self._history: List[ConfigDraft] = []
        self._index = -1

    # -- text ---------------------------------------------------------------

    def _refresh(self) -> None:
        text, lines = serialize(self.draft)
        if text != self.text or not self.serial:
            self.serial += 1
        self.text, self.lines = text, lines

    def load(self, text: str) -> None:
        """Start over from saved text."""
        self.loaded_text = text
        self.draft = parse(text)
        self._history = []
        self._index = -1
        self._refresh()
        self._push()

    def replace_text(self, text: str) -> None:
        """Read edited text into the blocks (the Text page's Apply)."""
        self.draft = parse(text)
        self._structural()

    def mark_saved(self, text: str) -> None:
        self.loaded_text = text

    @property
    def dirty(self) -> bool:
        return self.text != self.loaded_text

    # -- history ------------------------------------------------------------

    def _push(self) -> bool:
        if self._index >= 0 and serialize(self._history[self._index])[0] == self.text:
            return False
        del self._history[self._index + 1:]
        self._history.append(self.draft.clone())
        del self._history[:-HISTORY_LIMIT]
        self._index = len(self._history) - 1
        return True

    def _structural(self) -> None:
        self._refresh()
        self._push()

    def typed(self) -> None:
        """A field's text changed: the draft's text follows; history waits."""
        self._refresh()

    def commit_typing(self) -> bool:
        """Record typing as one undo step. True when there was any."""
        return self._push()

    @property
    def can_undo(self) -> bool:
        return self._index > 0 or (
            self._index == 0 and serialize(self._history[0])[0] != self.text
        )

    @property
    def can_redo(self) -> bool:
        return self._index < len(self._history) - 1

    def _restore(self, index: int) -> None:
        self._index = index
        self.draft = self._history[index].clone()
        self._refresh()

    def undo(self) -> bool:
        self._push()
        if self._index <= 0:
            return False
        self._restore(self._index - 1)
        return True

    def redo(self) -> bool:
        if not self.can_redo:
            return False
        self._restore(self._index + 1)
        return True

    # -- blocks -------------------------------------------------------------

    @property
    def blocks(self) -> List[Block]:
        return self.draft.blocks

    def find_block(self, uid: Optional[int]) -> Optional[Block]:
        return self.draft.find_block(uid) if uid is not None else None

    def find_option(self, uid: Optional[int]):
        return self.draft.find_option(uid) if uid is not None else (None, None)

    @property
    def has_global(self) -> bool:
        return any(block.kind == GLOBAL for block in self.draft.blocks)

    def add_block(self, kind: str) -> Optional[Block]:
        """A new block with one empty option: global options go first, a
        Host or Match block last. None when a global block exists already."""
        if kind == GLOBAL:
            if self.has_global:
                return None
            block = Block(kind=GLOBAL, options=[Option()])
            self.draft.blocks.insert(0, block)
        else:
            block = Block(kind=kind, patterns="", options=[Option()])
            self.draft.blocks.append(block)
        self._structural()
        return block

    def _reorder(self, blocks: List[Block]):
        if blocks == self.draft.blocks:
            return False
        if not can_place(blocks):
            return Refusal.GLOBAL_ORDER
        self.draft.blocks = blocks
        self._structural()
        return True

    def move_block(self, uid: int, index: int):
        """Put a block at *index*. True, False (no change) or a Refusal."""
        block = self.find_block(uid)
        if block is None:
            return False
        blocks = [b for b in self.draft.blocks if b is not block]
        blocks.insert(max(0, min(index, len(blocks))), block)
        return self._reorder(blocks)

    def move_block_by(self, uid: int, delta: int):
        block = self.find_block(uid)
        if block is None or block.kind == GLOBAL:
            return False
        index = self.draft.blocks.index(block) + delta
        if not 0 <= index < len(self.draft.blocks):
            return False
        return self.move_block(uid, index)

    def toggle_block(self, uid: int):
        """Switch a block on or off. True, False or a Refusal."""
        block = self.find_block(uid)
        if block is None:
            return False
        block.disabled = not block.disabled
        if not can_place(self.draft.blocks):
            block.disabled = not block.disabled
            return Refusal.GLOBAL_ORDER
        self._structural()
        return True

    def duplicate_block(self, uid: int) -> Optional[Block]:
        block = self.find_block(uid)
        if block is None or block.kind == GLOBAL:
            return None
        copy = Block(
            kind=block.kind, patterns=block.patterns, disabled=block.disabled,
            options=[
                Option(key=option.key, value=option.value, comment=option.comment)
                for option in block.options
            ],
        )
        self.draft.blocks.insert(self.draft.blocks.index(block) + 1, copy)
        self._structural()
        return copy

    def delete_block(self, uid: int) -> bool:
        block = self.find_block(uid)
        if block is None:
            return False
        self.draft.blocks.remove(block)
        self._structural()
        return True

    def set_patterns(self, uid: int, text: str) -> bool:
        block = self.find_block(uid)
        if block is None or block.patterns == text:
            return False
        block.patterns = text
        self.typed()
        return True

    def set_note(self, uid: int, text: str) -> bool:
        block = self.find_block(uid)
        if block is None or block.note == text:
            return False
        block.set_note(text, self.draft.newline)
        self.typed()
        return True

    # -- options ------------------------------------------------------------

    def add_option(self, block_uid: int, key: str = "", value: str = "") -> Optional[Option]:
        block = self.find_block(block_uid)
        if block is None:
            return None
        option = Option(key=key, value=value)
        block.options.append(option)
        self._structural()
        return option

    def delete_option(self, uid: int) -> bool:
        block, option = self.find_option(uid)
        if option is None:
            return False
        block.options.remove(option)
        self._structural()
        return True

    def move_option(self, uid: int, block_uid: int, index: Optional[int]) -> bool:
        """Put an option at *index* of a block (None: at its end)."""
        source, option = self.find_option(uid)
        target = self.find_block(block_uid)
        if option is None or target is None:
            return False
        before = [(b.uid, [o.uid for o in b.options]) for b in self.draft.blocks]
        position = source.options.index(option)
        source.options.remove(option)
        if index is None:
            index = len(target.options)
        elif source is target and position < index:
            index -= 1
        target.options.insert(max(0, min(index, len(target.options))), option)
        if before == [(b.uid, [o.uid for o in b.options]) for b in self.draft.blocks]:
            return False
        self._structural()
        return True

    def set_option(self, uid: int, field: str, text: str) -> bool:
        """Change an option's ``key`` or ``value``."""
        _block, option = self.find_option(uid)
        if option is None or field not in ("key", "value") or getattr(option, field) == text:
            return False
        setattr(option, field, text)
        self.typed()
        return True

    # -- what the rest of the window asks ------------------------------------

    def owner_of_line(self, line: int, lines: Optional[LineMap] = None) -> Optional[int]:
        """The uid of the option or block at *line* of the serialized text."""
        lines = lines or self.lines
        for uid, number in lines.options.items():
            if number == line:
                return uid
        for uid, number in lines.headers.items():
            if number and number == line:
                return uid
        return None
