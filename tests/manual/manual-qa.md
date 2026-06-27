# Manual QA Checklist

## Multi-selection connection management

### Delete multiple connections
1. Launch sshPilot and ensure the sidebar lists several connections.
2. Hold Ctrl (or Command on macOS) and click two connection rows to multi-select them.
3. Click the trash icon in the connection toolbar.
4. Confirm that a single confirmation dialog references removing the selected hosts, then approve the action.
5. Verify that both connections disappear from the sidebar.

### Move multiple connections into a group
1. With at least two connections selected (via Ctrl/Command-click), open the context menu on one of the highlighted rows.
2. Choose **Move to Group** and pick an existing group (or create a new one) in the dialog.
3. Confirm that all selected connections now appear under the chosen group.

### Ungroup multiple connections
1. Multi-select two grouped connections.
2. Open the context menu on one of the highlighted rows and choose **Ungroup**.
3. Confirm that both connections return to the Ungrouped section.

## Stability regression surfaces (real GTK only)

These exercise crash/lifecycle paths the headless test suite cannot reach
(the suite stubs `gi.repository`, so real GObject finalization, drag-and-drop,
and the document portal never run). Verify on a real GTK build before release.

### File-manager tab teardown — no shutdown segfault
1. Open several tabs including at least two file-manager tabs.
2. Right-click a tab and choose **Close Other Tabs** (also try the × button and
   **Close**). Confirm no crash and no `crash.log` is written.
3. Quit the application with file-manager tabs still open. Confirm a clean exit
   (check the log for the "survived shutdown teardown with a live controller"
   warning — it must NOT appear).

### Drag-and-drop reordering
1. Drag connection rows to reorder them within and across groups; drag groups to
   reorder. Confirm the drop lands where the indicator showed and nothing is
   duplicated or lost.
2. Drag a connection near the top/bottom edge of the list and confirm it
   autoscrolls smoothly.
3. Drag a connection row onto a split-view tab: dropping onto an empty pane fills
   it in place; dropping onto an occupied pane opens a new pane.

### Flatpak portal SCP / file paths
1. In a Flatpak build, open the SCP window and transfer to/from a folder granted
   via the document portal. Confirm friendly host paths are shown (not portal
   document IDs) and the transfer succeeds.
