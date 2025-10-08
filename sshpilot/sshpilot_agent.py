#!/usr/bin/env python3
"""
SSHPilot Agent - Host-side PTY handler for Flatpak sandbox

This agent runs on the host system (outside the Flatpak sandbox) and handles
proper PTY creation and shell spawning to avoid job control issues.

Inspired by the Ptyxis agent architecture.
"""

import os
import sys
import pty
import tty
import termios
import fcntl
import subprocess
import struct
import select
import signal
import pwd
import json
import logging
from typing import Optional, Tuple

# Set up logging
logging.basicConfig(
    level=logging.DEBUG if '--verbose' in sys.argv else logging.WARNING,
    format='[sshpilot-agent] %(levelname)s: %(message)s'
)
logger = logging.getLogger(__name__)


class PTYAgent:
    """Agent that manages PTY creation and shell spawning on the host"""

    def __init__(self):
        self.master_fd: Optional[int] = None
        self.slave_fd: Optional[int] = None
        self.shell_pid: Optional[int] = None
        self.original_termios = None
        self.control_fd: Optional[int] = None
        self._control_buffer = b''

    def set_control_fd(self, fd: Optional[int]):
        """Configure the optional control channel file descriptor."""
        self.control_fd = fd
        self._control_buffer = b''

        if fd is None:
            return

        try:
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        except OSError as exc:
            logger.debug(f"Failed to configure control FD {fd}: {exc}")
        
    def discover_shell(self) -> str:
        """Discover the user's preferred shell on the host system"""
        try:
            # Try SHELL environment variable first
            shell = os.environ.get('SHELL')
            if shell and os.path.isfile(shell):
                logger.debug(f"Found shell from SHELL env: {shell}")
                return shell
        except Exception as e:
            logger.debug(f"Failed to get SHELL env: {e}")
        
        try:
            # Try getent passwd for current user
            username = os.environ.get('USER') or pwd.getpwuid(os.getuid()).pw_name
            result = subprocess.run(
                ['getent', 'passwd', username],
                capture_output=True,
                text=True,
                check=True
            )
            if result.stdout:
                # Format: username:x:uid:gid:gecos:home:shell
                fields = result.stdout.strip().split(':')
                if len(fields) >= 7:
                    shell = fields[6]
                    if shell and os.path.isfile(shell):
                        logger.debug(f"Found shell from getent: {shell}")
                        return shell
        except Exception as e:
            logger.debug(f"Failed to get shell from getent: {e}")
        
        try:
            # Try pwd module as fallback
            shell = pwd.getpwuid(os.getuid()).pw_shell
            if shell and os.path.isfile(shell):
                logger.debug(f"Found shell from pwd module: {shell}")
                return shell
        except Exception as e:
            logger.debug(f"Failed to get shell from pwd: {e}")
        
        # Final fallback
        logger.warning("Could not determine user shell, falling back to /bin/bash")
        return '/bin/bash'
    
    def create_pty(self) -> Tuple[int, int]:
        """
        Create a PTY master/slave pair with proper flags.
        
        This is the critical fix: we create the PTY with O_NOCTTY to prevent
        automatic controlling TTY allocation, then the shell can properly claim
        it with TIOCSCTTY.
        """
        try:
            # Create PTY pair - returns (master_fd, slave_fd)
            master_fd, slave_fd = pty.openpty()
            
            # Set O_NOCTTY on slave to prevent automatic controlling TTY
            # This allows the shell to properly claim the TTY later
            flags = fcntl.fcntl(slave_fd, fcntl.F_GETFL)
            fcntl.fcntl(slave_fd, fcntl.F_SETFL, flags | os.O_NOCTTY)
            
            logger.debug(f"Created PTY: master_fd={master_fd}, slave_fd={slave_fd}")
            
            self.master_fd = master_fd
            self.slave_fd = slave_fd
            
            return master_fd, slave_fd
            
        except Exception as e:
            logger.error(f"Failed to create PTY: {e}")
            raise
    
    def set_pty_size(self, rows: int, cols: int):
        """Set the PTY size"""
        if self.master_fd is None:
            return

        try:
            winsize = struct.pack('HHHH', rows, cols, 0, 0)
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)
            logger.debug(f"Set PTY size to {rows}x{cols}")
        except Exception as e:
            logger.error(f"Failed to set PTY size: {e}")

    def _handle_control_messages(self):
        """Process pending messages on the control channel."""
        if self.control_fd is None:
            return

        try:
            data = os.read(self.control_fd, 4096)
        except BlockingIOError:
            return
        except OSError as exc:
            logger.debug(f"Control FD read error: {exc}")
            self._close_control_fd()
            return

        if not data:
            logger.debug("Control FD closed by peer")
            self._close_control_fd()
            return

        self._control_buffer += data

        while b'\n' in self._control_buffer:
            raw_line, self._control_buffer = self._control_buffer.split(b'\n', 1)
            line = raw_line.strip()
            if not line:
                continue

            try:
                message = json.loads(line.decode('utf-8'))
            except json.JSONDecodeError as exc:
                logger.warning(f"Invalid control message: {line!r} ({exc})")
                continue

            self._process_control_message(message)

    def _process_control_message(self, message: dict):
        """Handle a structured control message."""
        msg_type = message.get('type')

        if msg_type == 'resize':
            rows = message.get('rows')
            cols = message.get('cols')
            if isinstance(rows, int) and isinstance(cols, int) and rows > 0 and cols > 0:
                self.set_pty_size(rows, cols)
                if self.shell_pid:
                    try:
                        os.kill(self.shell_pid, signal.SIGWINCH)
                    except ProcessLookupError:
                        logger.debug("Shell process gone while handling resize")
                    except OSError as exc:
                        logger.debug(f"Failed to signal shell about resize: {exc}")
            else:
                logger.debug(f"Invalid resize payload: {message}")
        else:
            logger.debug(f"Ignoring unknown control message type: {msg_type}")

    def _close_control_fd(self):
        if self.control_fd is None:
            return

        try:
            os.close(self.control_fd)
        except OSError:
            pass
        finally:
            self.control_fd = None
    
    def spawn_shell(self, shell: str, cwd: Optional[str] = None) -> int:
        """
        Spawn the user's shell with proper PTY setup.
        
        This runs the shell as a child process with the PTY slave as its
        controlling terminal, ensuring proper job control.
        """
        if self.slave_fd is None:
            raise RuntimeError("PTY not created yet")

        try:
            # Prepare environment
            env = os.environ.copy()
            env['TERM'] = env.get('TERM', 'xterm-256color')
            env['SHELL'] = shell
            
            # Set working directory
            if cwd is None:
                cwd = os.path.expanduser('~')
            
            # Fork to create shell process
            pid = os.fork()
            
            if pid == 0:
                # Child process
                try:
                    # Create new session and make this process the session leader
                    os.setsid()
                    
                    # Close master FD in child
                    os.close(self.master_fd)
                    
                    # Make the PTY slave the controlling terminal
                    # This is the critical operation that fixes job control
                    fcntl.ioctl(self.slave_fd, termios.TIOCSCTTY, 0)
                    
                    # Redirect stdin, stdout, stderr to PTY slave
                    os.dup2(self.slave_fd, 0)
                    os.dup2(self.slave_fd, 1)
                    os.dup2(self.slave_fd, 2)
                    
                    # Close the original slave FD if it's not one of stdin/stdout/stderr
                    if self.slave_fd > 2:
                        os.close(self.slave_fd)
                    
                    # Change to working directory
                    os.chdir(cwd)
                    
                    # Execute the shell as a login shell
                    os.execvpe(shell, [shell, '-l'], env)
                    
                except Exception as e:
                    logger.error(f"Child process failed: {e}")
                    sys.exit(1)
            
            else:
                # Parent process
                # Close slave FD in parent (child has its own copy)
                os.close(self.slave_fd)
                self.slave_fd = None
                
                self.shell_pid = pid
                logger.info(f"Spawned shell: {shell} (PID: {pid})")

                return pid

        except Exception as e:
            logger.error(f"Failed to spawn shell: {e}")
            raise

    def _send_status(self, status_type: str, **payload):
        """Send a structured status message to stderr for the caller."""
        stream = getattr(sys, 'stderr', None)
        if not stream:
            return

        # When the agent is spawned through VTE the stderr stream is the
        # interactive terminal itself, so emitting JSON control messages would be
        # visible to the user.  Only send structured messages when stderr is a
        # pipe (or otherwise not a tty), which covers the non-VTE execution path
        # used by the Flatpak launcher while keeping the terminal output clean.
        if hasattr(stream, 'isatty') and stream.isatty():
            return

        try:
            message = {'type': status_type}
            message.update(payload)
            stream.write(json.dumps(message) + '\n')
            stream.flush()

        except Exception as exc:
            logger.debug(f"Failed to send status message: {exc}")

    def io_loop(self):
        """
        Main I/O loop: relay data between master PTY and stdin/stdout.

        This forwards:
        - stdin (from VTE) -> master_fd (to shell)
        - master_fd (from shell) -> stdout (to VTE)
        """
        if self.master_fd is None:
            raise RuntimeError("PTY not created yet")
        
        # Set stdin to raw mode to pass through all data
        try:
            self.original_termios = termios.tcgetattr(sys.stdin.fileno())
            tty.setraw(sys.stdin.fileno())
        except Exception as e:
            logger.debug(f"Could not set raw mode on stdin: {e}")
        
        logger.debug("Starting I/O loop")
        
        try:
            stdin_fd = sys.stdin.fileno()

            while True:
                # Use select to wait for data on stdin/master_fd/control_fd
                read_fds = [stdin_fd, self.master_fd]
                if self.control_fd is not None:
                    read_fds.append(self.control_fd)

                readable, _, _ = select.select(read_fds, [], [])

                for fd in readable:
                    if fd == stdin_fd:
                        # Data from VTE -> forward to shell
                        try:
                            data = os.read(stdin_fd, 4096)
                            if not data:
                                logger.debug("stdin closed")
                                return
                            os.write(self.master_fd, data)
                        except OSError as e:
                            logger.debug(f"stdin read error: {e}")
                            return

                    elif fd == self.master_fd:
                        # Data from shell -> forward to VTE
                        try:
                            data = os.read(self.master_fd, 4096)
                            if not data:
                                logger.debug("master_fd closed")
                                return
                            os.write(sys.stdout.fileno(), data)
                            sys.stdout.flush()
                        except OSError as e:
                            logger.debug(f"master_fd read error: {e}")
                            return

                    elif self.control_fd is not None and fd == self.control_fd:
                        self._handle_control_messages()
        
        except KeyboardInterrupt:
            logger.debug("Received interrupt")
        
        finally:
            # Restore terminal settings
            if self.original_termios:
                try:
                    termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self.original_termios)
                except Exception:
                    pass
    
    def cleanup(self):
        """Clean up resources"""
        logger.debug("Cleaning up agent resources")
        
        # Kill shell process if still running
        if self.shell_pid:
            try:
                os.kill(self.shell_pid, signal.SIGTERM)
                os.waitpid(self.shell_pid, 0)
            except Exception as e:
                logger.debug(f"Failed to kill shell: {e}")
        
        # Close FDs
        if self.master_fd:
            try:
                os.close(self.master_fd)
            except Exception:
                pass
        
        if self.slave_fd:
            try:
                os.close(self.slave_fd)
            except Exception:
                pass

        if self.control_fd:
            self._close_control_fd()

    def run(self, rows: int = 24, cols: int = 80, cwd: Optional[str] = None, control_fd: Optional[int] = None):
        """Main entry point: create PTY, spawn shell, run I/O loop"""
        try:
            # Discover shell
            shell = self.discover_shell()
            logger.info(f"Using shell: {shell}")

            # Create PTY with proper flags
            self.create_pty()

            # Set initial size
            self.set_pty_size(rows, cols)

            # Configure optional control channel
            if control_fd is not None:
                self.set_control_fd(control_fd)

            # Spawn shell
            shell_pid = self.spawn_shell(shell, cwd)

            if shell_pid:
                self._send_status('ready', pid=shell_pid)

            # Close stderr to prevent log messages from appearing in terminal
            # unless in verbose/debug mode
            if logger.getEffectiveLevel() > logging.DEBUG:
                try:
                    # Redirect stderr to /dev/null
                    devnull = os.open(os.devnull, os.O_WRONLY)
                    os.dup2(devnull, sys.stderr.fileno())
                    os.close(devnull)
                except Exception:
                    pass
            
            # Run I/O loop (blocks until shell exits or connection closes)
            self.io_loop()
            
        except Exception as e:
            logger.error(f"Agent error: {e}", exc_info=True)
            # Send error to parent
            error_msg = json.dumps({'type': 'error', 'message': str(e)})
            sys.stderr.write(error_msg + '\n')
            sys.stderr.flush()
            return 1
        
        finally:
            self.cleanup()
        
        return 0


def handle_resize_signal(signum, frame):
    """Handle SIGWINCH for terminal resize"""
    # In a real implementation, we'd need to communicate resize events
    # from the UI to the agent, possibly via a control channel
    pass


def main():
    """Main entry point for the agent"""
    import argparse
    
    parser = argparse.ArgumentParser(description='SSHPilot PTY Agent')
    parser.add_argument('--rows', type=int, default=24, help='Terminal rows')
    parser.add_argument('--cols', type=int, default=80, help='Terminal columns')
    parser.add_argument('--cwd', type=str, default=None, help='Working directory')
    parser.add_argument('--verbose', action='store_true', help='Verbose logging')
    parser.add_argument('--control-fd', type=int, default=None, help='Control channel file descriptor')
    
    args = parser.parse_args()
    
    # Set up signal handlers
    signal.signal(signal.SIGWINCH, handle_resize_signal)
    
    # Create and run agent
    agent = PTYAgent()
    return agent.run(rows=args.rows, cols=args.cols, cwd=args.cwd, control_fd=args.control_fd)


if __name__ == '__main__':
    sys.exit(main())

