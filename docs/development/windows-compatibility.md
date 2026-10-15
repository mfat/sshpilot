# Keeping Windows working while developing on Linux

A checklist for changes made on Linux. The design and history of the port
are in [windows-port.md](../architecture/windows-port.md). This page only
covers what to keep in mind so new code does not break Windows.

The ground rule from the port still applies: **Linux and macOS behaviour does
not change for Windows' sake.** Windows gets its own implementation behind a
platform check or a factory. Two changes made during the port did not follow
that rule and do affect Linux. Treat them as product decisions:

- The "Remember until SSH Pilot quits" password option
  (`INTERACTIONS_SESSION_SECRETS`) is advertised on every platform.
- A missing VTE no longer stops the app. `main.py` catches the
  `require_version('Vte')` failure, and the terminal falls back to the
  built-in backend on Linux too.

## How Windows is tested

- `.github/workflows/windows.yml` runs the unit tests and a few GUI tests on
  `windows-latest`. It runs **only on pull requests and manual dispatch.**
  Commits pushed straight to `dev` or `main` are not tested on Windows. For
  anything that touches the areas below, open a PR, or start the workflow by
  hand (`gh workflow run windows.yml --ref <branch>`).
- `tests/windows_posix_only.txt` lists tests that assume POSIX. On Windows,
  `tests/conftest.py` runs each one as a non-strict xfail, and does not
  collect modules listed with `collect:`. On Linux and macOS the file is not
  read, so listed tests still run normally there. Add a test to it only when
  the test itself is about POSIX behaviour (fork, file modes, Unix sockets).
  If the test fails because the code is broken on Windows, fix the code.
- Headless tests do not catch everything. A module-level mistake in
  `daemon/launcher.py` once passed every test and crashed the GUI on its
  first daemon connect (windows-port.md, section 10).

## Where the platforms split

| Concern | Linux / macOS | Windows | Use |
|---|---|---|---|
| Daemon IPC | Unix socket, mode 0600 | Loopback TCP, token preamble, endpoint JSON file | `api/transport/endpoint.py` (`USES_TCP_ENDPOINT`, `connect`) |
| Askpass channel | Unix socket | `tcp:127.0.0.1:PORT`, `askpass.cmd` | `daemon/interaction_broker.py` |
| Session PTY | `daemon/pty_runner.py` | `daemon/conpty_runner.py` (ConPTY via ctypes) | runner chosen once in `daemon/server.py` |
| Local shell tab | `pty.fork` + `$SHELL` | ConPTY + `terminal.windows_shell` | `local_shell_pty.LocalShellProcess` |
| Terminal widget | VTE | built-in view, libvterm by default, pyte fallback | `terminal_backends.VTE_AVAILABLE`, `BaseTerminalBackend` |
| Paths | XDG dirs | `%APPDATA%`, `%LOCALAPPDATA%` | `platform/paths.py` |
| File locks | `fcntl.flock` | `msvcrt.locking` | `platform/locking.py` |
| Private files | `chmod 0600` | owner-only ACL | `platform/windows/acl.py` |
| Connection sharing | ControlMaster | none, forced off | `ssh_multiplex.MULTIPLEXING_SUPPORTED`, `controlmaster_args()` |
| Detached child | `start_new_session=True` | `CREATE_NEW_PROCESS_GROUP \| CREATE_NO_WINDOW` | `daemon/launcher.detached_process_options()` |
| ssh-agent | `SSH_AUTH_SOCK` | named pipe `\\.\pipe\openssh-ssh-agent` | `providers/system_agent.system_agent_socket()` |
| Serial / RDP plugins | picocom / FreeRDP | plink / mstsc | `_windows()` seam in each plugin |
| Shell for user commands | `sh -lc` | `cmd /d /s /c` | `platform/process.windows_cmd_command_line()` |
| Is a pid alive? | `os.kill(pid, 0)` | `psutil.pid_exists` | `platform/process.pid_alive()` |
| Directory fsync | `os.fsync` on the directory | skipped | `platform/fs.fsync_directory()` |
| Platform checks | | | `platform/detect.py` (`is_windows`, `is_macos`, `is_flatpak`), GLib-free |
| External terminal | desktop default / Terminal.app | Windows Terminal, PowerShell, cmd | `system_terminal.py` |

## Rules of thumb

### Imports

`fcntl`, `termios`, `pty`, `pwd`, `grp` and `resource` do not exist on
Windows. A module that Windows imports must not import them at the top. Import
them inside the POSIX branch, or guard the import. Modules that only POSIX
code imports may use them freely: `daemon/pty_runner.py` does, and
`server.py` never imports it on Windows.
`tests/architecture/test_platform_portability.py` enforces this on every
platform; add a file to its `POSIX_ONLY_FILES` only when Windows never
imports it. The same applies to `Vte`. Use
`terminal_backends.VTE_AVAILABLE`, and do not assume the terminal is a
`Vte.Terminal`.

### Processes and signals

- `os.kill(pid, 0)` **kills** the process on Windows: Python calls
  `TerminateProcess` for any signal other than the console CTRL events. To
  check whether a pid is alive, use `platform.process.pid_alive(pid,
  on_error=...)`. `on_error` is the answer when the probe itself fails;
  callers differ on purpose (`process_registry` treats an unknown pid as
  alive, the terminal tab as gone, orphan cleanup leaves it alone with
  `None`). The architecture test rejects `os.kill(x, 0)` anywhere else.
- `os.killpg`, `os.getpgid`, `os.setsid`, `os.fork`, `SIGKILL`, `SIGHUP`,
  `SIGALRM` and `SIGCHLD` are POSIX only. To end a process and its children,
  use psutil on Windows (see `conpty_runner`). Write
  `getattr(signal, "SIGKILL", signal.SIGTERM)` when you need a hard kill
  signal, and use `threading.Timer` instead of `signal.alarm`.
- `start_new_session=True` is ignored on Windows. Use
  `detached_process_options()` for background children.
- A child started without `CREATE_NO_WINDOW` flashes a console window in the
  windowed (frozen) build. Children of the daemon inherit its hidden console.
  Commands started directly from the GUI process do not: pass
  `**platform.process.hidden_console_kwargs()` to `run` / `Popen` there. It
  is empty on POSIX (it follows the real interpreter, not a patched
  `sys.platform`). Never use it where a console window is the point, such as
  the external terminal launch (`CREATE_NEW_CONSOLE`).
- `GLib.unix_signal_add`, `GLib.unix_fd_add_full` and `Gio.UnixFDList` are
  POSIX only. Keep them on POSIX-only paths or behind a `try`.

### Waiting on subprocess output

On Windows, `selectors` and `select.select` only work on sockets. Passing a
pipe from `Popen` raises `OSError: [WinError 10038]`. To stream a child's
stdout and stderr, use a selector on POSIX and one reader thread per pipe on
Windows. `daemon/broadcast_service.py` and
`daemon/privileged_file_service.py` show the pattern.

### The daemon "socket path"

Most code still calls it the socket path. On Windows it is a JSON file holding
the port, the token and the pid. Connect through
`api/transport/endpoint.connect()` and the helpers in `daemon/lifecycle.py`.
Do not create an `AF_UNIX` socket, and do not `stat` or `chmod` the path as if
it were a socket.

### Environment of child processes

When you build an allowlisted `env={...}` for `ssh`, `ssh-add` or
`ssh-keygen`, also keep the names in
`platform/windows/environment.WINDOWS_PROCESS_ENV`, and compare names in upper
case on Windows. Without `PROGRAMDATA` (and `SystemRoot` and similar), OpenSSH
for Windows exits with code 255 and prints nothing. See
`daemon/identity_service._base_env`.

### OpenSSH

- Use the system `ssh`. Do not add Paramiko or a bundled ssh. At startup,
  `prefer_builtin_openssh()` puts `System32\OpenSSH` first on `PATH`, so a
  plain `ssh` does not resolve to the MSYS copy that Git for Windows ships.
- Code that adds `ControlMaster`, `ControlPath` or `ControlPersist` must check
  `MULTIPLEXING_SUPPORTED`, or go through `controlmaster_args()`. Do not
  assume that a second channel to a host can reuse a login. On Windows each
  tab, SFTP channel and forward authenticates on its own.
- Quit never treats files in the socket directory as masters on Windows.
  `daemon/control_masters.control_masters_possible()` is the seam: when it is
  false, the default survey, the daemon's shutdown sweep and the quit
  verification all see no masters, so a stray file in
  `%LOCALAPPDATA%\sshPilot\run\cm` cannot block quitting. Tests patch it to
  drive both branches.
- `ssh-copy-id` does not exist on Windows. `platform/windows/copy_id.py`
  replaces it.

### Shell commands and quoting

- There is no `sh` on Windows (unless Git's happens to be on `PATH`). A user
  command needs `%COMSPEC% /d /s /c "<command>"` there, passed as one string:
  `platform.process.windows_cmd_command_line()`.
- `shlex.quote` and `shlex.join` produce POSIX quoting. Use them for commands
  that run **on the remote host** or under `sh`. For a local Windows command
  line, use `subprocess.list2cmdline` or `local_shell_pty.windows_command_line`.
- `shlex.split` in POSIX mode treats `\` as an escape and breaks
  `C:\Users\...`. For `Include` lines in ssh config, use
  `ssh_config_document.split_include_args`.

### Paths and files

- Get directories from `platform/paths.py` (`get_config_dir`, `get_data_dir`,
  `get_state_dir`, `get_runtime_dir`, `get_ssh_dir`). Do not hardcode
  `~/.config`, `~/.local/share`, `XDG_*` or `/tmp`. The GLib-backed getters in
  `platform_utils` hand off to these on Windows, where GLib would prefer
  `%HOME%` over `%USERPROFILE%`.
- Remote paths are always POSIX. Use `posixpath` (or `remote_path_utils`) for
  them, never `os.path`, which uses `\` on Windows.
- `os.rename` fails on Windows when the target exists. Use `os.replace` when
  you mean to overwrite.
- Windows cannot `os.open` a directory, so fsyncing the parent directory
  after an atomic write must be skipped there. Use
  `platform.fs.fsync_directory()`, which raises `OSError` for the caller to
  convert.
- `chmod` does nothing useful on Windows, and OpenSSH rejects keys and config
  files that others can read. Use `platform/windows/acl.py` for files that
  must be private.
- File locks go through `platform/locking.py`, not `fcntl.flock`.
- A file that is open cannot be deleted or replaced on Windows. Close it
  first.
- `:`, `<`, `>`, `"`, `|`, `?` and `*` are not allowed in Windows file names,
  and names such as `CON` or `NUL` are reserved. Sanitise local file names
  built from host names, `user@host:port` strings or remote file names.
- `Path.home()` and `expanduser` read `USERPROFILE` on Windows, not `HOME`.
  A test that only sets `HOME` does not redirect them there.

### Text encoding

Python 3.14 on Windows does not use UTF-8 by default. `open()`,
`Path.read_text()` and `subprocess.run(..., text=True)` use the ANSI code
page (usually cp1252) when no encoding is given. Non-ASCII output can then be
garbled, or raise `UnicodeDecodeError`. In new code, pass
`encoding="utf-8"` (and `errors="replace"` for child output you only
display). Text mode on Windows also writes `\r\n` for `\n`. Pass
`newline=""` or use binary mode when line endings must be preserved, as in
ssh config and `known_hosts` writers.

### Terminal

- If you change terminal behaviour through VTE (mouse, paste, resize,
  scrollback, OSC handling, shortcuts, search), check whether the built-in
  view (`terminal_backend_pyte.py` with `vterm_terminal_model.py` /
  `pyte_terminal_model.py`) needs the same change, or note that it is VTE
  only.
- You can run the built-in backend on Linux: set `terminal.backend = "pyte"`,
  and pick the model with `terminal.builtin_model` or
  `SSHPILOT_TERMINAL_MODEL=vterm`. This is the cheapest way to test the
  Windows terminal from Linux. Only the ConPTY layer differs.

### Plugins

- Keep the POSIX tool as the default. Add the Windows choice behind a
  `_windows()` helper in the plugin, as the serial and RDP plugins do. Tests
  can then patch the helper instead of `sys.platform`.
- `PluginContext.run_local_command` and `run_local_command_stream`
  (`plugins/api.py`) run `sh -lc` on POSIX (through `flatpak-spawn --host`
  under Flatpak). On Windows they run `%COMSPEC% /d /s /c "<command>"`,
  falling back to `cmd.exe`. This is passed as one string, not a list. A list
  goes through `list2cmdline`, which turns every `"` into `\"`, and `cmd`
  does not understand that. The daemon's pre-connection command
  (`daemon/pre_connection_command._shell_argv`) does the same; both build the
  line with `platform.process.windows_cmd_command_line()`. Windows output
  is decoded as UTF-8 with `errors="replace"`, and the console is hidden.
  The branch sits behind `plugins.api._windows()`, which tests patch.
- The command string is shell syntax, so build it from an argv with
  `plugins.api.local_command_line()`. It uses `shlex.join` on POSIX and
  `subprocess.list2cmdline` on Windows. A command that relies on `sh`
  features (pipes into `sh` builtins, `$(...)`, `2>/dev/null`) still needs
  its own Windows form. The Docker manager's client quotes every argument
  through the runner it is given. Its local runner carries
  `local_command_line` and the `_windows()` result, so on Windows the
  `--format` templates and names are double-quoted, runtime detection uses
  `where`, and compose files are read with `type`. Remote commands and POSIX
  local commands keep their `sh` quoting byte for byte.

## Before merging

1. Search the diff for `AF_UNIX`, `os.getuid`, `os.kill(`, `killpg`,
   `SIGKILL`, `SIGALRM`, `fcntl`, `termios`, `/tmp`, `XDG_`, `~/.`,
   `chmod(`, `ControlMaster`, `Vte.`, `"sh"`, `shlex.`, `selectors`,
   `text=True`, `open(`, `os.rename`:

   ```bash
   git diff main... | grep -nE '^\+.*(AF_UNIX|os\.getuid|os\.kill\(|killpg|SIGKILL|SIGALRM|fcntl|termios|/tmp|XDG_|chmod\(|ControlMaster|Vte\.|"sh"|shlex\.|selectors|text=True|os\.rename|open\()'
   ```

   Each hit should be on a POSIX-only path, behind a platform check, or safe
   on both platforms.
2. For changes to the transport, PTY or terminal, process spawning, the
   askpass path, or anything in `platform/`, open a PR so the Windows workflow
   runs.
3. Follow one session through both platforms in your head: daemon connect,
   ssh launch, askpass, PTY I/O, terminal drawing.
