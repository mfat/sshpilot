"""Nautilus permission model used by the file manager properties dialog."""

from sshpilot.file_manager import permissions as perms
from sshpilot.file_manager.permissions import EXEC, INCONSISTENT, READ, WRITE, PermissionClass

OWNER, GROUP, OTHERS = PermissionClass.OWNER, PermissionClass.GROUP, PermissionClass.OTHERS


def test_labels_match_nautilus_for_files_and_folders():
    assert perms.permission_label(READ | WRITE, False) == "Read and Write"
    assert perms.permission_label(READ, False) == "Read-Only"
    assert perms.permission_label(0, False) == "None"
    assert perms.permission_label(READ | WRITE | EXEC, True) == "Create and Delete Files"
    assert perms.permission_label(READ | EXEC, True) == "Access Files"
    assert perms.permission_label(READ, True) == "List Files Only"
    assert perms.permission_label(READ | WRITE, True) == "Read and Write, No Access"
    assert perms.permission_label(INCONSISTENT, True) == "---"


def test_owner_is_never_offered_no_access():
    assert perms.permission_choices(OWNER, False) == [READ, READ | WRITE]
    assert perms.permission_choices(GROUP, False) == [0, READ, READ | WRITE]
    assert perms.permission_choices(OTHERS, True) == [0, READ, READ | EXEC, READ | EXEC | WRITE]


def test_summary_reads_each_class_and_leaves_exec_out_of_file_access():
    summary = perms.summarize([(False, 0o100754)])
    assert summary.file == {OWNER: READ | WRITE, GROUP: READ, OTHERS: READ}
    assert summary.file_exec == INCONSISTENT
    assert summary.has_files and not summary.has_folders


def test_summary_marks_disagreeing_items_inconsistent():
    summary = perms.summarize([(True, 0o40755), (True, 0o40700), (False, 0o100777)])
    assert summary.folder[OWNER] == READ | WRITE | EXEC
    assert summary.folder[GROUP] == INCONSISTENT
    assert summary.file[OTHERS] == READ | WRITE
    assert summary.file_exec == EXEC


def test_file_access_change_keeps_exec_and_special_bits():
    mask = perms.class_mask(GROUP, describes_folder=False)
    bits = perms.permission_to_mode(GROUP, READ | WRITE)
    assert perms.apply_bits(0o104751, bits, mask) == 0o4771


def test_folder_access_change_includes_exec():
    mask = perms.class_mask(OTHERS, describes_folder=True)
    assert perms.apply_bits(0o40755, perms.permission_to_mode(OTHERS, 0), mask) == 0o750


def test_executable_switch_sets_all_three_exec_bits():
    assert perms.set_executable(0o100644, True) == 0o755
    assert perms.set_executable(0o100755, False) == 0o644
