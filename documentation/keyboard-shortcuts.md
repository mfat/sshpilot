# SSH Pilot — Keyboard Shortcuts

SSH Pilot is designed for efficient keyboard navigation. Every shortcut — including split view — can be customized in **Preferences → Shortcuts** or by pressing **Ctrl+?** (**Cmd+?** on macOS).

To avoid clashing with command-line tools (readline, vim, …) and keyboard layouts, several shortcuts are **disabled by default** on Linux/Windows. They are still listed in **Preferences → Shortcuts**, where you can assign your own keys.

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
| New Connection | Ctrl+Shift+N | Cmd+N | Opens the connection editor |
| Open New Connection Tab | *Disabled by default* | Cmd+Alt+N | Opens a new tab for the selected connection |
| Open or Focus Selected Connection | Enter | Enter | Switches to an existing tab, or opens one |
| Force Open in New Tab | Ctrl+Enter | Cmd+Enter | Always opens a new tab, even if one exists |
| Search Connections | Ctrl+F | Cmd+F | Focuses the sidebar search bar |
| Focus Connection List | *Disabled by default* | Cmd+Shift+L | Moves focus to the sidebar from anywhere |
| Copy Key to Server | Ctrl+Shift+K | Cmd+Shift+K | Opens the SSH key copy tool |
| SSH Config Editor | *Disabled by default* | Cmd+Shift+E | Opens the SSH configuration file in a text editor |
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
| New Split View Tab | *Disabled by default* | Cmd+Shift+S |
| Command Blocks Sidebar | *Disabled by default* | *Disabled by default* |

---

## Split View

These actions apply while a split view tab is in focus. They are **disabled by
default** — assign your own keys in **Preferences → Shortcuts** (the *Split View*
group). "Close focused pane" uses the regular **Close Tab** shortcut.

| Action | Default |
|--------|---------|
| Focus pane left / down / up / right | *Disabled by default* |
| Resize pane left / down / up / right | *Disabled by default* |
| Side-by-side layout | *Disabled by default* |
| Top / bottom layout | *Disabled by default* |
| Add pane | *Disabled by default* |

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
