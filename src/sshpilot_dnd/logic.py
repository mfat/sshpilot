"""Pure helper functions for sidebar drag-and-drop workflows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Literal, Tuple


Position = Literal["above", "below"]


@dataclass(frozen=True)
class RowBounds:
    """Geometry metadata for hit-testing listbox rows."""

    key: str
    top: float
    height: float


@dataclass(frozen=True)
class HitTestResult:
    """Result of translating a pointer Y coordinate to an insertion slot."""

    key: str
    position: Position


@dataclass(frozen=True)
class AutoscrollParams:
    """Parameters for computing autoscroll velocity."""

    viewport_height: float
    pointer_y: float
    margin: float
    max_velocity: float


@dataclass(frozen=True)
class ReorderPlan:
    """Immutable container for the reordered connection state."""

    connection_to_group: Dict[str, Optional[str]]
    group_connections: Dict[Optional[str], List[str]]
    changed: bool


def hit_test_insertion(rows: Sequence[RowBounds], pointer_y: float) -> Optional[HitTestResult]:
    """Return which row the insertion line should target for a given pointer Y.

    The rows are expected to be sorted by their top coordinate. If ``pointer_y``
    lies before all rows we return the first row with ``position='above'``. If it
    lies beyond the final row we return the last row with ``position='below'``.
    """

    if not rows:
        return None

    pointer = float(pointer_y)
    ordered = sorted(rows, key=lambda r: (r.top, r.height))

    first = ordered[0]
    if pointer <= first.top:
        return HitTestResult(first.key, "above")

    for row in ordered:
        height = max(0.0, float(row.height))
        bottom = row.top + height

        if height <= 0.0:
            # Treat zero-height rows as infinitesimal lines.
            if pointer <= row.top:
                return HitTestResult(row.key, "above")
            continue

        mid = row.top + (height / 2.0)

        if pointer < mid:
            return HitTestResult(row.key, "above")
        if pointer <= bottom:
            return HitTestResult(row.key, "below")

    last = ordered[-1]
    return HitTestResult(last.key, "below")


def autoscroll_velocity(params: AutoscrollParams) -> float:
    """Calculate the signed autoscroll velocity for a pointer.

    Negative velocities scroll upwards, positive values scroll downwards. The
    computation is linear within the configured margin and zero elsewhere.
    """

    height = float(params.viewport_height)
    if height <= 0.0:
        return 0.0

    pointer = max(0.0, min(float(params.pointer_y), height))
    margin = max(1.0, min(float(params.margin), height / 2.0))
    max_velocity = max(0.1, float(params.max_velocity))

    top_threshold = margin
    bottom_threshold = height - margin

    if pointer < top_threshold:
        distance = top_threshold - pointer
        return -_scale_velocity(distance, margin, max_velocity)

    if pointer > bottom_threshold:
        distance = pointer - bottom_threshold
        return _scale_velocity(distance, margin, max_velocity)

    return 0.0


def reorder_connections_state(
    connection_to_group: Dict[str, Optional[str]],
    group_connections: Dict[Optional[str], List[str]],
    target_connection: str,
    dragged_connections: Sequence[str],
    position: Position,
) -> ReorderPlan:
    """Return an updated mapping describing the reordered connection state.

    The input dictionaries are treated as immutable templates; the returned
    ``ReorderPlan`` always contains copies that may safely be mutated by the
    caller. Connections absent from ``dragged_connections`` remain untouched.
    """

    if position not in {"above", "below"}:
        raise ValueError(f"Unsupported position '{position}'")

    if target_connection not in connection_to_group:
        raise ValueError(f"Unknown target connection '{target_connection}'")

    base_connection_map = dict(connection_to_group)

    # Ensure None is present to represent the ungrouped bucket.
    base_group_lists: Dict[Optional[str], List[str]] = {
        key: list(value) for key, value in group_connections.items()
    }
    base_group_lists.setdefault(None, [])

    target_group = base_connection_map[target_connection]
    dragged = [
        nickname
        for nickname in dragged_connections
        if nickname in base_connection_map and nickname != target_connection
    ]

    if not dragged:
        return ReorderPlan(base_connection_map, base_group_lists, changed=False)

    new_connection_map = dict(base_connection_map)
    dragged_set = set(dragged)

    # Copy lists and remove dragged nicknames.
    new_group_lists: Dict[Optional[str], List[str]] = {}
    for group_id, items in base_group_lists.items():
        new_group_lists[group_id] = [item for item in items if item not in dragged_set]

    # Ensure any groups referenced by dragged connections exist in the mapping.
    for nickname in dragged:
        current_group = base_connection_map.get(nickname)
        new_group_lists.setdefault(current_group, [])

    dest_list = new_group_lists.setdefault(target_group, [])

    if target_connection not in dest_list:
        # The target may have been temporarily removed if the data was malformed.
        dest_list.append(target_connection)

    dest_list = [item for item in dest_list if item not in dragged_set]

    try:
        anchor_index = dest_list.index(target_connection)
    except ValueError as exc:  # pragma: no cover - defensive coding
        raise ValueError("Target connection not present in destination list") from exc

    insert_index = anchor_index if position == "above" else anchor_index + 1

    for offset, nickname in enumerate(dragged):
        dest_list.insert(insert_index + offset, nickname)
        new_connection_map[nickname] = target_group

    new_group_lists[target_group] = dest_list

    changed = _state_changed(
        base_connection_map,
        new_connection_map,
        base_group_lists,
        new_group_lists,
    )

    return ReorderPlan(new_connection_map, new_group_lists, changed=changed)


def _scale_velocity(distance: float, margin: float, max_velocity: float) -> float:
    ratio = min(1.0, max(0.0, distance) / margin)
    return max_velocity * ratio


def _state_changed(
    original_map: Dict[str, Optional[str]],
    updated_map: Dict[str, Optional[str]],
    original_lists: Dict[Optional[str], List[str]],
    updated_lists: Dict[Optional[str], List[str]],
) -> bool:
    if original_map != updated_map:
        return True

    keys = set(original_lists.keys()) | set(updated_lists.keys())
    for key in keys:
        if original_lists.get(key, []) != updated_lists.get(key, []):
            return True
    return False

