"""
Backup and Restore Manager for sshPilot
Handles import/export of SSH and application configuration
"""

import json
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List

from .platform_utils import get_config_dir, get_ssh_dir, is_flatpak
from .config import Config, CONFIG_VERSION

logger = logging.getLogger(__name__)

BACKUP_VERSION = 1


class BackupManager:
    """Manages configuration backup and restore operations"""

    def __init__(self, config: Config, connection_manager=None):
        self.config = config
        self.connection_manager = connection_manager
        self.backup_dir = Path(get_config_dir()) / 'backups'
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def get_ssh_config_path(self) -> str:
        """Get the current SSH config path based on mode"""
        if self.connection_manager:
            return getattr(self.connection_manager, 'ssh_config_path', '')
        
        # Fallback: determine from config
        use_isolated = self.config.get_setting('ssh.use_isolated_config', False)
        if use_isolated:
            return str(Path(get_config_dir()) / 'ssh_config')
        else:
            return str(Path(get_ssh_dir()) / 'config')

    def get_known_hosts_path(self) -> Optional[str]:
        """Get the known_hosts path if in isolated mode"""
        if self.connection_manager:
            isolated = getattr(self.connection_manager, 'isolated_mode', False)
            if isolated:
                return getattr(self.connection_manager, 'known_hosts_path', None)
        
        use_isolated = self.config.get_setting('ssh.use_isolated_config', False)
        if use_isolated:
            return str(Path(get_config_dir()) / 'known_hosts')
        return None

    def export_configuration(self, export_path: str) -> Tuple[bool, Optional[str]]:
        """
        Export all configuration to a JSON file
        
        Args:
            export_path: Path where to save the export file
            
        Returns:
            Tuple of (success, error_message)
        """
        try:
            export_data = {
                'version': BACKUP_VERSION,
                'export_date': datetime.now().isoformat(),
                'platform': 'flatpak' if is_flatpak() else os.name,
                'config_version': CONFIG_VERSION,
            }

            # Export SSH configuration mode
            use_isolated = self.config.get_setting('ssh.use_isolated_config', False)
            export_data['isolated_mode'] = bool(use_isolated)

            # Export SSH config file
            ssh_config_path = self.get_ssh_config_path()
            if ssh_config_path and os.path.exists(ssh_config_path):
                try:
                    with open(ssh_config_path, 'r', encoding='utf-8') as f:
                        export_data['ssh_config'] = f.read()
                    logger.info(f"Exported SSH config from {ssh_config_path}")
                except Exception as e:
                    logger.warning(f"Could not read SSH config: {e}")
                    export_data['ssh_config'] = ''
            else:
                export_data['ssh_config'] = ''
                logger.warning(f"SSH config not found at {ssh_config_path}")

            # Export known_hosts if in isolated mode
            known_hosts_path = self.get_known_hosts_path()
            if known_hosts_path and os.path.exists(known_hosts_path):
                try:
                    with open(known_hosts_path, 'r', encoding='utf-8') as f:
                        export_data['known_hosts'] = f.read()
                    logger.info(f"Exported known_hosts from {known_hosts_path}")
                except Exception as e:
                    logger.warning(f"Could not read known_hosts: {e}")
                    export_data['known_hosts'] = None
            else:
                export_data['known_hosts'] = None

            # Export app configuration
            config_file = Path(get_config_dir()) / 'config.json'
            if config_file.exists():
                try:
                    with open(config_file, 'r', encoding='utf-8') as f:
                        export_data['app_config'] = json.load(f)
                    logger.info(f"Exported app config from {config_file}")
                except Exception as e:
                    logger.warning(f"Could not read app config: {e}")
                    export_data['app_config'] = self.config.get_default_config()
            else:
                export_data['app_config'] = self.config.get_default_config()
                logger.warning("App config not found, using defaults")

            # Write export file
            export_path = os.path.expanduser(export_path)
            with open(export_path, 'w', encoding='utf-8') as f:
                json.dump(export_data, f, indent=2)

            logger.info(f"Configuration exported successfully to {export_path}")
            return True, None

        except Exception as e:
            error_msg = f"Failed to export configuration: {e}"
            logger.error(error_msg)
            return False, error_msg

    def import_configuration(
        self, 
        import_path: str, 
        mode: str = 'replace',
        create_backup: bool = True
    ) -> Tuple[bool, Optional[str]]:
        """
        Import configuration from a JSON file
        
        Args:
            import_path: Path to the import file
            mode: 'replace' or 'merge'
            create_backup: Whether to create a backup before importing
            
        Returns:
            Tuple of (success, error_message)
        """
        try:
            # Validate import file
            import_path = os.path.expanduser(import_path)
            if not os.path.exists(import_path):
                return False, f"Import file not found: {import_path}"

            # Load import data
            try:
                with open(import_path, 'r', encoding='utf-8') as f:
                    import_data = json.load(f)
            except json.JSONDecodeError as e:
                return False, f"Invalid JSON file: {e}"

            # Validate import data structure
            is_valid, validation_error = self._validate_import_data(import_data)
            if not is_valid:
                return False, validation_error

            # Create backup before import
            if create_backup:
                backup_path = self._create_auto_backup()
                if backup_path:
                    logger.info(f"Created automatic backup at {backup_path}")

            # Import based on mode
            if mode == 'replace':
                success, error = self._import_replace(import_data)
            elif mode == 'merge':
                success, error = self._import_merge(import_data)
            else:
                return False, f"Invalid import mode: {mode}"

            if success:
                # Reload configuration manager if available
                if self.connection_manager:
                    try:
                        self.connection_manager.load_ssh_config()
                    except Exception as e:
                        logger.warning(f"Failed to reload SSH config: {e}")

            return success, error

        except Exception as e:
            error_msg = f"Failed to import configuration: {e}"
            logger.error(error_msg)
            return False, error_msg

    def _validate_import_data(self, data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Validate import data structure"""
        if not isinstance(data, dict):
            return False, "Import data must be a JSON object"

        # Check version
        version = data.get('version')
        if version is None:
            return False, "Missing 'version' field in import data"
        if not isinstance(version, int) or version > BACKUP_VERSION:
            return False, f"Unsupported backup version: {version}"

        # Check required fields
        if 'app_config' not in data:
            return False, "Missing 'app_config' field in import data"

        if not isinstance(data['app_config'], dict):
            return False, "'app_config' must be a JSON object"

        # Warn about platform/mode differences (but don't fail)
        current_isolated = self.config.get_setting('ssh.use_isolated_config', False)
        import_isolated = data.get('isolated_mode', False)
        if current_isolated != import_isolated:
            logger.warning(
                f"Import isolated mode ({import_isolated}) differs from current mode ({current_isolated})"
            )

        return True, None

    def _import_replace(self, import_data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Replace all configuration with imported data"""
        try:
            # Import SSH config
            if 'ssh_config' in import_data and import_data['ssh_config']:
                ssh_config_path = self.get_ssh_config_path()
                os.makedirs(os.path.dirname(ssh_config_path), exist_ok=True)
                with open(ssh_config_path, 'w', encoding='utf-8') as f:
                    f.write(import_data['ssh_config'])
                # Ensure correct permissions
                os.chmod(ssh_config_path, 0o600)
                logger.info(f"Replaced SSH config at {ssh_config_path}")

            # Import known_hosts if present
            if 'known_hosts' in import_data and import_data['known_hosts']:
                known_hosts_path = self.get_known_hosts_path()
                if known_hosts_path:
                    os.makedirs(os.path.dirname(known_hosts_path), exist_ok=True)
                    with open(known_hosts_path, 'w', encoding='utf-8') as f:
                        f.write(import_data['known_hosts'])
                    os.chmod(known_hosts_path, 0o600)
                    logger.info(f"Replaced known_hosts at {known_hosts_path}")

            # Import app config
            app_config = import_data['app_config']
            config_file = Path(get_config_dir()) / 'config.json'
            os.makedirs(config_file.parent, exist_ok=True)
            with open(config_file, 'w', encoding='utf-8') as f:
                json.dump(app_config, f, indent=2)
            logger.info(f"Replaced app config at {config_file}")

            # Reload config in memory
            self.config.config_data = self.config.load_json_config()

            logger.info("Configuration replaced successfully")
            return True, None

        except Exception as e:
            error_msg = f"Failed to replace configuration: {e}"
            logger.error(error_msg)
            return False, error_msg

    def _import_merge(self, import_data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Merge imported configuration with existing"""
        try:
            # For SSH config, we'll append imported hosts that don't exist
            if 'ssh_config' in import_data and import_data['ssh_config']:
                ssh_config_path = self.get_ssh_config_path()
                self._merge_ssh_config(ssh_config_path, import_data['ssh_config'])

            # For known_hosts, append if in isolated mode
            if 'known_hosts' in import_data and import_data['known_hosts']:
                known_hosts_path = self.get_known_hosts_path()
                if known_hosts_path:
                    self._merge_known_hosts(known_hosts_path, import_data['known_hosts'])

            # Merge app config
            app_config = import_data['app_config']
            self._merge_app_config(app_config)

            # Reload config in memory
            self.config.config_data = self.config.load_json_config()

            logger.info("Configuration merged successfully")
            return True, None

        except Exception as e:
            error_msg = f"Failed to merge configuration: {e}"
            logger.error(error_msg)
            return False, error_msg

    def _merge_ssh_config(self, target_path: str, imported_config: str):
        """Merge SSH config by appending imported hosts that don't exist"""
        try:
            # Read existing config
            existing_config = ''
            if os.path.exists(target_path):
                with open(target_path, 'r', encoding='utf-8') as f:
                    existing_config = f.read()

            # Extract host entries from existing config (simple approach)
            existing_hosts = self._extract_host_names(existing_config)
            
            # Parse imported config
            imported_lines = imported_config.split('\n')
            new_entries = []
            current_entry = []
            in_host_block = False
            current_host = None

            for line in imported_lines:
                stripped = line.strip()
                if stripped.lower().startswith('host '):
                    # Start of a new host block
                    if current_entry and current_host:
                        # Save previous entry if it's new
                        if current_host not in existing_hosts:
                            new_entries.extend(current_entry)
                            new_entries.append('')  # blank line separator
                    
                    # Start new entry
                    current_entry = [line]
                    in_host_block = True
                    # Extract host name
                    current_host = stripped[5:].strip().split()[0] if len(stripped) > 5 else None
                elif in_host_block:
                    current_entry.append(line)
                    # Check if we've exited the host block
                    if stripped and not stripped.startswith('#') and not line.startswith((' ', '\t')):
                        if not stripped.lower().startswith('host '):
                            # Not indented and not a host directive - end of block
                            in_host_block = False

            # Add last entry if needed
            if current_entry and current_host and current_host not in existing_hosts:
                new_entries.extend(current_entry)

            # Append new entries to existing config
            if new_entries:
                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                with open(target_path, 'a', encoding='utf-8') as f:
                    if existing_config and not existing_config.endswith('\n'):
                        f.write('\n')
                    f.write('\n# Imported entries\n')
                    f.write('\n'.join(new_entries))
                os.chmod(target_path, 0o600)
                logger.info(f"Merged SSH config - added {len([h for h in new_entries if h.strip().lower().startswith('host ')])} new hosts")

        except Exception as e:
            logger.error(f"Failed to merge SSH config: {e}")
            raise

    def _extract_host_names(self, config_text: str) -> set:
        """Extract all Host directive names from SSH config"""
        hosts = set()
        for line in config_text.split('\n'):
            stripped = line.strip()
            if stripped.lower().startswith('host '):
                # Extract host patterns
                host_patterns = stripped[5:].strip().split()
                hosts.update(host_patterns)
        return hosts

    def _merge_known_hosts(self, target_path: str, imported_hosts: str):
        """Merge known_hosts by appending unique entries"""
        try:
            existing_lines = set()
            if os.path.exists(target_path):
                with open(target_path, 'r', encoding='utf-8') as f:
                    existing_lines = set(line.strip() for line in f if line.strip())

            # Add new unique lines
            imported_lines = [line.strip() for line in imported_hosts.split('\n') if line.strip()]
            new_lines = [line for line in imported_lines if line not in existing_lines]

            if new_lines:
                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                with open(target_path, 'a', encoding='utf-8') as f:
                    for line in new_lines:
                        f.write(line + '\n')
                os.chmod(target_path, 0o600)
                logger.info(f"Merged known_hosts - added {len(new_lines)} new entries")

        except Exception as e:
            logger.error(f"Failed to merge known_hosts: {e}")
            raise

    def _merge_app_config(self, imported_config: Dict[str, Any]):
        """Merge app configuration with existing"""
        try:
            current_config = self.config.config_data.copy()

            # Merge groups
            if 'connection_groups' in imported_config:
                self._merge_groups(
                    current_config.get('connection_groups', {}),
                    imported_config['connection_groups']
                )

            # Merge connections metadata
            if 'connections_meta' in imported_config:
                current_meta = current_config.get('connections_meta', {})
                imported_meta = imported_config['connections_meta']
                # Only add new connection metadata, don't overwrite existing
                for conn_key, meta in imported_meta.items():
                    if conn_key not in current_meta:
                        current_meta[conn_key] = meta
                current_config['connections_meta'] = current_meta

            # Merge shortcuts (keep existing, add new)
            if 'shortcuts' in imported_config:
                current_shortcuts = current_config.get('shortcuts', {})
                imported_shortcuts = imported_config['shortcuts']
                for action, keys in imported_shortcuts.items():
                    if action not in current_shortcuts:
                        current_shortcuts[action] = keys
                current_config['shortcuts'] = current_shortcuts

            # For other settings, we can be more conservative and keep existing values
            # But we can add new keys that don't exist
            for key, value in imported_config.items():
                if key not in ['connection_groups', 'connections_meta', 'shortcuts', 'config_version']:
                    if key not in current_config:
                        current_config[key] = value

            # Update config version to current
            current_config['config_version'] = CONFIG_VERSION

            # Save merged config
            config_file = Path(get_config_dir()) / 'config.json'
            with open(config_file, 'w', encoding='utf-8') as f:
                json.dump(current_config, f, indent=2)

            logger.info("Merged app configuration")

        except Exception as e:
            logger.error(f"Failed to merge app config: {e}")
            raise

    def _merge_groups(self, current_groups: Dict[str, Any], imported_groups: Dict[str, Any]):
        """Merge group data, preserving existing groups and adding new ones"""
        try:
            current_group_data = current_groups.get('groups', {})
            imported_group_data = imported_groups.get('groups', {})

            # Build mapping of group names to IDs for existing groups
            existing_names = {
                info['name'].lower(): group_id 
                for group_id, info in current_group_data.items()
            }

            # Import groups that don't exist by name
            import uuid
            for imported_id, imported_info in imported_group_data.items():
                group_name = imported_info.get('name', '')
                if group_name.lower() not in existing_names:
                    # Create new group with new UUID to avoid conflicts
                    new_id = str(uuid.uuid4())
                    new_info = imported_info.copy()
                    new_info['id'] = new_id
                    new_info['order'] = len(current_group_data)
                    # Preserve imported color
                    current_group_data[new_id] = new_info
                    logger.info(f"Added new group: {group_name}")

            # Update the groups in current config
            if 'groups' not in current_groups:
                current_groups['groups'] = {}
            current_groups['groups'] = current_group_data

        except Exception as e:
            logger.error(f"Failed to merge groups: {e}")
            raise

    def _create_auto_backup(self) -> Optional[str]:
        """Create automatic backup before import"""
        try:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            backup_filename = f"auto_backup_{timestamp}.json"
            backup_path = self.backup_dir / backup_filename
            
            success, error = self.export_configuration(str(backup_path))
            if success:
                return str(backup_path)
            else:
                logger.error(f"Failed to create auto backup: {error}")
                return None

        except Exception as e:
            logger.error(f"Failed to create auto backup: {e}")
            return None

    def list_backups(self) -> List[Dict[str, Any]]:
        """List all available backups"""
        backups = []
        try:
            if not self.backup_dir.exists():
                return backups

            for backup_file in self.backup_dir.glob('*.json'):
                try:
                    stat = backup_file.stat()
                    backups.append({
                        'path': str(backup_file),
                        'name': backup_file.name,
                        'size': stat.st_size,
                        'modified': datetime.fromtimestamp(stat.st_mtime),
                    })
                except Exception as e:
                    logger.warning(f"Failed to stat backup file {backup_file}: {e}")

            # Sort by modification time, newest first
            backups.sort(key=lambda x: x['modified'], reverse=True)

        except Exception as e:
            logger.error(f"Failed to list backups: {e}")

        return backups
