"""Group management utilities for sshPilot.

This module provides the :class:`GroupManager`, which handles creation,
deletion and organisation of hierarchical connection groups.
"""

import logging
from typing import Dict, List

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
            groups_data = self.config.get_setting("connection_groups", {})
            self.groups = groups_data.get("groups", {})
            self.connections = groups_data.get("connections", {})
            self.root_connections = groups_data.get("root_connections", [])
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

    def _save_groups(self):
        """Save groups to configuration"""
        try:
            groups_data = {
                "groups": self.groups,
                "connections": self.connections,
                "root_connections": self.root_connections,
            }
            self.config.set_setting("connection_groups", groups_data)
        except Exception as e:
            logger.error(f"Failed to save groups: {e}")

    def group_name_exists(self, name: str) -> bool:
        """Check if a group name already exists"""
        for group in self.groups.values():
            if group["name"].lower() == name.lower():
                return True
        return False

    def create_group(self, name: str, parent_id: str = None, color: str = None) -> str:
        """Create a new group and return its ID"""
        # Check for duplicate names (case-insensitive)
        if self.group_name_exists(name):
            raise ValueError(f"Group name '{name}' already exists")

        import uuid

        group_id = str(uuid.uuid4())

        self.groups[group_id] = {
            "id": group_id,
            "name": name,
            "parent_id": parent_id,
            "children": [],
            "connections": [],
            "expanded": True,
            "order": len(self.groups),
            "color": color,
        }

        if parent_id and parent_id in self.groups:
            self.groups[parent_id]["children"].append(group_id)

        self._save_groups()
        return group_id

    def update_group_color(self, group_id: str, color: str = None):
        """Update the color of a group"""
        if group_id in self.groups:
            self.groups[group_id]["color"] = color
            self._save_groups()

    def delete_group(self, group_id: str):
        """Delete a group and move its contents to parent or root"""
        if group_id not in self.groups:
            return

        group = self.groups[group_id]
        parent_id = group.get("parent_id")

        # Move connections to parent or root
        for conn_nickname in group["connections"]:
            self.connections[conn_nickname] = parent_id
            if parent_id is None:
                if conn_nickname not in self.root_connections:
                    self.root_connections.append(conn_nickname)

        # Move child groups to parent
        for child_id in group["children"]:
            if child_id in self.groups:
                self.groups[child_id]["parent_id"] = parent_id
                if parent_id and parent_id in self.groups:
                    self.groups[parent_id]["children"].append(child_id)

        # Remove from parent's children
        if parent_id and parent_id in self.groups:
            if group_id in self.groups[parent_id]["children"]:
                self.groups[parent_id]["children"].remove(group_id)

        # Delete the group
        del self.groups[group_id]
        self._save_groups()

    def move_connection(self, connection_nickname: str, target_group_id: str = None):
        """Move a connection to a different group"""
        self.connections[connection_nickname] = target_group_id

        # Remove from old group and root list
        for group in self.groups.values():
            if connection_nickname in group["connections"]:
                group["connections"].remove(connection_nickname)
        if connection_nickname in self.root_connections:
            self.root_connections.remove(connection_nickname)

        # Add to new group or root list
        if target_group_id and target_group_id in self.groups:
            if connection_nickname not in self.groups[target_group_id]["connections"]:
                self.groups[target_group_id]["connections"].append(connection_nickname)
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
            if old_nickname in group.get("connections", []):
                group["connections"] = [n for n in group["connections"] if n != old_nickname]

        if group_id and group_id in self.groups:
            conn_list = self.groups[group_id].setdefault("connections", [])
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
            group["connections"] = [n for n in group.get("connections", []) if not (n in seen or seen.add(n))]

        self._save_groups()

    def get_group_hierarchy(self) -> List[Dict]:
        """Get the complete group hierarchy"""

        def build_tree(parent_id=None):
            result = []
            for group_id, group in self.groups.items():
                if group.get("parent_id") == parent_id:
                    group_copy = group.copy()
                    group_copy["children"] = build_tree(group_id)
                    result.append(group_copy)
            return sorted(result, key=lambda x: x.get("order", 0))

        return build_tree()

    def get_all_groups(self) -> List[Dict]:
        """Get all groups as a flat list for selection dialogs"""
        result = []
        for group_id, group in self.groups.items():
            group_copy = group.copy()
            # Remove children list to avoid confusion in flat view
            if "children" in group_copy:
                del group_copy["children"]
            result.append(group_copy)
        logger.debug(f"get_all_groups: Found {len(result)} groups: {[g['name'] for g in result]}")
        return sorted(result, key=lambda x: x.get("order", 0))

    def get_connection_group(self, connection_nickname: str) -> str:
        """Get the group ID for a connection"""
        return self.connections.get(connection_nickname)

    def set_group_expanded(self, group_id: str, expanded: bool):
        """Set whether a group is expanded"""
        if group_id in self.groups:
            self.groups[group_id]["expanded"] = expanded
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
            connections = group["connections"]
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
        if position == "above":
            connections.insert(target_index, connection_nickname)
        else:  # 'below'
            connections.insert(target_index + 1, connection_nickname)

        self._save_groups()

    def reorder_group(self, source_group_id: str, target_group_id: str, position: str):
        """Reorder a group relative to another group at the same level"""
        # Get both groups
        source_group = self.groups.get(source_group_id)
        target_group = self.groups.get(target_group_id)

        if not source_group or not target_group:
            return

        # Both groups must have the same parent (be at the same level)
        source_parent = source_group.get("parent_id")
        target_parent = target_group.get("parent_id")

        if source_parent != target_parent:
            return

        # Get the list of groups at this level
        if source_parent:
            parent_group = self.groups.get(source_parent)
            if not parent_group:
                return
            groups_list = parent_group["children"]
        else:
            # Root level groups - we need to maintain order differently
            # For now, use the order field in the group data
            root_groups = [gid for gid, ginfo in self.groups.items() if ginfo.get("parent_id") is None]
            root_groups.sort(key=lambda gid: self.groups[gid].get("order", 0))
            groups_list = root_groups

        # Remove source group from its current position
        if source_group_id in groups_list:
            groups_list.remove(source_group_id)

        # Find target position
        try:
            target_index = groups_list.index(target_group_id)
        except ValueError:
            # Target not found, append to end
            groups_list.append(source_group_id)
            self._update_group_orders(groups_list, source_parent)
            self._save_groups()
            return

        # Insert at appropriate position
        if position == "above":
            groups_list.insert(target_index, source_group_id)
        else:  # 'below'
            groups_list.insert(target_index + 1, source_group_id)

        # Update the parent's children list or root group orders
        if source_parent:
            parent_group["children"] = groups_list
        else:
            self._update_group_orders(groups_list, None)

        self._save_groups()

    def _update_group_orders(self, groups_list, parent_id):
        """Update the order field for groups at a given level"""
        for i, group_id in enumerate(groups_list):
            if group_id in self.groups:
                self.groups[group_id]["order"] = i
