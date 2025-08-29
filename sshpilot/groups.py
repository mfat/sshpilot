"""Group management utilities for sshPilot.

This module provides the :class:`GroupManager`, which handles creation,
deletion and organisation of hierarchical connection groups.
"""

from typing import Dict, List
import logging

from .config import Config

logger = logging.getLogger(__name__)


class GroupManager:
    """Manages hierarchical groups for connections"""

    def __init__(self, config: Config):
        self.config = config
        self.groups = {}  # group_id -> GroupInfo
        self.connections = {}  # connection_nickname -> group_id
        self._load_groups()

    def _load_groups(self):
        """Load groups from configuration"""
        try:
            groups_data = self.config.get_setting('connection_groups', {})
            self.groups = groups_data.get('groups', {})
            self.connections = groups_data.get('connections', {})
        except Exception as e:
            logger.error(f"Failed to load groups: {e}")
            self.groups = {}
            self.connections = {}

    def _save_groups(self):
        """Save groups to configuration"""
        try:
            groups_data = {
                'groups': self.groups,
                'connections': self.connections
            }
            self.config.set_setting('connection_groups', groups_data)
        except Exception as e:
            logger.error(f"Failed to save groups: {e}")

    def create_group(self, name: str, parent_id: str = None) -> str:
        """Create a new group and return its ID"""
        import uuid
        group_id = str(uuid.uuid4())

        self.groups[group_id] = {
            'id': group_id,
            'name': name,
            'parent_id': parent_id,
            'children': [],
            'connections': [],
            'expanded': True,
            'order': len(self.groups)
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
        self._save_groups()

    def move_connection(self, connection_nickname: str, target_group_id: str = None):
        """Move a connection to a different group"""
        self.connections[connection_nickname] = target_group_id

        # Remove from old group
        for group in self.groups.values():
            if connection_nickname in group['connections']:
                group['connections'].remove(connection_nickname)

        # Add to new group
        if target_group_id and target_group_id in self.groups:
            if connection_nickname not in self.groups[target_group_id]['connections']:
                self.groups[target_group_id]['connections'].append(connection_nickname)

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
        if source_group_id != target_group_id or not source_group_id:
            return

        group = self.groups.get(source_group_id)
        if not group:
            return

        connections = group['connections']

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

