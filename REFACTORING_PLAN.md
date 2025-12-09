# SSH Connection Builder Refactoring Plan

## Overview
Unify all SSH connection paths to use `ssh_connection_builder.py` as the single source of truth for building SSH commands.

## Features to Support

### Terminal Connections (`terminal.py`)
1. **Native mode** - Minimal SSH command using host identifier
2. **Quick connect mode** - Use pre-built command from connection
3. **Password authentication** - sshpass with FIFO
4. **Key-based authentication** - askpass for passphrases
5. **Port forwarding**:
   - Dynamic (`-D`)
   - Local (`-L`)
   - Remote (`-R`)
6. **Remote commands** - With TTY allocation (`-t -t`)
7. **Local commands** - PermitLocalCommand
8. **X11 forwarding** (`-X`)
9. **Extra SSH config options** - From advanced tab
10. **Verbosity/debug** - Multiple `-v` flags, LogLevel
11. **Known hosts file** - UserKnownHostsFile
12. **Certificate files** - CertificateFile
13. **Key preparation** - AddKeysToAgent support
14. **Passphrase handling** - Saved passphrase detection
15. **ProxyCommand/ProxyJump**
16. **BatchMode**
17. **Connection timeout/attempts**
18. **Keepalive settings**
19. **StrictHostKeyChecking**
20. **Compression** (`-C`)
21. **NumberOfPasswordPrompts**
22. **PreferredAuthentications**
23. **PubkeyAuthentication**
24. **IdentitiesOnly**
25. **ExitOnForwardFailure**
26. **TERM and SHELL** environment variables
27. **Flatpak PATH** handling

### SCP Operations (`window.py`)
1. Password authentication with sshpass
2. Key-based authentication with askpass
3. Known hosts file
4. Extra SSH options
5. Key file and IdentitiesOnly
6. Recursive transfers (`-r`)
7. Port specification (`-P`)

### SFTP Connections (`file_manager_window.py`)
- Uses paramiko (Python library) - not SSH command line
- Can extract connection parameters from ssh_connection_builder

### Connection Manager (`connection_manager.py`)
1. Similar features to terminal.py
2. Native connect mode
3. Quick connect command support

## Implementation Plan

1. **Enhance `ssh_connection_builder.py`**:
   - Add support for all features listed above
   - Add ConnectionContext fields for advanced features
   - Update `build_ssh_connection()` to handle all cases

2. **Replace connection paths**:
   - terminal.py `_setup_ssh_terminal()`
   - window.py SCP operations
   - connection_manager.py SSH command building
   - sftp_utils.py (if applicable)

3. **Testing**:
   - Test all connection types
   - Test all authentication methods
   - Test port forwarding
   - Test native/quick connect modes

