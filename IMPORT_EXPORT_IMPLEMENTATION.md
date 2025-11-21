# Import/Export Configuration Implementation

## Overview

This implementation adds robust import/export functionality to sshPilot, allowing users to backup and restore their complete configuration including SSH settings, connections, groups, and application preferences.

## What is Backed Up

The export includes:

1. **SSH Configuration File**
   - Location depends on operation mode:
     - Default mode: `~/.ssh/config`
     - Isolated mode: `~/.config/sshpilot/ssh_config`
   - Complete SSH host configurations

2. **Application Configuration (config.json)**
   - Connection groups and hierarchy
   - Group colors
   - Connection metadata
   - Keyboard shortcuts
   - Terminal settings (themes, fonts, etc.)
   - UI preferences
   - SSH settings
   - File manager settings
   - Security settings

3. **Known Hosts (optional)**
   - Only included in isolated mode
   - Location: `~/.config/sshpilot/known_hosts`

## Export Format

Exports are saved as JSON files with the following structure:

```json
{
  "version": 1,
  "export_date": "2025-01-21T12:00:00",
  "platform": "linux/macos/flatpak",
  "config_version": 3,
  "isolated_mode": true/false,
  "ssh_config": "... SSH config file contents ...",
  "app_config": { ... config.json contents ... },
  "known_hosts": "... optional known_hosts contents ..."
}
```

## Import Modes

### Replace Mode (Default)
- Completely replaces all current configuration
- SSH config, app config, and known_hosts are overwritten
- Use when migrating to a new system or restoring from backup

### Merge Mode
- Intelligently merges imported configuration with existing
- **SSH Config**: Appends new host entries that don't exist
- **Groups**: Adds new groups (by name), preserves existing ones
- **Connection Metadata**: Only adds new connections, keeps existing
- **Shortcuts**: Keeps existing shortcuts, adds new ones
- Use when combining configurations or importing selected items

## Edge Cases Handled

### 1. Platform Differences
- Detects platform mismatch (Linux, macOS, Flatpak)
- Warns user but allows import to proceed
- Paths are automatically adjusted

### 2. Operation Mode Differences
- Detects isolated vs default mode mismatch
- Warns user but allows import to proceed
- Configuration is applied to current mode's location

### 3. Group Color Conflicts
- In merge mode, preserves existing group colors
- Only applies colors to newly created groups
- Prevents accidental color changes

### 4. Connection Conflicts
- In merge mode, preserves existing connections
- Only adds connections that don't exist
- Connection metadata is preserved

### 5. Validation
- Validates JSON structure before import
- Checks for required fields (version, app_config)
- Verifies backup version compatibility
- Provides clear error messages for invalid files

### 6. Automatic Backup
- Creates automatic backup before every import
- Stored in `~/.config/sshpilot/backups/`
- Named with timestamp: `auto_backup_YYYYMMDD_HHMMSS.json`
- Allows recovery if import goes wrong

### 7. Missing/Corrupted Files
- Gracefully handles missing SSH config
- Gracefully handles missing config.json
- Uses defaults when files don't exist
- Provides informative error messages

### 8. File Permissions
- Ensures proper permissions on SSH files (0600)
- Creates parent directories as needed
- Handles permission errors gracefully

## User Interface

### Menu Location
- Main menu → Import/Export submenu
  - Export Configuration
  - Import Configuration

### Export Flow
1. User selects "Export Configuration" from menu
2. File chooser dialog opens with suggested filename
3. Configuration is exported to selected location
4. Success/failure notification displayed

### Import Flow
1. User selects "Import Configuration" from menu
2. File chooser dialog opens (filtered for .json files)
3. User selects import file
4. Mode selection dialog appears:
   - Radio buttons for Replace or Merge mode
   - Descriptions of each mode
   - Warning about automatic backup
5. Import is performed with automatic backup
6. Success dialog with reload option:
   - "OK" - Dismiss dialog
   - "Restart Now" - Reload configuration immediately

## Implementation Files

### New Files
- `sshpilot/backup_manager.py` - Core import/export logic

### Modified Files
- `sshpilot/window.py` - Added dialog methods and menu integration
- `sshpilot/actions.py` - Added action handlers and registration

## Code Changes

### backup_manager.py (New)
- `BackupManager` class with methods:
  - `export_configuration()` - Export all configuration
  - `import_configuration()` - Import with replace or merge
  - `get_ssh_config_path()` - Get correct SSH config path
  - `get_known_hosts_path()` - Get known_hosts path if isolated
  - `_validate_import_data()` - Validate import file structure
  - `_import_replace()` - Replace mode implementation
  - `_import_merge()` - Merge mode implementation
  - `_merge_ssh_config()` - Smart SSH config merging
  - `_merge_app_config()` - Smart app config merging
  - `_merge_groups()` - Group conflict resolution
  - `_create_auto_backup()` - Automatic backup creation
  - `list_backups()` - List available backups

### window.py
- Added imports: `from datetime import datetime`
- Updated `create_menu()`: Added Import/Export submenu
- Added methods:
  - `show_export_dialog()` - Export configuration UI
  - `show_import_dialog()` - Import configuration UI
  - `_show_import_mode_dialog()` - Mode selection UI
  - `_perform_import()` - Import execution with reload

### actions.py
- Added action handlers:
  - `on_export_config_action()` - Export action handler
  - `on_import_config_action()` - Import action handler
- Updated `register_window_actions()`: Registered new actions

## Testing

The implementation has been:
- ✅ Syntax validated with py_compile
- ✅ Linter checked (no errors)
- ✅ Code reviewed for edge cases
- ✅ Error handling verified
- ✅ Platform compatibility checked

## Usage Examples

### Exporting Configuration
1. Open sshPilot
2. Click main menu (hamburger icon)
3. Hover over "Import/Export"
4. Click "Export Configuration"
5. Choose save location and filename
6. Click "Save"

### Importing Configuration (Replace)
1. Open sshPilot
2. Click main menu
3. Hover over "Import/Export"
4. Click "Import Configuration"
5. Select the exported .json file
6. Choose "Replace current configuration"
7. Click "Import"
8. Click "Restart Now" to reload (recommended)

### Importing Configuration (Merge)
1. Follow steps 1-4 above
2. Choose "Merge with current configuration"
3. Click "Import"
4. Click "Restart Now" to reload (recommended)

## Future Enhancements

Possible future improvements:
- Selective import (choose specific groups/connections)
- Import preview showing what will be added/changed
- Comparison view between current and import
- Scheduled automatic backups
- Cloud backup integration
- Import from other SSH managers (PuTTY, etc.)

## Notes for Developers

### Adding New Configuration Items
When adding new configuration fields:
1. Update `get_default_config()` in config.py
2. Ensure fields are included in config.json
3. BackupManager automatically includes all config.json data
4. No changes to backup_manager.py needed for new fields

### Modifying SSH Config Format
If SSH config handling changes:
- Update `_merge_ssh_config()` method
- Update `_extract_host_names()` if needed
- Consider backward compatibility with old exports

### Platform-Specific Handling
Platform detection uses:
- `is_flatpak()` for Flatpak detection
- `os.name` for platform detection
- `get_config_dir()` and `get_ssh_dir()` for paths

## Security Considerations

1. **File Permissions**
   - SSH config and known_hosts: 0600 (owner read/write only)
   - Ensures private keys and host keys remain secure

2. **Password Storage**
   - Passwords are stored via platform keyring (not in export)
   - Export does not include sensitive credentials
   - Only configuration structure is exported

3. **Backup Location**
   - Automatic backups stored in user's config directory
   - Protected by filesystem permissions
   - Consider excluding from public backups/sync

## Troubleshooting

### Import Fails
- Check JSON file validity
- Verify backup version compatibility
- Check file permissions
- Look for error in automatic backup

### Groups Not Appearing After Merge
- Groups with same name (case-insensitive) are considered duplicates
- Only new groups are added
- Check automatic backup to see previous state

### SSH Config Not Updated
- Verify operation mode (isolated vs default)
- Check SSH config file permissions
- Reload connections after import

### Configuration Not Taking Effect
- Use "Restart Now" option after import
- Or restart sshPilot manually
- Check logs for errors during import
