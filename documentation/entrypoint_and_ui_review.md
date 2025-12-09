# Entry Points and UI Migration Notes

## Application boot flow and dependency wiring
- **run.py** inserts repository and `src` on `sys.path`, then imports `sshpilot.main.main()`; no other initialization occurs in this shim.
- **sshpilot/main.py** loads GTK/Adw/VTE resources early via `load_resources()`, patches `Gtk.Image` icons, and defines `SshPilotApplication`.
  - Constructor wires logging, config, accelerator overrides, and native-connect flags before registering Gio actions (quit, new connection, tab navigation, etc.).
  - Activation (`do_activate`/`on_activate`) instantiates `MainWindow(application=self, isolated=...)` and presents it; shutdown disconnects config handlers and calls `terminal.process_manager.cleanup_all()`.
  - Command-line parsing happens in `main()` (`--verbose`, `--isolated`, `--native-connect`), which constructs `SshPilotApplication` and runs it.
- **MainWindow (window.py)** depends on `Config`, `ConnectionManager`, `KeyManager`, and `GroupManager` injected during `__init__`, and registers GTK actions plus window cleanup hooks.

## GTK widget and signal catalog (selected UI modules)
- **window.py / MainWindow**
  - Widgets: `Adw.ApplicationWindow`, `Gtk.HeaderBar` (sidebar/home toggle buttons), `Adw.OverlaySplitView`/`Adw.NavigationSplitView`/`Gtk.Paned`, `Adw.ToastOverlay`, `Adw.TabView`/`Gtk.Notebook` for terminals, `Gtk.ListView` sidebar rows from `sidebar.py`.
  - Signals/handlers: window `close-request`, config `setting-changed`, action callbacks for sidebar toggle, tab overview, quick connect, etc.
  - Data/model usage: `ConnectionManager` (connection data and native-connect flag), `Config` (settings and shortcuts), `GroupManager` (sidebar grouping), `TerminalManager`/`TerminalWidget` (active terminal tracking).
- **terminal.py / TerminalWidget**
  - Widgets: `Gtk.Box` container wrapping `Gtk.ScrolledWindow`, `Gtk.Overlay` with search banner (`Gtk.SearchEntry`, navigation buttons), and VTE backend (`VTETerminalBackend` -> `Vte.Terminal`).
  - Signals: custom `connection-established`, `connection-failed(str)`, `connection-lost`, `title-changed(str)` plus GObject connections to `ConnectionManager` updates.
  - Data/model usage: `connection` record (from `ConnectionManager`), `Config` for terminal settings and pass-through mode, `SSHProcessManager` for lifecycle, backend selection via `terminal_backends`.
- **connection_dialog.py / ConnectionDialog**
  - Widgets: modal `Adw.Window` with `Adw.HeaderBar`, `Gtk.Notebook` tabs, form controls (`Gtk.Entry`/`Gtk.SearchEntry`/`Gtk.SpinButton`), toggles (`Gtk.Switch`/`Gtk.CheckButton`), and file dialogs for key selection.
  - Signals: custom `connection-saved` plus inline validators; hooks to `Gtk` entry signals and GLib idle loading.
  - Data/model usage: `SSHConnectionValidator`, `connection_manager` for persistence, platform helpers (`get_ssh_dir`, `is_macos`) for defaults.

## UI-agnostic utility modules to preserve
- **Configuration and platform**: `config.py` (settings, JSON/GSettings), `platform_utils.py` (paths, OS checks), `update_checker.py` (HTTP version checks).
- **SSH/back-end helpers**: `ssh_utils.py`, `ssh_password_exec.py`, `scp_utils.py`, `port_utils.py`, `terminal_backends.py` abstraction layer, `askpass_utils.py`.
- **Storage/data**: `connection_manager.py` (connection records and signals), `groups.py`, `key_manager.py`, `connection_sort.py`.

## Migration spreadsheet
See `documentation/gtk_to_qt_migration.csv` for widget-to-Qt mappings covering main window, terminal, and connection dialog components.
