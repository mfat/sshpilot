"""Group management utilities for sshPilot.

This module provides the :class:`GroupManager`, which handles creation,
deletion and organisation of hierarchical connection groups.
"""

from typing import Dict, List, Optional
import logging

from .config import Config

logger = logging.getLogger(__name__)


class GroupManager:
    """Manages hierarchical groups for connections"""

    def __init__(self, config: Config):
        self.config = config
        self.groups = {}  # group_id -> GroupInfo
        self.connections = {}  # connection_nickname -> group_id
        self.root_connections: List[str] = []  # order of ungrouped connections
        self._load_groups()

    def _load_groups(self):
        """Load groups from configuration"""
        try:
            groups_data = self.config.get_setting('connection_groups', {})
            self.groups = groups_data.get('groups', {})
            self.connections = groups_data.get('connections', {})
            self.root_connections = groups_data.get('root_connections', [])
        except Exception as e:
            logger.error(f"Failed to load groups: {e}")
            self.groups = {}
            self.connections = {}
            self.root_connections = []

        # Ensure root_connections only contains ungrouped connections
        for nickname, group_id in self.connections.items():
            if group_id is None:
                if nickname not in self.root_connections:
                    self.root_connections.append(nickname)
            elif nickname in self.root_connections:
                self.root_connections.remove(nickname)

        # Deduplicate while preserving order
        seen = set()
        self.root_connections = [n for n in self.root_connections if not (n in seen or seen.add(n))]

    def _get_sibling_ids(self, parent_id: Optional[str], exclude: Optional[str] = None) -> List[str]:
        """Return ordered list of sibling group IDs for a parent."""
        siblings = [
            group_id
            for group_id, group in self.groups.items()
            if group.get('parent_id') == parent_id and group_id != exclude
        ]
        siblings.sort(key=lambda gid: self.groups[gid].get('order', 0))
        return siblings

    def _next_group_order(self, parent_id: Optional[str], exclude: Optional[str] = None) -> int:
        """Return the next order index for a parent's children."""
        return len(self._get_sibling_ids(parent_id, exclude=exclude))

    def refresh_group_order(self, parent_id: Optional[str], save: bool = False) -> List[str]:
        """Recalculate order values for children of ``parent_id``."""
        siblings = self._get_sibling_ids(parent_id)
        for index, group_id in enumerate(siblings):
            self.groups[group_id]['order'] = index
        if parent_id and parent_id in self.groups:
            self.groups[parent_id]['children'] = siblings.copy()
        if save:
            self._save_groups()
        return siblings

    def reorder_group(
        self,
        group_id: str,
        target_group_id: Optional[str],
        position: str,
        parent_id: Optional[str] = None,
    ) -> bool:
        """Reorder ``group_id`` relative to ``target_group_id`` within ``parent_id``."""

        if group_id not in self.groups:
            return False

        if parent_id is None:
            parent_id = self.groups[group_id].get('parent_id')

        siblings = self._get_sibling_ids(parent_id, exclude=group_id)

        position = position or 'end'

        if position == 'above':
            if target_group_id and target_group_id in siblings:
                index = siblings.index(target_group_id)
                siblings.insert(index, group_id)
            else:
                siblings.insert(0, group_id)
        elif position == 'below':
            if target_group_id and target_group_id in siblings:
                index = siblings.index(target_group_id)
                siblings.insert(index + 1, group_id)
            else:
                siblings.append(group_id)
        else:  # 'end' or unspecified positions
            siblings.append(group_id)

        for index, sibling_id in enumerate(siblings):
            self.groups[sibling_id]['order'] = index

        if parent_id and parent_id in self.groups:
            self.groups[parent_id]['children'] = siblings.copy()

        self._save_groups()
        return True

    def _save_groups(self):
        """Save groups to configuration"""
        try:
            groups_data = {
                'groups': self.groups,
                'connections': self.connections,
                'root_connections': self.root_connections,
            }
            self.config.set_setting('connection_groups', groups_data)
        except Exception as e:
            logger.error(f"Failed to save groups: {e}")

    def group_name_exists(self, name: str) -> bool:
        """Check if a group name already exists"""
        for group in self.groups.values():
            if group['name'].lower() == name.lower():
                return True
        return False

    def create_group(self, name: str, parent_id: str = None) -> str:
        """Create a new group and return its ID"""
        # Check for duplicate names (case-insensitive)
        if self.group_name_exists(name):
            raise ValueError(f"Group name '{name}' already exists")
        
        import uuid
        group_id = str(uuid.uuid4())

        self.groups[group_id] = {
            'id': group_id,
            'name': name,
            'parent_id': parent_id,
            'children': [],
            'connections': [],
            'expanded': True,
            'order': self._next_group_order(parent_id)
        }

        if parent_id and parent_id in self.groups:
            self.groups[parent_id]['children'].append(group_id)

        self._save_groups()
        return group_id

    def delete_group(self, group_id: str):
        """Delete a group and move its contents to parent or root"""
        if group_id not in self.groups:
            return

        group = self.groups[group_id]
        parent_id = group.get('parent_id')

        # Move connections to parent or root
        for conn_nickname in group['connections']:
            self.connections[conn_nickname] = parent_id
            if parent_id is None:
                if conn_nickname not in self.root_connections:
                    self.root_connections.append(conn_nickname)

        # Move child groups to parent
        for child_id in group['children']:
            if child_id in self.groups:
                self.groups[child_id]['parent_id'] = parent_id
                if parent_id and parent_id in self.groups:
                    self.groups[parent_id]['children'].append(child_id)

        # Remove from parent's children
        if parent_id and parent_id in self.groups:
            if group_id in self.groups[parent_id]['children']:
                self.groups[parent_id]['children'].remove(group_id)

        # Delete the group
        del self.groups[group_id]
        self.refresh_group_order(parent_id)
        self._save_groups()

    def move_connection(self, connection_nickname: str, target_group_id: str = None):
        """Move a connection to a different group"""
        self.connections[connection_nickname] = target_group_id

        # Remove from old group and root list
        for group in self.groups.values():
            if connection_nickname in group['connections']:
                group['connections'].remove(connection_nickname)
        if connection_nickname in self.root_connections:
            self.root_connections.remove(connection_nickname)

        # Add to new group or root list
        if target_group_id and target_group_id in self.groups:
            if connection_nickname not in self.groups[target_group_id]['connections']:
                self.groups[target_group_id]['connections'].append(connection_nickname)
        else:
            if connection_nickname not in self.root_connections:
                self.root_connections.append(connection_nickname)

        self._save_groups()

    def rename_connection(self, old_nickname: str, new_nickname: str):
        """Rename a connection while preserving its group membership."""
        if old_nickname == new_nickname:
            return

        group_id = self.connections.pop(old_nickname, None)
        self.connections[new_nickname] = group_id

        # Remove any stray references to the old nickname
        if old_nickname in self.root_connections:
            self.root_connections = [n for n in self.root_connections if n != old_nickname]
        for group in self.groups.values():
            if old_nickname in group.get('connections', []):
                group['connections'] = [n for n in group['connections'] if n != old_nickname]

        if group_id and group_id in self.groups:
            conn_list = self.groups[group_id].setdefault('connections', [])
            if new_nickname not in conn_list:
                conn_list.append(new_nickname)
            if new_nickname in self.root_connections:
                self.root_connections.remove(new_nickname)
        else:
            if new_nickname not in self.root_connections:
                self.root_connections.append(new_nickname)

        # Deduplicate connections within groups
        for group in self.groups.values():
            seen = set()
            group['connections'] = [n for n in group.get('connections', []) if not (n in seen or seen.add(n))]


        self._save_groups()

    def get_group_hierarchy(self) -> List[Dict]:
        """Get the complete group hierarchy"""

        def build_tree(parent_id=None):
            result = []
            for group_id, group in self.groups.items():
                if group.get('parent_id') == parent_id:
                    group_copy = group.copy()
                    group_copy['children'] = build_tree(group_id)
                    result.append(group_copy)
            return sorted(result, key=lambda x: x.get('order', 0))

        return build_tree()

    def get_all_groups(self) -> List[Dict]:
        """Get all groups as a flat list for selection dialogs"""
        result = []
        for group_id, group in self.groups.items():
            group_copy = group.copy()
            # Remove children list to avoid confusion in flat view
            if 'children' in group_copy:
                del group_copy['children']
            result.append(group_copy)
        logger.debug(f"get_all_groups: Found {len(result)} groups: {[g['name'] for g in result]}")
        return sorted(result, key=lambda x: x.get('order', 0))

    def get_connection_group(self, connection_nickname: str) -> str:
        """Get the group ID for a connection"""
        return self.connections.get(connection_nickname)

    def set_group_expanded(self, group_id: str, expanded: bool):
        """Set whether a group is expanded"""
        if group_id in self.groups:
            self.groups[group_id]['expanded'] = expanded
            self._save_groups()

    def reorder_connection_in_group(self, connection_nickname: str, target_connection_nickname: str, position: str):
        """Reorder a connection within the same group relative to another connection"""
        # Get the group for both connections
        source_group_id = self.connections.get(connection_nickname)
        target_group_id = self.connections.get(target_connection_nickname)

        # Both connections must be in the same group
        if source_group_id != target_group_id:
            return

        if source_group_id:
            group = self.groups.get(source_group_id)
            if not group:
                return
            connections = group['connections']
        else:
            connections = self.root_connections

        # Remove the source connection from its current position
        if connection_nickname in connections:
            connections.remove(connection_nickname)

        # Find the target connection's position
        try:
            target_index = connections.index(target_connection_nickname)
        except ValueError:
            # Target not found, append to end
            connections.append(connection_nickname)
            self._save_groups()
            return

        # Insert at the appropriate position
        if position == 'above':
            connections.insert(target_index, connection_nickname)
        else:  # 'below'
            connections.insert(target_index + 1, connection_nickname)

        self._save_groups()

