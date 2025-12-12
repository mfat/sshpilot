# sshPilot Main Execution Flow

This document visualizes the startup and primary action flows defined in `sshpilot/main.py` and the entry wrapper in `run.py`.

## Flowchart
```mermaid
flowchart TD
    entry[run.py __main__] --> setpath[Configure sys.path]
    setpath --> runner[call sshpilot.main.main]
    runner --> parseargs[Parse CLI flags (--verbose, --isolated, --native-connect)]
    parseargs --> app[SshPilotApplication(...)]

    subgraph AppInit[SshPilotApplication.__init__]
        app --> loadres[load_resources()]
        loadres -->|success| patchimg[patch_gtk_image()]
        loadres -->|failure| exit[sys.exit(1)]
        patchimg --> logsetup[setup_logging()]
        logsetup --> startup[print_startup_info()]
        startup --> cfgload[Load Config() + theme/shortcut/native-connect settings]
        cfgload --> actions[create_action registrations (keyboard shortcuts)]
        actions --> signals[connect('shutdown'/'activate') signals]
        signals --> sigint[Optional SIGINT handler closes active window]
    end

    runner --> runapp[app.run(None)]
    runapp --> activate{{on_activate / do_activate}}
    activate --> newwin[MainWindow(application=self, isolated=...)]
    newwin --> present[window.present()]

    subgraph ActionHandlers[Action handlers]
        actions --> onNewConn[on_new_connection → window.show_connection_dialog()]
        actions --> onNewTab[on_open_new_connection_tab → window.open_new_connection_tab_action]
        actions --> onToggleList[on_toggle_list → window.toggle_list_focus()]
        actions --> onSearch[on_search → window.focus_search_entry()]
        actions --> onNewKey[on_new_key → connection selection → on_copy_key_to_server_clicked]
        actions --> onManageFiles[on_manage_files → on_manage_files_button_clicked/_open_manage_files_for_connection]
        actions --> onLocalTerm[on_local_terminal → window.terminal_manager.show_local_terminal()]
        actions --> onTermSearch[on_terminal_search → toggle_terminal_search_overlay(select_all=True)]
        actions --> onPrefs[on_preferences → window.show_preferences()]
        actions --> onAbout[on_about → window.show_about_dialog()]
        actions --> onHelp[on_help → window.open_help_url()]
        actions --> onShortcuts[on_shortcuts → window.show_shortcuts_window()]
        actions --> onTabNext[on_tab_next → window._select_tab_relative(+1)]
        actions --> onTabPrev[on_tab_prev → window._select_tab_relative(-1)]
        actions --> onTabClose[on_tab_close → tab_view.close_page(selected)]
        actions --> onTabOverview[on_tab_overview → tab_overview.set_open(toggle)]
        actions --> onQuickConnect[on_quick_connect → QuickConnectDialog.present()]
        actions --> onBroadcast[on_broadcast_command → window.broadcast_command_action.activate(None)]
        actions --> onQuit[on_quit_action → app.quit()]
    end

    quitpath[app.quit()] -->|active window| windowclose[window.on_close_request → close or cancel]
    quitpath -->|no window| appExit[Adw.Application.quit]

    shutdown[on_shutdown signal]
    shutdown --> closeWins[Iterate application + registry windows → cleanup/close managers]
    shutdown --> disconnectCfg[Disconnect config handler]
    shutdown --> procCleanup[terminal.process_manager.cleanup_all()]
    procCleanup --> end[Process exit]
```

## Execution Sequence
1. **Entry wrapper**: `run.py` adjusts `sys.path` to include the repo root and `src/`, then imports `sshpilot.main.main` and calls it when executed directly.
2. **Argument parsing**: `main()` in `sshpilot/main.py` parses `--verbose`, `--isolated`, and `--native-connect` flags.
3. **Resource bootstrap**: `load_resources()` locates the compiled GResource file and registers it; failure exits the process. It also ensures bundled icons are prioritized.
4. **Startup patching**: `patch_gtk_image()` modifies `Gtk.Image` behavior to prefer bundled icons before any UI is created.
5. **Application creation**: `SshPilotApplication` is instantiated with parsed flags.
6. **Initialization pipeline** within `__init__`:
   - Configure logging via `setup_logging()` (RotatingFileHandler + console, respecting verbose settings).
   - Print startup info.
   - Attempt to load `Config` to set theme, shortcut overrides, accelerator state, isolated/native-connect flags, and color overrides.
   - Register all GActions via `create_action` with platform-specific accelerators and connect application signals (`activate`, `shutdown`).
   - Install a SIGINT handler that closes the active window (or quits) via GLib idle callback.
7. **Application run loop**: `app.run(None)` starts the GLib/GTK loop. Activation events invoke `on_activate`/`do_activate` to create and present a `MainWindow` if none exists.
8. **User interactions**: Action callbacks route to `MainWindow` helpers (dialog display, tab operations, file management, preferences/help overlays, quick connect, broadcast command, etc.). Shortcuts are subject to accelerator enablement and configuration overrides.
9. **Quit path**: `on_quit_action` funnels to `app.quit()`, which delegates to the active window’s `on_close_request`; if confirmed, it proceeds to the base `Adw.Application.quit`.
10. **Shutdown cleanup**: `on_shutdown` iterates open and registry-tracked windows to invoke cleanup hooks, disconnects config signal handlers, and calls `terminal.process_manager.cleanup_all()` before exit.

## Function Notes
- `load_resources()`: Loads and registers GResource bundles, prioritizes bundled icon paths, and aborts startup on failure.
- `patch_gtk_image()`: Patches `Gtk.Image` so icon resolution prefers bundled assets.
- `SshPilotApplication.__init__`: Central initializer that configures logging, theming, shortcut state, actions, and signals; installs SIGINT handler and records configuration flags.
- `setup_logging()`: Sets rotating file and console handlers, honoring verbose/debug toggles from CLI and config.
- `_on_config_setting_changed()`, `_apply_shortcut_for_action()`, `apply_shortcut_overrides()`, `_refresh_window_accelerators()`, `_update_accelerators_enabled_flag()`: Manage dynamic shortcut/accelerator state when preferences change.
- `create_action()`: Wraps creation of `Gio.SimpleAction`, binding callbacks and default shortcuts.
- `get_registered_shortcut_defaults()` / `get_registered_action_order()`: Accessors for registered shortcut metadata.
- `quit()` / `on_quit_action()`: Consistent shutdown path via window close confirmation before quitting the app.
- `on_activate()` / `do_activate()`: Ensure a `MainWindow` exists and is presented on activation.
- `Action handlers` (`on_new_connection`, `on_open_new_connection_tab`, `on_toggle_list`, `on_search`, `on_new_key`, `on_manage_files`, `on_local_terminal`, `on_terminal_search`, `on_preferences`, `on_about`, `on_help`, `on_shortcuts`, `on_tab_next`, `on_tab_prev`, `on_tab_close`, `on_tab_overview`, `on_quick_connect`, `on_broadcast_command`): Each routes keyboard-initiated actions to the active window’s methods for dialogs, tab management, search, file operations, terminal features, and command broadcast.
- `on_shutdown()`: Finalizes cleanup for windows and terminal processes, ensuring config handlers are detached.
- `main()`: CLI entry that constructs `SshPilotApplication` and starts the GTK event loop via `run(None)`.
