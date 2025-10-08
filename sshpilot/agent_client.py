"""
Agent Client - UI-side communication with sshpilot-agent

This module handles launching the agent on the host and managing
communication between the VTE terminal and the agent.
"""

import os
import sys
import json
import logging
import subprocess
import shutil
import shlex
from dataclasses import dataclass
from typing import Optional, Tuple, List
from pathlib import Path

from .platform_utils import is_flatpak

logger = logging.getLogger(__name__)


@dataclass
class AgentLaunchCommand:
    """Return value for agent launch configuration."""

    command: List[str]
    control_reader_fd: int
    control_writer_fd: int


class AgentClient:
    """Client for communicating with the sshpilot-agent on the host"""
    
    def __init__(self):
        self.agent_path: Optional[str] = None
        self.python_path: Optional[str] = None
        
    def find_agent(self) -> Tuple[Optional[str], Optional[str]]:
        """
        Find the agent script and Python interpreter.
        
        Returns:
            Tuple of (python_path, agent_path) or (None, None) if not found
        """
        # Look for agent in multiple locations
        possible_locations = [
            '/app/bin/sshpilot-agent',  # Flatpak install location
            '/usr/local/bin/sshpilot-agent',  # System-wide install
            '/usr/bin/sshpilot-agent',  # System install
        ]
        
        # Also check relative to this module
        try:
            module_dir = Path(__file__).parent
            local_agent = module_dir / 'sshpilot_agent.py'
            if local_agent.exists():
                possible_locations.insert(0, str(local_agent))
        except Exception as e:
            logger.debug(f"Could not determine module directory: {e}")
        
        # Find Python interpreter on host
        python_path = None
        if is_flatpak():
            # In Flatpak, we need to find Python on the host
            flatpak_spawn = shutil.which('flatpak-spawn')
            if flatpak_spawn:
                # Try python3 on host
                try:
                    result = subprocess.run(
                        [flatpak_spawn, '--host', 'which', 'python3'],
                        capture_output=True,
                        text=True,
                        check=True
                    )
                    python_path = result.stdout.strip()
                    logger.debug(f"Found host Python: {python_path}")
                except subprocess.CalledProcessError:
                    logger.warning("Could not find python3 on host")
        else:
            # Not in Flatpak, use local Python
            python_path = sys.executable
        
        # Find agent script
        for agent_path in possible_locations:
            if os.path.isfile(agent_path):
                logger.debug(f"Found agent at: {agent_path}")
                self.agent_path = agent_path
                self.python_path = python_path
                return python_path, agent_path
        
        logger.warning("Could not find sshpilot-agent")
        return None, None
    
    def build_agent_command(
        self,
        rows: int = 24,
        cols: int = 80,
        cwd: Optional[str] = None,
        verbose: bool = False
    ) -> Optional[AgentLaunchCommand]:
        """
        Build the command to launch the agent.
        
        Args:
            rows: Terminal rows
            cols: Terminal columns
            cwd: Working directory
            verbose: Enable verbose logging
            
        Returns:
            AgentLaunchCommand with process command and control pipe fds, or None if
            the agent could not be located.
        """

        control_reader, control_writer = os.pipe()

        try:
            os.set_inheritable(control_reader, True)
        except OSError as exc:
            logger.error("Failed to mark control FD inheritable: %s", exc)
            os.close(control_reader)
            os.close(control_writer)
            return None

        # In Flatpak, use embedded agent code approach
        if is_flatpak():
            command = self._build_flatpak_agent_command(
                rows, cols, cwd, verbose, control_reader
            )
            if not command:
                os.close(control_reader)
                os.close(control_writer)
                return None

            return AgentLaunchCommand(command, control_reader, control_writer)

        # Not in Flatpak, run directly
        python_path, agent_path = self.find_agent()

        if not python_path or not agent_path:
            os.close(control_reader)
            os.close(control_writer)
            return None

        # Build agent command
        agent_cmd = [
            python_path,
            agent_path,
            '--rows', str(rows),
            '--cols', str(cols),
            '--control-fd', str(control_reader),
        ]

        if cwd:
            agent_cmd.extend(['--cwd', cwd])

        if verbose:
            agent_cmd.append('--verbose')

        return AgentLaunchCommand(agent_cmd, control_reader, control_writer)
    
    def _build_flatpak_agent_command(
        self,
        rows: int = 24,
        cols: int = 80,
        cwd: Optional[str] = None,
        verbose: bool = False,
        control_fd: Optional[int] = None
    ) -> Optional[List[str]]:
        """
        Build agent command for Flatpak environment.
        
        In Flatpak, /app paths don't exist on the host, so we encode the
        agent code in base64 and pass it via environment variable.
        """
        import base64
        
        flatpak_spawn = shutil.which('flatpak-spawn')
        if not flatpak_spawn:
            logger.error("flatpak-spawn not found")
            return None
        
        # Find agent script in sandbox and discover host python if available
        python_path, agent_path = self.find_agent()
        if not agent_path:
            logger.error("Agent script not found")
            return None

        python_exec = python_path or 'python3'
        
        # Read and encode agent code
        try:
            with open(agent_path, 'r') as f:
                agent_code = f.read()
            agent_b64 = base64.b64encode(agent_code.encode('utf-8')).decode('ascii')
        except Exception as e:
            logger.error(f"Failed to read/encode agent: {e}")
            return None
        
        # Build arguments for the agent
        agent_args = [
            '--rows', str(rows),
            '--cols', str(cols),
        ]

        if control_fd is not None:
            agent_args.extend(['--control-fd', str(control_fd)])
        
        if cwd:
            agent_args.extend(['--cwd', cwd])
        
        if verbose:
            agent_args.append('--verbose')

        python_code = (
            "import base64,os,sys;"
            "exec(base64.b64decode(os.environ['SSHPILOT_AGENT']).decode('utf-8'))"
        )

        wrapper_args = [
            python_exec,
            '-c',
            python_code,
            *agent_args,
        ]

        # Create bash script that decodes and runs the agent
        wrapper_script = shlex.join(wrapper_args)
        
        # Store agent code in environment variable format
        self._agent_env = {'SSHPILOT_AGENT': agent_b64}
        
        # Execute via flatpak-spawn
        cmd = [
            flatpak_spawn,
            f'--forward-fd={control_fd}' if control_fd is not None else None,
            '--host',
            f'--env=SSHPILOT_AGENT={agent_b64}',
            'bash',
            '-c',
            wrapper_script
        ]

        # Remove any None entries that may have been inserted when control_fd is None
        cmd = [arg for arg in cmd if arg is not None]

        return cmd
    
    def launch_agent(
        self,
        rows: int = 24,
        cols: int = 80,
        cwd: Optional[str] = None,
        verbose: bool = False
    ) -> Optional[Tuple[subprocess.Popen, AgentLaunchCommand]]:
        """
        Launch the agent process.

        Returns:
            Tuple of the spawned Popen object and the launch configuration with
            control pipe descriptors, or None if launching failed.
        """
        launch = self.build_agent_command(rows, cols, cwd, verbose)

        if not launch:
            logger.error("Could not build agent command")
            return None

        try:
            logger.info(f"Launching agent: {' '.join(launch.command)}")

            # Launch agent with pipes for I/O
            process = subprocess.Popen(
                launch.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True  # Important: agent becomes session leader
            )

            logger.debug(f"Agent launched with PID: {process.pid}")
            return process, launch

        except Exception as e:
            logger.error(f"Failed to launch agent: {e}")
            try:
                os.close(launch.control_reader_fd)
            except OSError:
                pass
            try:
                os.close(launch.control_writer_fd)
            except OSError:
                pass
            launch.control_reader_fd = -1
            launch.control_writer_fd = -1
            return None
        finally:
            try:
                os.close(launch.control_reader_fd)
            except OSError:
                pass
            else:
                launch.control_reader_fd = -1
    
    def get_agent_fds(self, process: subprocess.Popen) -> Tuple[int, int, int]:
        """
        Get file descriptors for agent communication.
        
        Args:
            process: The agent Popen object
            
        Returns:
            Tuple of (stdin_fd, stdout_fd, stderr_fd)
        """
        if not process or not process.stdin or not process.stdout or not process.stderr:
            raise ValueError("Invalid agent process")
        
        stdin_fd = process.stdin.fileno()
        stdout_fd = process.stdout.fileno()
        stderr_fd = process.stderr.fileno()
        
        return stdin_fd, stdout_fd, stderr_fd
    
    def wait_for_ready(self, process: subprocess.Popen, timeout: float = 5.0) -> bool:
        """
        Wait for agent to signal ready.
        
        Args:
            process: The agent Popen object
            timeout: Timeout in seconds
            
        Returns:
            True if agent is ready, False otherwise
        """
        import select
        import time
        
        if not process or not process.stderr:
            return False
        
        start_time = time.time()
        
        try:
            while time.time() - start_time < timeout:
                # Check if process is still alive
                if process.poll() is not None:
                    logger.error("Agent process exited unexpectedly")
                    return False
                
                # Check for ready message on stderr
                readable, _, _ = select.select([process.stderr], [], [], 0.1)
                
                if process.stderr in readable:
                    line = process.stderr.readline()
                    if line:
                        try:
                            msg = json.loads(line)
                            if msg.get('type') == 'ready':
                                logger.info(f"Agent ready (shell PID: {msg.get('pid')})")
                                return True
                            elif msg.get('type') == 'error':
                                logger.error(f"Agent error: {msg.get('message')}")
                                return False
                        except json.JSONDecodeError:
                            logger.debug(f"Non-JSON stderr from agent: {line}")
            
            logger.warning("Timeout waiting for agent ready signal")
            return False
            
        except Exception as e:
            logger.error(f"Error waiting for agent ready: {e}")
            return False


def create_agent_launcher_script():
    """
    Create a standalone launcher script for the agent.
    
    This is used when installing the agent to /app/bin in Flatpak.
    """
    script = """#!/usr/bin/env python3
import sys
import os

# Add sshpilot module to path
sys.path.insert(0, '/app/lib/python{}.{}/site-packages'.format(
    sys.version_info.major, sys.version_info.minor
))

from sshpilot.sshpilot_agent import main

if __name__ == '__main__':
    sys.exit(main())
"""
    return script


if __name__ == '__main__':
    # Test agent discovery
    logging.basicConfig(level=logging.DEBUG)
    
    client = AgentClient()
    python_path, agent_path = client.find_agent()
    
    if python_path and agent_path:
        print(f"Python: {python_path}")
        print(f"Agent: {agent_path}")
        
        launch = client.build_agent_command(rows=24, cols=80, verbose=True)
        if launch:
            print(f"Command: {' '.join(launch.command)}")
            print(f"Control reader FD: {launch.control_reader_fd}")
    else:
        print("Agent not found")

