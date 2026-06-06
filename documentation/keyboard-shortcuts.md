# SSH Pilot — Keyboard Shortcuts

SSH Pilot is designed for efficient keyboard navigation. Most shortcuts can be customized in **Preferences → Shortcuts** or by pressing **Ctrl+?** (**Cmd+?** on macOS). Split view shortcuts are hardcoded and cannot be changed.

## Platform Notes

- **Linux** — the primary modifier is **Ctrl**
- **macOS** — the primary modifier is **Cmd** (Command key, shown as ⌘)
- **All platforms** — the **Alt** key works the same way on both

---

## General Application

| Action | Linux | macOS |
|--------|-------|-------|
| Quit | Ctrl+Shift+Q | Cmd+Shift+Q |
| Settings | Ctrl+, | Cmd+, |
| Keyboard Shortcuts | Ctrl+? | Cmd+? |
| Documentation | F1 | F1 |

---

## Connection Management

| Action | Linux | macOS | Notes |
|--------|-------|-------|-------|
| New Connection | Ctrl+N | Cmd+N | Opens the connection editor |
| Open New Connection Tab | Ctrl+Alt+N | Cmd+Alt+N | Opens a new tab for the selected connection |
| Open or Focus Selected Connection | Enter | Enter | Switches to an existing tab, or opens one |
| Force Open in New Tab | Ctrl+Enter | Cmd+Enter | Always opens a new tab, even if one exists |
| Search Connections | Ctrl+F | Cmd+F | Focuses the sidebar search bar |
| Focus Connection List | Ctrl+Shift+L | Cmd+Shift+L | Moves focus to the sidebar from anywhere |
| Copy Key to Server | Ctrl+Shift+K | Cmd+Shift+K | Opens the SSH key copy tool |
| SSH Config Editor | Ctrl+Shift+E | Cmd+Shift+E | Opens the SSH configuration file in a text editor |
| Manage Files | Ctrl+Shift+O | Cmd+Shift+O | Opens the file manager for the selected connection |
| Delete Selected Connection(s) | Delete or Backspace | Delete | Prompts for confirmation |
| Toggle Sidebar | F9 | F9 | Also accessible from the header bar button |

---

## Terminal

| Action | Linux | macOS |
|--------|-------|-------|
| Search in Terminal | Ctrl+Shift+F | Cmd+Shift+F |
| Find Next Match | Ctrl+G or Enter | Cmd+G or Enter |
| Find Previous Match | Ctrl+Shift+G or Shift+Enter | Cmd+Shift+G or Shift+Enter |
| Dismiss Search | Escape | Escape |
| Copy | Ctrl+Shift+C | Cmd+C |
| Paste | Ctrl+Shift+V | Cmd+V |
| Select All | Ctrl+Shift+A | Cmd+A |
| Zoom In | Ctrl+= | Cmd+= |
| Zoom Out | Ctrl+- | Cmd+- |
| Reset Zoom | Ctrl+0 | Cmd+0 |
| Local Terminal | Ctrl+Shift+T | Cmd+Shift+T |
| Broadcast Command | Ctrl+Shift+B | Cmd+Shift+B |

---

## Tab Management

| Action | Linux | macOS |
|--------|-------|-------|
| Next Tab | Ctrl+Page Down | Ctrl+Page Down |
| Previous Tab | Ctrl+Page Up | Ctrl+Page Up |
| Move Tab Left | Ctrl+Shift+Page Up | Ctrl+Shift+Page Up |
| Move Tab Right | Ctrl+Shift+Page Down | Ctrl+Shift+Page Down |
| Close Tab | Ctrl+Shift+W | Cmd+Shift+W |
| Tab Overview | Ctrl+Shift+Tab | Cmd+Shift+Tab |
| New Split View Tab | Ctrl+Shift+S | Cmd+Shift+S |
| Command Blocks Sidebar | Ctrl+Alt+S | Ctrl+Alt+S |

---

## Split View (not customizable)

These shortcuts are active whenever a split view tab is in focus.

| Action | Shortcut |
|--------|----------|
| Focus pane left | Ctrl+Alt+H |
| Focus pane down | Ctrl+Alt+J |
| Focus pane up | Ctrl+Alt+K |
| Focus pane right | Ctrl+Alt+L |
| Resize pane left | Ctrl+Alt+Shift+H |
| Resize pane down | Ctrl+Alt+Shift+J |
| Resize pane up | Ctrl+Alt+Shift+K |
| Resize pane right | Ctrl+Alt+Shift+L |
| Side-by-side layout | Ctrl+Shift+\ |
| Top / bottom layout | Ctrl+Shift+- |
| Add pane | Ctrl+Shift+N |
| Close focused pane | Ctrl+Shift+W |
| Focus pane by number (1–4) | Alt+1 through Alt+4 |

---

## File Manager

| Action | Linux | macOS |
|--------|-------|-------|
| Focus path entry | Ctrl+L | Cmd+L |
| Refresh directory | F5 or Ctrl+R | F5 or Cmd+R |
| Copy | Ctrl+C | Cmd+C |
| Cut | Ctrl+X | Cmd+X |
| Paste | Ctrl+V | Cmd+V |
| Paste as move | Ctrl+Shift+V | Cmd+Shift+V |
| Delete | Delete or Shift+Delete | Delete or Shift+Delete |

---

## Connection Editor Dialog

| Action | Linux | macOS |
|--------|-------|-------|
| Save | Ctrl+S | Cmd+S |
| Cancel / Close | Escape | Escape |

---

## Connection List Navigation

When the sidebar has focus, you can navigate without the mouse:

- **Up / Down arrow** — move between connections and groups
- **Enter** — connect to the selected server (or focus its existing tab)
- **Ctrl+Enter** — force open a new tab for the selected connection
- **Ctrl+click** — select multiple connections for batch operations
- **Delete** or **Backspace** — delete selected connection(s)

---

## Customizing Shortcuts

Open **Preferences → Shortcuts** or press **Ctrl+?** (**Cmd+?** on macOS). Click the shortcut next to any action and press the new key combination. SSH Pilot detects conflicts and warns you before saving.

To reset a single shortcut to its default, click the reset button on that row.

Shortcuts are saved per user and persist across application restarts.
