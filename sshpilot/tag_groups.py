"""Derive virtual tag groups from per-connection tags.

Kept GTK-free (no ``gi`` imports) so the grouping logic is unit-testable under
the test suite's stubbed ``gi`` environment. Tag groups are synthesized at
sidebar render time and are never stored in GroupManager — the tags in
``connections_meta`` remain the single source of truth.
"""

from typing import Dict, List, Mapping, Optional, Sequence, Tuple

TAG_GROUP_ID_PREFIX = "tag::"


def complete_tag_text(text: str, cursor: int, known_tags: Sequence[str]) -> Optional[Tuple[str, int]]:
    """Inline completion for a comma-separated value entry.

    Completes the segment being typed at the end of *text* with the first
    candidate (case-insensitive prefix match) not already present. The typed
    prefix is replaced by the candidate's canonical casing — values may be
    ssh Host aliases, whose config matching is case-sensitive, so the
    candidate's casing must win (typing "us" against "USA" yields "USA",
    never "usA"). Returns (completed_text, selection_start) — the caller
    selects from selection_start to the end so further typing replaces the
    suggestion — or None when nothing applies.
    """
    text = str(text)
    if cursor != len(text):
        return None  # only complete while typing at the end
    head, _sep, segment = text.rpartition(',')
    typed = segment.lstrip()
    if not typed:
        return None
    typed_key = typed.casefold()
    existing = {t.strip().casefold() for t in head.split(',') if t.strip()}
    for tag in known_tags:
        tag = str(tag)
        key = tag.casefold()
        # Slice-compare so the prefix alignment is exact at len(typed) chars.
        if (tag[:len(typed)].casefold() == typed_key
                and key != typed_key and key not in existing):
            return text[:len(text) - len(typed)] + tag, len(text)
    return None


def tag_group_id(tag: str) -> str:
    """Synthetic, stable id for a tag group (never a real GroupManager id)."""
    return TAG_GROUP_ID_PREFIX + str(tag).casefold()


def is_tag_group_id(group_id) -> bool:
    return isinstance(group_id, str) and group_id.startswith(TAG_GROUP_ID_PREFIX)


def compute_tag_groups(tag_map: Mapping[str, Sequence[str]]) -> List[Tuple[str, List[str]]]:
    """Group connections by tag.

    tag_map: nickname -> list of tags.
    Returns [(display_tag, [nicknames])] sorted case-insensitively by tag.
    Tags differing only by case merge; first-seen casing wins for display.
    Member nicknames are sorted case-insensitively and de-duplicated.
    """
    merged: Dict[str, Tuple[str, List[str]]] = {}  # casefold -> (display, members)
    for nickname, tags in tag_map.items():
        for raw in (tags or []):
            tag = str(raw).strip()
            if not tag:
                continue
            display, members = merged.setdefault(tag.casefold(), (tag, []))
            if nickname not in members:
                members.append(nickname)
    result = []
    for key in sorted(merged):
        display, members = merged[key]
        result.append((display, sorted(members, key=str.casefold)))
    return result


def add_tag_to_list(tags: Sequence[str], new_tag: str) -> Tuple[List[str], bool]:
    """Append *new_tag* unless already present (case-insensitive).

    Returns (new_list, changed).
    """
    new_tag = str(new_tag).strip()
    result = [str(t).strip() for t in (tags or []) if str(t).strip()]
    if not new_tag:
        return result, False
    if any(t.casefold() == new_tag.casefold() for t in result):
        return result, False
    result.append(new_tag)
    return result, True


def rename_tag_in_list(tags: Sequence[str], old_key: str, new_name: str) -> Tuple[List[str], bool]:
    """Replace tags matching *old_key* (casefold) with *new_name*.

    De-duplicates case-insensitively, keeping the first occurrence's position.
    Returns (new_list, changed).
    """
    old_key = str(old_key).casefold()
    result: List[str] = []
    seen = set()
    changed = False
    for raw in (tags or []):
        tag = str(raw).strip()
        if not tag:
            continue
        if tag.casefold() == old_key:
            if tag != new_name:
                changed = True
            tag = new_name
        key = tag.casefold()
        if key in seen:
            changed = True  # merge collapsed a duplicate
            continue
        seen.add(key)
        result.append(tag)
    return result, changed


def migrate_expanded_state(state: Mapping[str, bool], old_key: str, new_key: str) -> Dict[str, bool]:
    """Move the expansion flag from *old_key* to *new_key* on tag rename.

    When *new_key* already exists (merge), its value wins. Returns a new dict.
    """
    result = dict(state or {})
    if old_key in result:
        value = result.pop(old_key)
        result.setdefault(new_key, value)
    return result


def make_tag_group_info(display_tag: str, nicknames: Sequence[str], expanded: bool) -> dict:
    """Synthetic group_info dict consumable by GroupRow/TagGroupRow.

    Shaped like a GroupManager group but flagged with ``is_tag`` and never
    persisted.
    """
    return {
        "id": tag_group_id(display_tag),
        "name": display_tag,
        "tag_key": str(display_tag).casefold(),
        "parent_id": None,
        "children": [],
        "connections": list(nicknames),
        "expanded": bool(expanded),
        "color": None,
        "is_tag": True,
    }
