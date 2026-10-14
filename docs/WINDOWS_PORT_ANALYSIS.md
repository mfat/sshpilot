# Windows Port Analysis

## Overview
This document summarizes the investigation into porting SSH Pilot to Windows and the blockers that need to be addressed.

## Current State
SSH Pilot is a GTK4/libadwaita-based SSH connection manager built for Linux and macOS. It uses:
- **PyGObject** for GTK4 bindings
- **VTE (GTK4)** for terminal emulation
- **WebKitGTK** (optional, Linux-only) for xterm.js backend
- **PyInstaller** for macOS bundling
- **Native OpenSSH** for SSH operations

## Critical Windows Blockers

### 1. **Terminal/PTY Handling** (HIGH PRIORITY)
**Problem**: The app uses VTE (Virtual Terminal Emulator) which is a GTK widget for Linux/Unix terminals.
- `src/sshpilot/xterm_pty_bridge.py` uses `Vte.Pty` for PTY creation
- `gi.require_version("Vte", "3.91")` - VTE is not available on Windows
- PTY operations use Unix-specific APIs (`os.set_blocking`, Unix fd handling)

**Solution Options**:
1. **Use xterm.js backend exclusively** on Windows (already optional)
   - Requires WebKit or alternative web view (e.g., webview2 on Windows)
   - May need to implement Windows pseudo-console (ConPTY) support
2. **Use Windows ConPTY** API (Windows 10 1809+)
   - Python `winpty` or direct ConPTY API calls
   - Would need platform-specific backend implementation

**Recommendation**: Use xterm.js with a Windows-compatible web view (webview2 via pywebview or similar).

### 2. **sshpass Dependency** (HIGH PRIORITY)
**Problem**: The app uses `sshpass` for password-based SSH authentication, which is a Unix utility not available on Windows.
- Used in multiple files:
  - `src/sshpilot/platform_utils.py` (get_sshpass_path)
  - `src/sshpilot/ssh_connection_builder.py`
  - `src/sshpilot/xterm_pty_bridge.py`

**Solution**:
- OpenSSH for Windows supports `SSH_ASKPASS` environment variable
- Remove `sshpass` dependency and use `SSH_ASKPASS` + `SSH_ASKPASS_REQUIRE=force` on Windows
- The app already has askpass infrastructure (`src/sshpilot/askpass_utils.py`)

**Recommendation**: Refactor to use SSH_ASKPASS exclusively (works on all platforms).

### 3. **Path Handling** (MEDIUM PRIORITY)
**Problem**: Unix-style path assumptions throughout the codebase.
- Many uses of `os.path.join`, but should verify proper usage
- SSH config location: `~/.ssh/config` (Windows: `%USERPROFILE%\.ssh\config`)
- GLib's `get_user_config_dir()` should handle XDG on Linux, but Windows uses different conventions

**Current Approach**:
- Uses `GLib.get_user_config_dir()`, `GLib.get_user_data_dir()`, `GLib.get_home_dir()`
- These should work cross-platform with GLib

**Action Required**:
- Audit all hardcoded paths
- Verify GLib path functions work correctly on Windows
- Test config file discovery

### 4. **Keyring/Credential Storage** (MEDIUM PRIORITY)
**Problem**: The app uses `keyring` library for credential storage.
- Python `keyring` library should support Windows Credential Manager
- Needs testing to ensure Windows backend works

**Status**: Likely works (keyring supports Windows), but needs verification.

### 5. **Unix-Specific System Calls** (LOW-MEDIUM PRIORITY)
**Problem**: Several Unix-specific imports found:
- `import pwd` (user database) - in `src/sshpilot/file_manager/properties_dialog.py`, `src/sshpilot/terminal.py`
- `import grp` (group database) - same files
- `import fcntl` (file control) - `src/sshpilot/platform/locking.py`, `src/sshpilot/daemon/pty_runner.py`
- `import termios` - terminal I/O control
- `os.fork()` - process forking (daemon)

**Solution**:
- Wrap Unix-specific imports in try/except or platform checks
- Provide Windows alternatives where needed (e.g., Windows file locking APIs)
- Daemon may need different implementation on Windows (Windows services vs fork)

### 6. **GTK4/GLib on Windows** (CRITICAL DEPENDENCY)
**Problem**: GTK4 runtime availability on Windows.

**Status**:
- GTK4 is available on Windows via MSYS2
- GLib, libadwaita, and GTK4 are all portable
- PyGObject can be built for Windows

**Solution**:
- Bundle GTK4 runtime with PyInstaller on Windows (similar to macOS approach)
- Use MSYS2 for build environment
- Alternative: gvsbuild for building GTK stack on Windows

### 7. **GtkSourceView** (MEDIUM PRIORITY)
**Problem**: Used for SSH config syntax highlighting.
- Should be available on Windows with GTK4 stack
- Needs to be bundled with PyInstaller

**Status**: Likely portable, needs testing.

### 8. **Flatpak-Specific Code** (LOW PRIORITY)
**Problem**: Several Flatpak-specific code paths exist.
- `is_flatpak()` checks in `platform_utils.py`
- `flatpak-spawn --host` for running commands

**Solution**: These paths are already conditional and won't execute on Windows.

### 9. **Process Management** (MEDIUM PRIORITY)
**Problem**: Daemon uses Unix process management.
- `src/sshpilot/daemon/pty_runner.py` uses PTY and fork
- Process spawning assumptions

**Solution**:
- May need Windows-specific daemon implementation
- Or run daemon in-process on Windows (simpler)

### 10. **External Dependencies** (LOW-MEDIUM PRIORITY)
**External tools assumed to be available**:
- `ssh`, `scp`, `sftp` - OpenSSH for Windows provides these
- `ssh-agent` - available on Windows
- `ssh-copy-id` - may need Windows port or alternative

**Solution**: Document requirement for OpenSSH for Windows (built-in on Windows 10+).

## Recommended Approach

### Phase 1: Core Portability
1. **Add platform detection helpers** to `platform_utils.py`
   - Enhance `is_windows()` usage
   - Create platform-specific code paths

2. **Remove sshpass dependency**
   - Use SSH_ASKPASS on all platforms
   - Already mostly implemented

3. **Wrap Unix-specific imports**
   - Guard `pwd`, `grp`, `fcntl`, `termios` imports
   - Provide Windows alternatives or graceful degradation

4. **Fix path handling**
   - Audit hardcoded paths
   - Ensure GLib functions are used consistently

### Phase 2: Terminal Backend
1. **Implement Windows terminal backend**
   - Option A: xterm.js + webview2 (pywebview)
   - Option B: ConPTY + custom widget
   - Recommend: Option A for consistency with macOS xterm.js experience

2. **Make VTE optional**
   - Conditional import of VTE
   - Fall back to xterm.js backend on Windows

### Phase 3: Build and Package
1. **Create PyInstaller spec for Windows**
   - Based on macOS spec (`packaging/pyinstaller/sshpilot.spec`)
   - Bundle GTK4 runtime from MSYS2
   - Bundle Python dependencies

2. **GitHub Actions CI**
   - Add Windows build workflow
   - Use `windows-latest` runner
   - Build installer (NSIS or WiX) or portable zip

3. **Testing**
   - Test on Windows 10 and 11
   - Verify OpenSSH integration
   - Test credential storage

### Phase 4: Documentation
1. Update README with Windows installation instructions
2. Document Windows-specific limitations
3. Add Windows build instructions for developers

## Known Limitations (Expected)

1. **WebKitGTK not available** - xterm.js backend won't work initially
   - Need webview2 integration or similar
2. **ssh-copy-id** - may need workaround (manual key copying instructions)
3. **File permissions** - Unix permissions UI won't translate perfectly
4. **Some SFTP features** - if they rely on Unix-specific metadata

## Implementation Priority

**Must Have** (blocks Windows build):
1. VTE optional / Windows terminal backend
2. sshpass removal
3. Unix-specific import guards
4. PyInstaller Windows spec

**Should Have** (for good UX):
1. Windows keyring testing
2. Path handling audit
3. Proper error messages for unsupported features
4. Windows installer

**Nice to Have** (future improvements):
1. Native Windows look-and-feel options
2. Windows-specific features (e.g., PuTTY session import)
3. Windows Terminal integration

## Estimated Effort

- **Core portability fixes**: 2-3 days
- **Terminal backend**: 3-5 days
- **PyInstaller packaging**: 2-3 days
- **CI/CD setup**: 1-2 days
- **Testing and fixes**: 2-3 days
- **Documentation**: 1 day

**Total**: ~2-3 weeks for a working Windows build with basic functionality.

## Open Questions

1. Should we support Windows 10 only (ConPTY available) or Windows 8.1+?
2. Do we need a full installer or is a portable zip sufficient?
3. Should we bundle OpenSSH or require user installation?
4. What level of feature parity with Linux/macOS is required?
