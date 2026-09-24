"""Pure helpers for multi-file transfer progress aggregation.

The daemon emits per-transfer byte counts. The file manager often starts several
transfers in one batch (up to the daemon concurrency limit). These helpers fold
per-key slices into one honest batch total so the progress dialog never treats
the latest file as the whole job.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Tuple


@dataclass(frozen=True)
class BatchByteProgress:
    """Aggregated byte progress for one UI transfer batch."""

    bytes_done: int
    bytes_total: Optional[int]
    fraction: Optional[float]


def aggregate_batch_bytes(
    *,
    expected: Mapping[str, Optional[int]],
    active: Mapping[str, Tuple[int, int]],
    settled: Mapping[str, Tuple[int, int]],
    files_completed: int = 0,
    total_files: int = 0,
) -> BatchByteProgress:
    """Fold per-transfer slices into batch done/total/fraction.

    ``expected`` lists every key in the batch (including queued and settled).
    ``active`` is the latest ``(done, reported_total)`` for in-flight keys
    (``reported_total`` of 0 means unknown). ``settled`` holds final
    ``(done, total)`` for finished keys.

    When every key has a known total, ``fraction`` is byte-based. Otherwise it
    falls back to file-count math using active per-file fractions where known,
    so concurrent transfers cannot make the bar jump backwards.
    """
    bytes_done = 0
    bytes_total = 0
    unknown = False

    for key, exp in expected.items():
        exp_n = int(exp) if exp is not None and exp > 0 else None
        if key in settled:
            done, total = settled[key]
            done_n = max(0, int(done or 0))
            total_n = max(0, int(total or 0))
            bytes_done += done_n
            bytes_total += total_n if total_n > 0 else done_n
            continue
        if key in active:
            done, total = active[key]
            done_n = max(0, int(done or 0))
            total_n = max(0, int(total or 0))
            bytes_done += done_n
            if total_n > 0:
                bytes_total += total_n
            elif exp_n is not None:
                bytes_total += max(exp_n, done_n)
            else:
                unknown = True
            continue
        # Queued / not yet reporting.
        if exp_n is not None:
            bytes_total += exp_n
        else:
            unknown = True

    if not unknown and bytes_total > 0:
        fraction = max(0.0, min(1.0, bytes_done / bytes_total))
        return BatchByteProgress(bytes_done, bytes_total, fraction)

    # Incomplete size knowledge: keep honest byte counts but use file-count
    # fraction so the bar stays monotonic across concurrent transfers.
    fraction: Optional[float] = None
    if total_files > 0:
        partial = 0.0
        for done, total in active.values():
            total_n = max(0, int(total or 0))
            done_n = max(0, int(done or 0))
            if total_n > 0:
                partial += min(1.0, done_n / total_n)
        overall = (max(0, int(files_completed)) + partial) / total_files
        if files_completed < total_files:
            overall = min(overall, (total_files - 1) / total_files)
        fraction = max(0.0, min(1.0, overall))

    return BatchByteProgress(
        bytes_done,
        None if unknown else (bytes_total if bytes_total > 0 else None),
        fraction,
    )


def progress_key_for_future(future: object) -> str:
    """Stable per-future key used to attribute ``progress-bytes`` emissions."""
    return f"fut-{id(future)}"
