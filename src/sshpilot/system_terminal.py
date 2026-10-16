"""Finding and opening the user's system (external) terminal.

Desktop-environment discovery on Linux, Terminal/iTerm and friends on macOS,
and Windows Terminal / PowerShell / cmd on Windows. GLib-free.
"""

from __future__ import annotations

import base64
import logging
import ntpath
import os
import shlex
import shutil
import subprocess
import time
from typing import Dict, List, Optional, Sequence, Tuple

from .platform.detect import is_macos, is_windows

logger = logging.getLogger(__name__)


# Windows terminals in preference order. Windows Terminal hosts a shell, so it
# comes first; the shells themselves get a new console window of their own.
WINDOWS_TERMINALS: Tuple[str, ...] = ('wt.exe', 'pwsh.exe', 'powershell.exe', 'cmd.exe')


def _find_windows_terminal() -> Optional[List[str]]:
    for term in WINDOWS_TERMINALS:
        path = shutil.which(term)
        if path:
            return [path]
    return None


def get_default_terminal_command() -> Optional[List[str]]:
    """Get the default terminal command from the desktop environment."""
    try:
        if is_windows():
            return _find_windows_terminal()

        if is_macos():
            # Map bundle identifiers to display names in preference order.
            mac_terms = {
                'com.apple.Terminal': 'Terminal',
                'com.googlecode.iterm2': 'iTerm',

                'dev.warp.Warp': 'Warp',
                'io.alacritty': 'Alacritty',
                'net.kovidgoyal.kitty': 'Kitty',
                'com.mitmaro.ghostty': 'Ghostty',
            }

            for bundle_id, name in mac_terms.items():
                # First try AppleScript lookup by app name
                try:
                    result = subprocess.run(
                        ['osascript', '-e', f'id of app "{name}"'],
                        capture_output=True,
                        text=True,
                        timeout=2,
                    )
                    if result.returncode == 0 and bundle_id in result.stdout:
                        return ['open', '-a', name]
                except Exception:
                    pass

                # Fallback to Spotlight metadata search by bundle identifier
                try:
                    result = subprocess.run(
                        ['mdfind', f'kMDItemCFBundleIdentifier=={bundle_id}'],
                        capture_output=True,
                        text=True,
                        timeout=2,
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        return ['open', '-a', name]
                except Exception:
                    pass

            return None

        desktop = os.environ.get('XDG_CURRENT_DESKTOP', '').lower()

        if 'gnome' in desktop:
            # Try gnome-terminal first, then ptyxis as fallback
            for term in ('gnome-terminal', 'ptyxis'):
                try:
                    result = subprocess.run(['which', term], capture_output=True, text=True, timeout=2)
                    if result.returncode == 0:
                        return [term]
                except Exception:
                    pass
            return None
        elif 'kde' in desktop or 'plasma' in desktop:
            return ['konsole']
        elif 'xfce' in desktop:
            return ['xfce4-terminal']
        elif 'cinnamon' in desktop:
            return ['gnome-terminal']
        elif 'mate' in desktop:
            return ['mate-terminal']
        elif 'lxqt' in desktop:
            return ['qterminal']
        elif 'lxde' in desktop:
            return ['lxterminal']

        common_terminals = [
            'gnome-terminal', 'ptyxis', 'konsole', 'xfce4-terminal', 'alacritty',
            'kitty', 'terminator', 'tilix', 'guake'
        ]

        for term in common_terminals:
            try:
                result = subprocess.run(['which', term], capture_output=True, text=True, timeout=2)
                if result.returncode == 0:
                    return [term]
            except Exception:
                continue

        return None

    except Exception as e:
        logger.error(f"Failed to get default terminal: {e}")
        return None


def get_user_preferred_terminal(config) -> Optional[List[str]]:
    """Get the user's preferred terminal from settings."""
    try:
        preferred_terminal = config.get_setting('external-terminal', 'gnome-terminal')

        if preferred_terminal == 'custom':
            custom_path = config.get_setting('custom-terminal-path', '')
            if custom_path:
                if is_macos():
                    return ['open', '-a', custom_path]
                return [custom_path]
            else:
                logger.warning("Custom terminal path is not set, falling back to built-in terminal")
                return None

        if is_macos():
            # Preferences may store either an app name ("iTerm") or a full
            # command ("open -a iTerm").  If the value already starts with
            # "open" use it verbatim, otherwise build an "open -a" command
            # for the specified app.
            if preferred_terminal.startswith('open'):
                return shlex.split(preferred_terminal)

            return ['open', '-a', preferred_terminal]

        if is_windows():
            # `which` is not a Windows command; shutil.which honours PATHEXT.
            path = shutil.which(preferred_terminal)
            if path:
                return [path]
            logger.warning(f"Preferred terminal '{preferred_terminal}' not found, falling back to default terminal")
            return None

        try:
            result = subprocess.run(['which', preferred_terminal], capture_output=True, text=True, timeout=2)
            if result.returncode == 0:
                return [preferred_terminal]
            else:
                logger.warning(f"Preferred terminal '{preferred_terminal}' not found, falling back to built-in terminal")
                return None
        except Exception as e:
            logger.error(f"Failed to check preferred terminal '{preferred_terminal}': {e}")
            return None

    except Exception as e:
        logger.error(f"Failed to get user preferred terminal: {e}")
        return None


def find_any_terminal() -> Optional[List[str]]:
    """Last-resort terminal discovery: probe common emulators, then xdg-terminal."""
    if is_windows():
        return _find_windows_terminal()
    for term in (
        'gnome-terminal', 'ptyxis', 'konsole', 'xterm', 'alacritty',
        'kitty', 'terminator', 'tilix', 'xfce4-terminal', 'xdg-terminal',
    ):
        try:
            result = subprocess.run(['which', term], capture_output=True, text=True, timeout=2)
            if result.returncode == 0:
                return [term]
        except Exception:
            continue
    return None


# C0 controls and DEL. Rejected outright rather than escaped: `do script` and
# `write text` type their argument into a shell, where a newline is Enter --
# and AppleScript's \n escape produces a *real* newline in the string value, so
# escaping it makes the script compile and then hands the shell two commands.
# Escaping is the wrong tool here; refusing to launch is the right one.
_CONTROL_CHARS = frozenset(chr(c) for c in range(0x20)) | {'\x7f'}


def _applescript_string(value: str) -> str:
    """Render *value* as an AppleScript string literal, escapes included.

    osascript takes the script as one argument, so anything interpolated into it
    must be escaped or a quote in the value ends the literal and the rest is
    executed as AppleScript. Backslash goes first so the escapes added below
    survive.

    Line breaks are escaped so the literal compiles, but that is *not* what
    makes them safe -- \\n in AppleScript source becomes an actual newline in
    the string, which `do script` would pass straight to the shell. Control
    characters are rejected upstream in open_system_terminal(); this escaping
    only keeps the helper correct in isolation.
    """
    escaped = (
        value.replace('\\', '\\\\')
        .replace('"', '\\"')
        .replace('\n', '\\n')
        .replace('\r', '\\r')
    )
    return '"' + escaped + '"'


def _powershell_literal(value: str) -> str:
    """Render *value* as a single-quoted PowerShell string (no expansion)."""
    return "'" + value.replace("'", "''") + "'"


def _powershell_encoded_args(
    argv: Sequence[str], environment: Optional[Dict[str, str]]
) -> List[str]:
    """PowerShell arguments that run *argv* and keep the window open.

    The script travels as -EncodedCommand (base64 UTF-16LE), so nothing in it
    is reparsed by the Windows command-line splitter, wt.exe's `;` handling or
    cmd.exe -- only PowerShell's own single-quote rules apply.
    """
    lines = [
        f'$env:{name} = {_powershell_literal(value)}'
        for name, value in (environment or {}).items()
        if name.replace('_', '').isalnum()
    ]
    lines.append('& ' + ' '.join(_powershell_literal(arg) for arg in argv))
    encoded = base64.b64encode('\n'.join(lines).encode('utf-16-le')).decode('ascii')
    return ['-NoLogo', '-NoExit', '-EncodedCommand', encoded]


# cmd.exe expands or reinterprets these even inside double quotes, so a
# command containing them is refused rather than escaped.
_CMD_METACHARS = frozenset('%!^&|<>"')


def _cmd_keep_open_args(argv: Sequence[str]) -> Optional[List[str]]:
    if any(_CMD_METACHARS.intersection(arg) for arg in argv):
        logger.error("Refusing to open cmd.exe: command contains cmd metacharacters")
        return None
    return ['/k', subprocess.list2cmdline(list(argv))]


def _open_windows_terminal(
    terminal_command: List[str],
    argv: Sequence[str],
    environment: Optional[Dict[str, str]],
    launch_environment: Dict[str, str],
) -> bool:
    new_console = getattr(subprocess, 'CREATE_NEW_CONSOLE', 0)
    # ntpath splits on both separators; it is os.path on Windows anyway.
    name = ntpath.basename(terminal_command[0]).lower()
    if not name.endswith('.exe'):
        name += '.exe'

    if name == 'wt.exe':
        shell = shutil.which('pwsh.exe') or shutil.which('powershell.exe')
        if shell:
            inner = [shell] + _powershell_encoded_args(argv, environment)
        else:
            cmd_args = _cmd_keep_open_args(argv)
            if cmd_args is None:
                return False
            inner = [shutil.which('cmd.exe') or 'cmd.exe'] + cmd_args
        cmd = terminal_command + ['new-tab', '--'] + inner
        flags = 0
    elif name in ('pwsh.exe', 'powershell.exe'):
        cmd = terminal_command + _powershell_encoded_args(argv, environment)
        flags = new_console
    elif name == 'cmd.exe':
        cmd_args = _cmd_keep_open_args(argv)
        if cmd_args is None:
            return False
        cmd = terminal_command + cmd_args
        flags = new_console
    else:
        cmd = terminal_command + list(argv)
        flags = new_console

    logger.info(f"Launching system terminal: {terminal_command[0]}")
    subprocess.Popen(cmd, env=launch_environment, creationflags=flags)
    return True


def open_system_terminal(
    terminal_command: List[str],
    ssh_command: str,
    *,
    environment: Optional[Dict[str, str]] = None,
    argv: Optional[Sequence[str]] = None,
) -> bool:
    """Launch a terminal emulator running *ssh_command*.

    *ssh_command* is a shell command line (built with ``shlex.join``, so its own
    arguments are already quoted). Terminals that take an argv get it as a
    separate ``bash -c`` argument; the few that only accept a single string get
    it quoted with ``shlex.quote`` / ``_applescript_string`` rather than
    interpolated, so a Host alias containing a quote or backtick cannot break
    out. Returns False on failure.

    Control characters are refused rather than quoted: the AppleScript branches
    type their command into a shell, where a newline is Enter, and no amount of
    escaping changes that. shlex.quote happens to single-quote newlines today,
    but that is a property of a helper two layers away, not a guarantee this
    function should rely on.

    On Windows there is no bash to hand *ssh_command* to, so *argv* (the
    unjoined command) is used when given; otherwise *ssh_command* is split.
    """
    if _CONTROL_CHARS.intersection(ssh_command):
        logger.error("Refusing to open a terminal: command contains control characters")
        return False

    # Keep the shell reading input after ssh exits, so errors stay on screen.
    keep_open = f'{ssh_command}; exec bash'
    launch_environment = os.environ.copy()
    if environment:
        launch_environment.update(environment)
    try:
        if is_windows():
            return _open_windows_terminal(
                terminal_command,
                tuple(argv) if argv is not None else tuple(shlex.split(ssh_command)),
                environment,
                launch_environment,
            )

        if is_macos():
            app = None
            if terminal_command and terminal_command[0] == 'open':
                # handle commands like ['open', '-a', 'App']
                if len(terminal_command) >= 3 and terminal_command[1] == '-a':
                    app = os.path.basename(terminal_command[2])
            if app:
                app_lower = app.lower()
                if app_lower in ['terminal', 'terminal.app']:
                    script = (
                        f'tell app "Terminal" to do script {_applescript_string(ssh_command)}\n'
                        'tell app "Terminal" to activate'
                    )
                    cmd = ['osascript', '-e', script]
                elif app_lower in ['iterm', 'iterm2', 'iterm.app']:
                    script = (
                        'tell application "iTerm"\n'
                        '    if (count of windows) = 0 then\n'
                        '        create window with default profile\n'
                        '    end if\n'
                        '    tell current window\n'
                        '        create tab with default profile\n'
                        f'        tell current session to write text {_applescript_string(ssh_command)}\n'
                        '    end tell\n'
                        '    activate\n'
                        'end tell'
                    )
                    cmd = ['osascript', '-e', script]
                elif app_lower == 'warp':
                    cmd = ['open', f'warp://{ssh_command}']
                    # Warp handles focus automatically via URL scheme
                elif app_lower in ['alacritty', 'kitty']:
                    cmd = ['open', '-a', app, '--args', '-e', 'bash', '-lc', keep_open]
                    # Launch terminal and then activate it
                    subprocess.Popen(cmd, start_new_session=True, env=launch_environment)
                    time.sleep(0.5)  # Give the app time to launch
                    activate_script = f'tell application "{app}" to activate'
                    subprocess.Popen(['osascript', '-e', activate_script])
                    return True
                elif app_lower == 'ghostty':
                    cmd = ['open', '-na', app, '--args', '-e', ssh_command]
                    # Launch terminal and then activate it
                    subprocess.Popen(cmd, start_new_session=True, env=launch_environment)
                    time.sleep(0.5)  # Give the app time to launch
                    activate_script = f'tell application "{app}" to activate'
                    subprocess.Popen(['osascript', '-e', activate_script])
                    return True
                else:
                    cmd = ['open', '-a', app, '--args', 'bash', '-lc', keep_open]
                    # Launch terminal and then activate it
                    subprocess.Popen(cmd, start_new_session=True, env=launch_environment)
                    time.sleep(0.5)  # Give the app time to launch
                    activate_script = f'tell application "{app}" to activate'
                    subprocess.Popen(['osascript', '-e', activate_script])
                    return True
            else:
                cmd = terminal_command + ['--args', 'bash', '-lc', keep_open]
        else:
            terminal_basename = os.path.basename(terminal_command[0])
            if terminal_basename == 'ptyxis':
                # Use --standalone with -- to start fresh instance with only our command
                # This prevents opening a default window when ptyxis isn't running
                cmd = terminal_command + ['--standalone', '--', 'bash', '-c', keep_open]
            elif terminal_basename in ['gnome-terminal', 'tilix', 'xfce4-terminal', 'foot', 'blackbox']:
                cmd = terminal_command + ['--', 'bash', '-c', keep_open]
            elif terminal_basename in ['konsole', 'terminator', 'guake']:
                cmd = terminal_command + ['-e', f'bash -c {shlex.quote(keep_open)}']
            elif terminal_basename in ['alacritty', 'kitty']:
                cmd = terminal_command + ['-e', 'bash', '-c', keep_open]
            elif terminal_basename == 'xterm':
                cmd = terminal_command + ['-e', f'bash -c {shlex.quote(keep_open)}']
            elif terminal_basename == 'xdg-terminal':
                cmd = terminal_command + [ssh_command]
            elif terminal_basename in ['ghostty']:
                cmd = terminal_command + ['+new-window', '-e', 'bash', '-c', keep_open]
            else:
                cmd = terminal_command + [ssh_command]

        logger.info(f"Launching system terminal: {' '.join(cmd)}")
        subprocess.Popen(cmd, start_new_session=True, env=launch_environment)

        # Try to bring the terminal to front on Linux
        if not is_macos():
            try:
                # Try wmctrl first (more reliable)
                result = subprocess.run(['which', 'wmctrl'], capture_output=True, timeout=1)
                terminal_basename = os.path.basename(terminal_command[0])
                if result.returncode == 0:
                    time.sleep(0.5)  # Give the terminal time to launch
                    subprocess.Popen(['wmctrl', '-a', terminal_basename],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    # Fallback to xdotool
                    result = subprocess.run(['which', 'xdotool'], capture_output=True, timeout=1)
                    if result.returncode == 0:
                        time.sleep(0.5)  # Give the terminal time to launch
                        subprocess.Popen(['xdotool', 'search', '--name', terminal_basename, 'windowactivate'],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                # Ignore focus errors - terminal launching is more important
                pass

        return True

    except Exception as e:
        logger.error(f"Failed to open system terminal: {e}")
        return False
