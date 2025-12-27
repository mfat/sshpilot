# sshPilot Execution Flow (run.py & sshpilot/main.py)

## Call Graph
```mermaid
graph TD
    subgraph Launcher
        runpy[run.py __main__]
    end

    subgraph EntryPoint
        mainFn[main()]
        loadRes[load_resources()]
        patchImage[patch_gtk_image()]
    end

    subgraph AppLifecycle
        appInit[SshPilotApplication.__init__]
        setupLog[setup_logging()]
        startupInfo[print_startup_info()]
        applyTheme[apply_color_overrides()]
        actions[create_action() x many]
        sigint[signal handler -> quit/close window]
        connectSignals[self.connect('activate'/'shutdown')]
    end

    subgraph Runtime
        activate[on_activate()/do_activate() -> MainWindow]
        shutdown[on_shutdown() -> process_manager.cleanup_all()]
        quit[quit()/on_quit_action()]
        shortcuts[apply_shortcut_overrides() & helpers]
        actionHandlers[
            Action callbacks:
            on_new_connection/on_open_new_connection_tab/on_toggle_list/
            on_search/on_new_key/on_manage_files/on_local_terminal/
            on_terminal_search/on_preferences/on_about/on_help/
            on_shortcuts/on_tab_next/on_tab_prev/on_tab_close/
            on_tab_overview/on_quick_connect/on_broadcast_command/
            on_edit_ssh_config
        ]
    end

    runpy --> mainFn
    mainFn --> loadRes --> patchImage --> appInit
    appInit --> setupLog --> startupInfo --> applyTheme
    appInit --> actions
    appInit --> connectSignals
    appInit --> sigint
    connectSignals --> activate
    connectSignals --> shutdown
    actions --> actionHandlers
    sigint --> quit
    quit --> shutdown
    actionHandlers -->|delegate| activate
    activate -->|creates| MainWindow
```

## Execution Sequence
1. `run.py` adjusts `sys.path`, imports `sshpilot.main.main`, and calls it when executed directly.【F:run.py†L6-L22】
2. `main()` parses CLI flags (`--verbose`, `--isolated`, `--native-connect`), then instantiates `SshPilotApplication` and runs it.【F:sshpilot/main.py†L751-L768】
3. Module import triggers `load_resources()` to register bundled GTK resources; failure exits the process.【F:sshpilot/main.py†L20-L71】
4. `patch_gtk_image()` is applied so GTK image lookups prefer bundled icons.【F:sshpilot/main.py†L73-L75】
5. `SshPilotApplication.__init__` runs:
   - Configures logging via `setup_logging()` and prints startup info.【F:sshpilot/main.py†L85-L104】
   - Loads persisted configuration, applies theme/accelerator flags, and merges native-connect/isolation modes.【F:sshpilot/main.py†L113-L159】
   - Registers platform-specific `Gio` actions and keyboard shortcuts (including optional file manager actions).【F:sshpilot/main.py†L160-L219】
   - Hooks `activate`/`shutdown` signals and installs a SIGINT handler that routes to window closure/quit.【F:sshpilot/main.py†L226-L256】
6. When the application is activated (`do_activate` or `on_activate`), a `MainWindow` is created/presented if none exists.【F:sshpilot/main.py†L258-L265】【F:sshpilot/main.py†L490-L496】
7. Action callbacks dispatch to the active window for UI operations (opening connections, toggling lists/tabs, managing files, terminals, help/about dialogs, quick connect, broadcast commands, etc.).【F:sshpilot/main.py†L497-L676】
8. Accelerators can be refreshed or suspended based on config changes via `_on_config_setting_changed`, `_apply_shortcut_for_action`, and related helpers.【F:sshpilot/main.py†L319-L456】
9. Shutdown flow (`on_shutdown` or `quit`) cleans up windows, disconnects config handlers, and clears terminal processes before exit.【F:sshpilot/main.py†L266-L317】【F:sshpilot/main.py†L472-L483】

## Function Summaries
- **run.py** – Launcher that exposes `main()` from `sshpilot.main` when executed directly.【F:run.py†L6-L22】
- **load_resources** – Registers bundled GTK resources and sets icon theme lookup order; exits if no bundle is found.【F:sshpilot/main.py†L20-L71】
- **patch_gtk_image** – Monkey-patches GTK image handling to prefer packaged icons.【F:sshpilot/main.py†L73-L75】
- **SshPilotApplication.__init__** – Configures logging, startup metadata, user settings (theme, accelerators, native connect, isolation), registers actions/shortcuts, connects lifecycle signals, and installs SIGINT handling.【F:sshpilot/main.py†L85-L256】
- **setup_logging** – Builds rotating file + console handlers and raises log verbosity based on config or CLI `--verbose`.【F:sshpilot/main.py†L319-L355】
- **_on_config_setting_changed** – Reacts to terminal pass-through setting by toggling accelerators and refreshing windows.【F:sshpilot/main.py†L356-L398】
- **create_action / _apply_shortcut_for_action / apply_shortcut_overrides / _refresh_window_accelerators / _update_accelerators_enabled_flag / get_registered_shortcut_defaults / get_registered_action_order** – Helper suite that registers Gio actions, applies default/overridden shortcuts, exposes shortcut metadata, and updates windows when accelerator state changes.【F:sshpilot/main.py†L399-L470】
- **quit / on_quit_action** – Routes quits through window close handling to ensure confirmation dialogs run before shutdown.【F:sshpilot/main.py†L472-L483】
- **on_edit_ssh_config** – Opens the SSH config editor in the active window if available.【F:sshpilot/main.py†L484-L489】
- **do_activate / on_activate** – Activation handlers that create/present the main window as needed.【F:sshpilot/main.py†L258-L265】【F:sshpilot/main.py†L490-L496】
- **Action callbacks (on_new_connection, on_open_new_connection_tab, on_toggle_list, on_search, on_new_key, on_manage_files, on_local_terminal, on_terminal_search, on_preferences, on_about, on_help, on_shortcuts, on_tab_next, on_tab_prev, on_tab_close, on_tab_overview, on_quick_connect, on_broadcast_command)** – Forward user actions/shortcuts to corresponding `MainWindow` behaviors for connection management, UI focus, tab navigation, dialogs, and broadcast commands.【F:sshpilot/main.py†L497-L676】
- **apply_color_overrides** – Applies user-configured accent color CSS to the GTK display, replacing prior overrides when necessary.【F:sshpilot/main.py†L677-L749】
- **on_shutdown** – Closes windows/file managers, disconnects config signals, and terminates terminal processes during shutdown.【F:sshpilot/main.py†L266-L317】
- **main** – Parses CLI args, instantiates `SshPilotApplication`, and runs the GTK application loop.【F:sshpilot/main.py†L751-L770】
