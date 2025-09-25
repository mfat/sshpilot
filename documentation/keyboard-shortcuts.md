# SSH Pilot - Keyboard Shortcuts

SSH Pilot is designed for efficient keyboard navigation. This guide covers all available keyboard shortcuts for seamless workflow management.

## Platform Notes

- **Linux/Windows**: Use `Ctrl` key (shown as `Ctrl` in shortcuts)
- **macOS**: Use `⌘` (Command) key (shown as `Cmd` in shortcuts)
- **All Platforms**: `Alt` key works the same across platforms

---

## 🚀 General Application

| Action | Linux/Windows | macOS | Description |
|--------|---------------|-------|-------------|
| **Quit Application** | `Ctrl+Shift+Q` | `Cmd+Shift+Q` | Exit SSH Pilot |
| **Preferences** | `Ctrl+,` | `Cmd+,` | Open application preferences |
| **Help Documentation** | `F1` | `F1` | Open help documentation |
| **Keyboard Shortcuts** | `Ctrl+Shift+/` | `Cmd+Shift+/` | Show this shortcuts window |

---

## 📂 Connection Management

| Action | Linux/Windows | macOS | Description |
|--------|---------------|-------|-------------|
| **New Connection** | `Ctrl+N` | `Cmd+N` | Create a new SSH connection |
| **Search Connections** | `Ctrl+F` | `Cmd+F` | Search/filter connection list |
| **Focus Connection List** | `Ctrl+L` | `Cmd+L` | Focus the sidebar connection list |
| **Quick Connect** | `Ctrl+Alt+C` | `Cmd+Alt+C` | Open the Quick Connect dialog |
| **Open New Tab** | `Ctrl+Alt+N` | `Cmd+Alt+N` | Open the highlighted connection in a new tab |
| **Open in New Tab** | `Ctrl+Enter` | `Cmd+Enter` | Force the selected connection to open in a new tab |
| **Open or Focus Selected Connection** | `Enter` | `Enter` | Focus an existing tab for the selected connection or open a new one |
| **Manage Remote Files** | `Ctrl+Shift+O` | `Cmd+Shift+O` | Launch the remote file manager for the selected connection* |
| **Delete Selected Connection(s)** | `Delete` or `Backspace` | `Delete` (⌫) | Remove the highlighted connections after confirmation |

---

## 🔑 SSH Key Management

| Action | Linux/Windows | macOS | Description |
|--------|---------------|-------|-------------|
| **Copy Key to Server** | `Ctrl+Shift+K` | `Cmd+Shift+K` | SSH key copy utility |
| **SSH Config Editor** | `Ctrl+Shift+E` | `Cmd+Shift+E` | Edit SSH configuration |

---

## 🖥️ Terminal Operations

| Action | Linux/Windows | macOS | Description |
|--------|---------------|-------|-------------|
| **Local Terminal** | `Ctrl+Shift+T` | `Cmd+Shift+T` | Open local terminal tab |
| **Broadcast Command** | `Ctrl+Shift+B` | `Cmd+Shift+B` | Send command to multiple terminals |

### Terminal Text Operations

| Action | Linux/Windows | macOS | Description |
|--------|---------------|-------|-------------|
| **Copy** | `Ctrl+Shift+C` | `Cmd+C` | Copy selected text |
| **Paste** | `Ctrl+Shift+V` | `Cmd+V` | Paste text |
| **Select All** | `Ctrl+Shift+A` | `Cmd+A` | Select all terminal text |
| **Zoom In** | `Ctrl+=` (`Ctrl+Plus`) | `Cmd+=` (`Cmd+Plus`) | Increase font size |
| **Zoom Out** | `Ctrl+Minus` | `Cmd+Minus` | Decrease font size |
| **Reset Zoom** | `Ctrl+0` | `Cmd+0` | Reset font size to default |

---

## 📑 Tab Management

| Action | Linux/Windows | macOS | Description |
|--------|---------------|-------|-------------|
| **Next Tab** | `Alt+Right` | `Alt+Right` | Switch to next tab |
| **Previous Tab** | `Alt+Left` | `Alt+Left` | Switch to previous tab |
| **Close Tab** | `Ctrl+F4` | `Cmd+F4` | Close current tab |
| **Tab Overview** | `Ctrl+Shift+Tab` | `Cmd+Shift+Tab` | Show tab overview |

---

## 🎛️ Interface Control

| Action | Linux/Windows | macOS | Description |
|--------|---------------|-------|-------------|
| **Toggle Sidebar** | `F9` or `Ctrl+B` | `F9` or `Cmd+B` | Show/hide connection sidebar |

---

## 📁 File Manager

| Action | Linux/Windows | macOS | Description |
|--------|---------------|-------|-------------|
| **Focus Path Entry** | `Ctrl+L` | `Cmd+L` | Focus the active pane's path entry and select its contents |
| **Refresh Directory** | `F5` or `Ctrl+R` | `F5` or `Cmd+R` | Reload the visible directory in the focused pane |
| **Copy Selection** | `Ctrl+C` | `Cmd+C` | Queue the selected files or folders for copy |
| **Cut Selection** | `Ctrl+X` | `Cmd+X` | Queue the selected files or folders for move |
| **Paste** | `Ctrl+V` | `Cmd+V` | Paste the queued items into the current directory |
| **Paste as Move** | `Ctrl+Shift+V` | `Cmd+Shift+V` | Move queued items into the current directory (force move) |
| **Delete Selection** | `Delete` or `Shift+Delete` | `Delete` or `Shift+Delete` | Delete the selected files or folders in the focused pane |

---

## 🗂️ Connection Dialog Shortcuts

When editing connection settings:

| Action | Linux/Windows | macOS | Description |
|--------|---------------|-------|-------------|
| **Save Connection** | `Ctrl+S` | `Cmd+S` | Save connection changes |
| **Cancel/Close** | `Escape` | `Escape` | Cancel and close dialog |

---

## 🏃 Quick Navigation Tips

### Efficient Connection Workflow
1. **Startup**: Press `Enter` on the highlighted server (the first row at launch) to open or focus it immediately
2. **Search**: Use `Ctrl/Cmd+F` to quickly find connections
3. **Multi-select**: Hold `Ctrl` while clicking to select multiple connections
4. **Quick switch**: Use `Ctrl/Cmd+L` to focus the connection list from anywhere
5. **Remote files**: `Ctrl/Cmd+Shift+O` jumps straight into the remote file manager when available

### Terminal Workflow
1. **New connection**: `Ctrl/Cmd+Alt+N` opens the selected connection in a new tab
2. **Force new tab**: `Ctrl/Cmd+Enter` always opens another tab for the highlighted connection
3. **Local work**: `Ctrl/Cmd+Shift+T` for a local terminal
4. **Tab switching**: `Alt+Left/Right` for quick tab navigation
5. **Broadcast**: `Ctrl/Cmd+Shift+B` to send commands to multiple terminals

### Sidebar Management
- **Toggle visibility**: `F9` or `Ctrl/Cmd+B`
- **Focus list**: `Ctrl/Cmd+L` from terminal
- **Arrow navigation**: Use `Up/Down` arrows to select connections
- **Enter to connect**: Press `Enter` on any selected connection

---

## 💡 Pro Tips

- **Multiple connections**: Select multiple connections with `Ctrl+Click`, then right-click for group operations
- **Keyboard-only workflow**: You can navigate the entire application without touching the mouse
- **Search shortcuts**: In search mode, use `Enter` to connect to the first filtered result
- **Context menus**: Right-click (or menu key) on connections for additional options
- **Quick access**: Memorize `Ctrl/Cmd+Alt+C` for the quick connect dialog when you need to connect to an ad-hoc server

---

*Remote file manager shortcuts and the `Ctrl/Cmd+Shift+O` accelerator are available when the file manager feature is enabled in your build.*

*This document reflects SSH Pilot's current keyboard shortcuts. For the most up-to-date information, press `Ctrl/Cmd+Shift+/` within the application, or access via Help → Keyboard Shortcuts menu.*