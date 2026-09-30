"""Nautilus-style permission model for the properties dialog.

Mirrors ``nautilus-properties.c``: each of owner / group / others gets one
access level, expressed differently for folders ("List Files Only", "Access
Files", ...) and files ("Read-Only", "Read and Write"). The execute bit on
files is a separate "Executable as Program" switch, so file access levels never
carry it. Selections mixing files and folders get separate folder and file rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from gettext import gettext as _
from typing import Optional, Sequence

# Same order as a mode's rwx triplet, so a class's bits shift straight in.
READ = 0o4
WRITE = 0o2
EXEC = 0o1
INCONSISTENT = 0o10

_PERMISSION_BITS = READ | WRITE | EXEC
_ALL_EXEC = 0o111


class PermissionClass(IntEnum):
    """Who a permission applies to; the value is the mode bit shift."""

    OWNER = 6
    GROUP = 3
    OTHERS = 0


def permission_from_mode(who: PermissionClass, mode: int) -> int:
    return (mode >> int(who)) & _PERMISSION_BITS


def permission_to_mode(who: PermissionClass, value: int) -> int:
    return (value & _PERMISSION_BITS) << int(who)


def exec_from_mode(mode: int) -> int:
    """``EXEC`` when all three execute bits are set, ``INCONSISTENT`` when some are."""
    bits = mode & _ALL_EXEC
    if bits == _ALL_EXEC:
        return EXEC
    if bits:
        return INCONSISTENT
    return 0


def permission_label(value: int, describes_folder: bool) -> str:
    """Nautilus ``permission_value_to_string``."""
    if value & INCONSISTENT:
        return "---"
    if value & READ:
        if value & WRITE:
            if not describes_folder:
                return _("Read and Write")
            if value & EXEC:
                return _("Create and Delete Files")
            return _("Read and Write, No Access")
        if not describes_folder:
            return _("Read-Only")
        if value & EXEC:
            return _("Access Files")
        return _("List Files Only")
    if value & WRITE:
        if not describes_folder or value & EXEC:
            return _("Write-Only")
        return _("Write-Only, No Access")
    if describes_folder and value & EXEC:
        return _("Access-Only")
    # Translators: the access someone has to a file or folder.
    return _("None")


def permission_choices(who: PermissionClass, describes_folder: bool) -> list[int]:
    """The access levels offered in a combo, before any current odd value.

    The owner is never offered "None" (Nautilus ``create_permission_list_model``).
    """
    choices = [] if who is PermissionClass.OWNER else [0]
    if describes_folder:
        choices += [READ, READ | EXEC, READ | EXEC | WRITE]
    else:
        choices += [READ, READ | WRITE]
    return choices


def _merge(current: Optional[int], value: int) -> int:
    if current is None or current == value:
        return value
    return INCONSISTENT


@dataclass
class PermissionsSummary:
    """Common access per class across a selection (Nautilus ``PermissionsInfo``)."""

    folder: dict[PermissionClass, int] = field(default_factory=dict)
    file: dict[PermissionClass, int] = field(default_factory=dict)
    file_exec: int = 0
    has_folders: bool = False
    has_files: bool = False


def summarize(items: Sequence[tuple[bool, int]]) -> PermissionsSummary:
    """Summarize ``(is_dir, mode)`` pairs into one value per class."""
    summary = PermissionsSummary()
    file_exec: Optional[int] = None
    for is_dir, mode in items:
        for who in PermissionClass:
            if is_dir:
                value = permission_from_mode(who, mode)
                summary.folder[who] = _merge(summary.folder.get(who), value)
            else:
                value = permission_from_mode(who, mode) & (READ | WRITE)
                summary.file[who] = _merge(summary.file.get(who), value)
        if is_dir:
            summary.has_folders = True
        else:
            file_exec = _merge(file_exec, exec_from_mode(mode))
            summary.has_files = True
    summary.file_exec = file_exec or 0
    return summary


def class_mask(who: PermissionClass, describes_folder: bool) -> int:
    """Mode bits a combo row controls: files leave execute to the switch."""
    value = READ | WRITE | (EXEC if describes_folder else 0)
    return permission_to_mode(who, value)


def apply_bits(mode: int, new_bits: int, mask: int) -> int:
    """Replace the ``mask`` bits of ``mode`` (permission bits only) with ``new_bits``."""
    return ((mode & 0o7777) & ~mask) | (new_bits & mask)


def set_executable(mode: int, executable: bool) -> int:
    return apply_bits(mode, _ALL_EXEC if executable else 0, _ALL_EXEC)
