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

## Terminal resizing (Flatpak)

### Verify agent-driven resize propagation
1. Launch sshPilot from the Flatpak build and open a local shell (which uses the host agent).
2. Run a full-screen terminal application such as `less /etc/hosts` or `htop`.
3. Resize the sshPilot window horizontally and vertically.
4. Confirm that the running application immediately adjusts to the new terminal dimensions without drawing artifacts.
5. Repeat the resize in the opposite direction to ensure the agent continues to handle additional changes.
