"""SSH process lifecycle management.

Extracted verbatim from terminal.py: a GTK-free singleton that tracks spawned
SSH processes and their terminals and terminates them (process-group aware) on
shutdown. Kept separate from the VTE terminal widget so this pure
process/subprocess logic carries no GTK dependency and is independently testable.

terminal.py re-exports ``SSHProcessManager`` and ``process_manager`` for
backwards compatibility (existing ``from .terminal import ...`` callers).
"""

import os
import signal
import threading
import time
import weakref
import logging

logger = logging.getLogger(__name__)


class SSHProcessManager:
    """Manages SSH processes and ensures proper cleanup"""
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.processes = {}
            cls._instance.terminals = weakref.WeakSet()
            cls._instance.lock = threading.Lock()
            cls._instance.cleanup_thread = None
            cls._instance._start_cleanup_thread()
        return cls._instance

    def _start_cleanup_thread(self):
        """Start background cleanup thread"""
        # Disable automatic cleanup thread to prevent race conditions
        # Manual cleanup will happen on app shutdown via cleanup_all()
        logger.debug("Automatic SSH cleanup thread disabled to prevent race conditions")

    def _cleanup_loop(self):
        """Background cleanup loop"""
        while True:
            try:
                time.sleep(60)  # Increased from 30s to 60s to reduce interference
                self._cleanup_orphaned_processes()
            except Exception as e:
                logger.error(f"Error in cleanup loop: {e}")

    def _cleanup_orphaned_processes(self):
        """Clean up processes not tracked by active terminals"""
        with self.lock:
            active_pids = set()
            for terminal in list(self.terminals):
                try:
                    # Use stored PID instead of calling _get_terminal_pid() which can hang
                    pid = getattr(terminal, 'process_pid', None)
                    if pid:
                        active_pids.add(pid)
                        logger.debug(f"Terminal {id(terminal)} has active PID {pid}")
                except Exception as e:
                    logger.debug(f"Error getting stored PID from terminal: {e}")

            # Only clean up processes that are definitely orphaned AND old enough
            import time
            current_time = time.time()
            orphaned_pids = []

            for pid in list(self.processes.keys()):
                if pid not in active_pids:
                    # Check if process is old enough to be considered orphaned (10+ minutes)
                    process_info = self.processes.get(pid, {})
                    start_time = process_info.get('start_time')
                    if start_time and hasattr(start_time, 'timestamp'):
                        process_age = current_time - start_time.timestamp()
                        if process_age < 600:  # Less than 10 minutes old - be very conservative
                            logger.debug(f"Process {pid} is only {process_age:.1f}s old, skipping cleanup")
                            continue
                    else:
                        # If we don't have start time info, assume it's recent and skip cleanup
                        logger.debug(f"Process {pid} has no start_time info, skipping cleanup")
                        continue

                    # Double-check: make sure the process actually exists before trying to kill it
                    try:
                        os.kill(pid, 0)  # Test if process exists
                        orphaned_pids.append(pid)
                        logger.debug(f"Found orphaned process {pid} (age: {process_age:.1f}s)")
                    except ProcessLookupError:
                        # Process already gone, just remove from tracking
                        logger.debug(f"Process {pid} already gone, removing from tracking")
                        if pid in self.processes:
                            del self.processes[pid]
                    except Exception as e:
                        logger.debug(f"Error checking process {pid}: {e}")

            # Clean up confirmed orphaned processes
            for pid in orphaned_pids:
                logger.info(f"Cleaning up orphaned process {pid}")
                self._terminate_process_by_pid(pid)

    def _terminate_process_by_pid(self, pid):
        """Terminate a process by PID"""
        try:
            # Always try process group first
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGTERM)

            # Wait with shorter timeout for faster cleanup
            for _ in range(3):  # 0.3 seconds max (reduced from 1 second)
                try:
                    os.killpg(pgid, 0)
                    time.sleep(0.1)
                except ProcessLookupError:
                    break
            else:
                # Force kill if still alive
                os.killpg(pgid, signal.SIGKILL)

            # Do NOT waitpid() here: the terminal child is spawned by VTE, which
            # owns a GLib child-watch source and reaps it via waitid(). Reaping
            # it ourselves makes GLib's waitid() fail with ECHILD and emit a
            # GLib-WARNING (fatal under G_DEBUG=fatal-warnings). VTE reaps it.
            return True
        except Exception:
            return False

    def register_terminal(self, terminal):
        """Register a terminal for tracking"""
        self.terminals.add(terminal)
        logger.debug(f"Registered terminal {id(terminal)}")

    def cleanup_all(self):
        """Clean up all managed processes"""
        import signal

        def timeout_handler(signum, frame):
            logger.warning("Cleanup timeout - forcing exit")
            os._exit(1)

        # Set 5-second timeout for entire cleanup
        signal.signal(signal.SIGALRM, timeout_handler)
        signal.alarm(5)

        try:
            logger.info("Cleaning up all SSH processes...")

            # First, mark all terminals as quitting to suppress signal handlers
            for terminal in list(self.terminals):
                terminal._is_quitting = True

            with self.lock:
                # Atomically extract and clear all processes
                processes_to_clean = dict(self.processes)
                self.processes.clear()

            # Clean up processes without holding the lock
            for pid, info in processes_to_clean.items():
                logger.debug(f"Cleaning up process {pid} (command: {info.get('command', 'unknown')})")
                self._terminate_process_by_pid(pid)

            # Clean up terminals separately
            for terminal in list(self.terminals):
                try:
                    if hasattr(terminal, 'disconnect') and hasattr(terminal, 'is_connected') and terminal.is_connected:
                        logger.debug(f"Disconnecting terminal {id(terminal)}")
                        terminal.disconnect()
                except Exception as e:
                    logger.error(f"Error cleaning up terminal {id(terminal)}: {e}")

            # Clear terminal references
            self.terminals.clear()

            logger.info("SSH process cleanup completed")
        finally:
            signal.alarm(0)  # Cancel timeout


# Global process manager instance
process_manager = SSHProcessManager()
