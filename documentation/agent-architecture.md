# SSHPilot Agent Architecture

## Overview

SSHPilot implements a **Ptyxis-inspired Agent-UI architecture** to provide fully functional host shell access with proper job control in Flatpak sandboxed environments.

This architecture resolves the common **"cannot set terminal process group / no job control"** error that occurs when trying to spawn shells directly from within a Flatpak sandbox using `flatpak-spawn --host`.

## The Problem

When running in a Flatpak sandbox, direct spawning of host shells using VTE's `spawn_async()` combined with `flatpak-spawn --host` leads to TTY/job control issues:

```
bash: cannot set terminal process group (-1): Inappropriate ioctl for device
bash: no job control in this shell
```

This occurs because:
1. The PTY is created inside the sandbox
2. The shell runs on the host via `flatpak-spawn`
3. The shell cannot properly claim the PTY as its controlling terminal
4. Job control operations (Ctrl+C, Ctrl+Z, background jobs) fail

## The Solution: Agent Architecture

Inspired by [Ptyxis](https://gitlab.gnome.org/chergert/ptyxis/-/tree/main/agent), SSHPilot now uses a two-process architecture:

```
┌─────────────────────────────────────┐
│   Flatpak Sandbox (UI Process)     │
│                                     │
│  ┌──────────────────────────────┐  │
│  │  SSHPilot UI (Python/GTK4)  │  │
│  │                              │  │
│  │  ┌────────────────────────┐ │  │
│  │  │   VTE Terminal Widget  │ │  │
│  │  │                        │ │  │
│  │  │   stdin/stdout/stderr  │ │  │
│  │  └────────┬───────────────┘ │  │
│  │           │                  │  │
│  └───────────┼──────────────────┘  │
│              │                      │
│              │ flatpak-spawn --host │
│              │ (auto-forwards I/O)  │
└──────────────┼──────────────────────┘
               │
               │ FD forwarding
               │
┌──────────────▼──────────────────────┐
│   Host System (Agent Process)      │
│                                     │
│  ┌──────────────────────────────┐  │
│  │  sshpilot-agent (Python)    │  │
│  │                              │  │
│  │  1. Discover user shell      │  │
│  │  2. Create PTY master/slave  │  │
│  │  3. Set O_NOCTTY flags       │  │
│  │  4. Fork & spawn shell       │  │
│  │  5. Shell claims TTY with    │  │
│  │     TIOCSCTTY (job control!) │  │
│  │  6. Relay I/O between        │  │
│  │     PTY master ↔ stdin/out   │  │
│  │                              │  │
│  └──────────────┬───────────────┘  │
│                 │                   │
│                 │ PTY master        │
│                 │                   │
│         ┌───────▼────────┐          │
│         │  /bin/bash -l  │          │
│         │                │          │
│         │  (Full job     │          │
│         │   control!)    │          │
│         └────────────────┘          │
└─────────────────────────────────────┘
```

## Architecture Components

### 1. `sshpilot-agent` (Host-side)

**File:** `sshpilot/sshpilot_agent.py`  
**Location:** Installed to `/app/bin/sshpilot-agent`  
**Runs on:** Host system (outside Flatpak sandbox)

**Responsibilities:**
- **Shell Discovery**: Finds the user's preferred shell via:
  - `SHELL` environment variable
  - `getent passwd $USER`
  - `pwd` module
  - Fallback to `/bin/bash`

- **PTY Creation**: Creates a master/slave PTY pair with proper flags:
  ```python
  master_fd, slave_fd = pty.openpty()
  
  # Critical: Set O_NOCTTY to prevent automatic controlling TTY
  flags = fcntl.fcntl(slave_fd, fcntl.F_GETFL)
  fcntl.fcntl(slave_fd, fcntl.F_SETFL, flags | os.O_NOCTTY)
  ```

- **Shell Spawning**: Forks and spawns the shell with proper session setup:
  ```python
  os.setsid()  # Create new session
  fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)  # Claim controlling TTY
  # Redirect stdin/stdout/stderr to PTY slave
  # Execute shell
  ```

- **I/O Relay**: Runs a select-based I/O loop that forwards:
  - `stdin` (from VTE) → PTY master (to shell)
  - PTY master (from shell) → `stdout` (to VTE)

### 2. `agent_client.py` (UI-side)

**File:** `sshpilot/agent_client.py`  
**Runs in:** Flatpak sandbox

**Responsibilities:**
- Locate the agent binary
- Find Python interpreter on host
- Build `flatpak-spawn` command with FD forwarding
- Launch agent and verify readiness
- Handle errors and fallback scenarios

### 3. Modified `terminal.py`

**Changes in:** `setup_local_shell()`

The local shell setup now follows this flow:

```python
def setup_local_shell(self):
    if is_flatpak() and self._try_agent_based_shell():
        # Use agent (with job control)
        return
    else:
        # Fall back to direct spawn (legacy, no job control in Flatpak)
        self._setup_local_shell_direct()
```

## Critical Technical Details

### PTY Flag Handling

The key to fixing job control is proper PTY flag management:

1. **O_NOCTTY**: Set on the PTY slave to prevent automatic controlling terminal allocation
   - Without this, the first open of the slave would make it the controlling terminal
   - This interferes with the shell's ability to properly claim the TTY

2. **TIOCSCTTY**: Called by the shell process after `setsid()`
   - Makes the PTY the controlling terminal for the new session
   - Enables job control operations (SIGTSTP, SIGTTOU, etc.)

### Session Management

```python
# In agent, child process:
os.setsid()                              # Create new session, become session leader
fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)  # Claim PTY as controlling terminal
```

This sequence ensures:
- The shell becomes the session leader
- The PTY becomes the controlling terminal for the session
- Job control signals work correctly

### File Descriptor Forwarding

The agent communicates with the VTE terminal via standard streams:

```bash
flatpak-spawn --host /app/bin/sshpilot-agent --rows 24 --cols 80
```

Note: `flatpak-spawn --host` automatically forwards stdin/stdout/stderr between the sandbox and host process, so no explicit `--forward-fd` flags are needed.

## Testing

### Test Agent Standalone

```bash
# Run automated tests
python3 scripts/test_agent.py

# Run interactive test
python3 scripts/test_agent.py --interactive
```

### Test in SSHPilot

1. Build and run SSHPilot in Flatpak
2. Open a Local Terminal (Ctrl+Shift+T or menu)
3. Verify shell prompt appears
4. Test job control:
   ```bash
   # Test Ctrl+C
   sleep 100
   # Press Ctrl+C - should interrupt
   
   # Test background jobs
   sleep 100 &
   jobs
   fg
   
   # Test Ctrl+Z
   sleep 100
   # Press Ctrl+Z - should suspend
   bg
   jobs
   ```

### Expected Behavior

**With Agent (Success):**
```bash
$ sleep 100 &
[1] 12345
$ jobs
[1]+  Running                 sleep 100 &
$ fg
sleep 100
^C
```

**Without Agent (Failure):**
```bash
bash: cannot set terminal process group (-1): Inappropriate ioctl for device
bash: no job control in this shell
$ sleep 100 &
$ # Job control not working
```

## Fallback Behavior

The implementation gracefully falls back to the old approach if:

1. Not running in Flatpak (agent not needed)
2. Agent binary not found
3. Agent fails to launch
4. `flatpak-spawn` not available

This ensures SSHPilot continues to work even if the agent setup fails.

## Performance Considerations

**Overhead:** The agent adds minimal overhead:
- One additional process on the host
- One I/O relay loop (select-based, efficient)
- No additional memory allocations per keystroke

**Latency:** Negligible (< 1ms) as the relay is in-process and uses efficient I/O.

## Debugging

### Enable Verbose Logging

```bash
# Run SSHPilot with verbose mode
python3 run.py --verbose
```

### Agent Logs

The agent logs to stderr with prefix `[sshpilot-agent]`:

```
[sshpilot-agent] INFO: Using shell: /bin/bash
[sshpilot-agent] DEBUG: Created PTY: master_fd=3, slave_fd=4
[sshpilot-agent] INFO: Spawned shell: /bin/bash (PID: 12345)
[sshpilot-agent] DEBUG: Starting I/O loop
```

### Common Issues

**Agent not found:**
- Verify `/app/bin/sshpilot-agent` exists in Flatpak
- Check Python paths in launcher script

**flatpak-spawn not working:**
- Ensure `--talk-name=org.freedesktop.Flatpak` in manifest
- Verify Flatpak version supports `--forward-fd`

**Shell still has no job control:**
- Check agent logs for errors
- Verify PTY flags are set correctly
- Test agent standalone first

## References

- [Ptyxis Agent Architecture](https://gitlab.gnome.org/chergert/ptyxis/-/tree/main/agent)
- [POSIX Terminal Control](https://www.gnu.org/software/libc/manual/html_node/Controlling-Terminal.html)
- [PTY Programming](https://man7.org/linux/man-pages/man7/pty.7.html)

## Future Enhancements

Potential improvements to the agent architecture:

1. **Window Resize Handling**: Implement proper SIGWINCH forwarding
2. **D-Bus Communication**: Use D-Bus instead of FD forwarding for more robust IPC
3. **Multiple Shells**: Support spawning multiple shells from one agent
4. **SSH Integration**: Extend agent to handle SSH connections with job control
5. **Performance Monitoring**: Add metrics for I/O throughput and latency

