"""Group management utilities for sshPilot.

This module provides the :class:`GroupManager`, which handles creation,
deletion and organisation of hierarchical connection groups.
"""

from typing import Dict, List, Optional
import logging

from .config import Config
from .connection_identity import (
    canonical_connection_uuid,
    connection_uuid_from_id,
)

logger = logging.getLogger(__name__)


class GroupManager:
    """Manages hierarchical groups for connections"""

    def __init__(self, config: Config, connection_manager=None):
        self.config = config
        self.connection_manager = connection_manager
        self._uuid_by_nickname = {}
        self._nickname_by_uuid = {}
        self.groups = {}  # group_id -> GroupInfo
        self.connections = {}  # connection UUID -> primary group_id
        self.root_connections: List[str] = []  # ordered ungrouped UUIDs
        self._load_groups()
        if connection_manager is not None:
            self.bind_connections(connection_manager.get_connections())

    def bind_connections(self, connections) -> None:
        """Bind nickname-compatible calls to UUID-backed persisted references."""

        uuid_by_nickname = {}
        nickname_by_uuid = {}
        ambiguous_nicknames = set()
        for connection in connections or ():
            try:
                connection_uuid = canonical_connection_uuid(connection.uuid)
            except (AttributeError, ValueError):
                continue
            nickname = str(getattr(connection, 'nickname', '') or '')
            if nickname:
                nickname_by_uuid[connection_uuid] = nickname
                if nickname in ambiguous_nicknames:
                    continue
                if nickname in uuid_by_nickname:
                    uuid_by_nickname.pop(nickname, None)
                    ambiguous_nicknames.add(nickname)
                else:
                    uuid_by_nickname[nickname] = connection_uuid
        self._uuid_by_nickname = uuid_by_nickname
        self._nickname_by_uuid = nickname_by_uuid
        if not uuid_by_nickname:
            return

        changed = False

        def migrate(reference):
            try:
                return canonical_connection_uuid(reference)
            except ValueError:
                return uuid_by_nickname.get(str(reference))

        for group in self.groups.values():
            migrated = []
            seen = set()
            for reference in group.get('connections', []) or []:
                connection_uuid = migrate(reference)
                if connection_uuid and connection_uuid not in seen:
                    migrated.append(connection_uuid)
                    seen.add(connection_uuid)
            if migrated != group.get('connections', []):
                group['connections'] = migrated
                changed = True

        migrated_primary = {}
        for reference, group_id in self.connections.items():
            connection_uuid = migrate(reference)
            if connection_uuid and connection_uuid not in migrated_primary:
                migrated_primary[connection_uuid] = group_id
        if migrated_primary != self.connections:
            self.connections = migrated_primary
            changed = True

        migrated_root = []
        seen_root = set()
        for reference in self.root_connections:
            connection_uuid = migrate(reference)
            if connection_uuid and connection_uuid not in seen_root:
                migrated_root.append(connection_uuid)
                seen_root.add(connection_uuid)
        if migrated_root != self.root_connections:
            self.root_connections = migrated_root
            changed = True
        if changed:
            self._save_groups()

    def connection_key(self, reference) -> str:
        """Return the UUID storage key, accepting legacy nickname callers."""

        if hasattr(reference, 'uuid'):
            return canonical_connection_uuid(reference.uuid)
        text = str(reference or '')
        mapped = self._uuid_by_nickname.get(text)
        if mapped:
            return mapped
        if self.connection_manager is not None:
            finder = getattr(
                self.connection_manager,
                'find_connection_by_nickname',
                None,
            )
            connection = finder(text) if callable(finder) else None
            if connection is not None:
                try:
                    connection_uuid = canonical_connection_uuid(connection.uuid)
                    self._uuid_by_nickname[text] = connection_uuid
                    self._nickname_by_uuid[connection_uuid] = text
                    return connection_uuid
                except (AttributeError, ValueError):
                    pass
        try:
            return canonical_connection_uuid(text)
        except ValueError:
            pass
        try:
            return connection_uuid_from_id(text)
        except ValueError:
            # Standalone legacy tests/configs without a bound manager retain
            # their historical text keys; production always binds UUIDs.
            return text

    def connection_nickname(self, reference) -> str:
        key = self.connection_key(reference)
        return self._nickname_by_uuid.get(key, str(reference or ''))

    def _load_groups(self):
        """Load groups from configuration"""
        try:
            groups_data = self.config.get_setting('connection_groups', {})
            if not isinstance(groups_data, dict):
                groups_data = {}

            raw_groups = groups_data.get('groups', {})
            if not isinstance(raw_groups, dict):
                raw_groups = {}

            migration_needed = False
            normalised_groups: Dict[str, Dict] = {}
            for group_id, group_info in raw_groups.items():
                if not isinstance(group_info, dict):
                    migration_needed = True
                    continue

                normalised = group_info.copy()
                if 'color' not in normalised:
                    normalised['color'] = None
                    migration_needed = True

                normalised_groups[group_id] = normalised

            self.groups = normalised_groups

            raw_connections = groups_data.get('connections', {})
            self.connections = raw_connections if isinstance(raw_connections, dict) else {}

            raw_root_connections = groups_data.get('root_connections', [])
            self.root_connections = raw_root_connections if isinstance(raw_root_connections, list) else []

            if migration_needed:
                # Persist any normalised structure so future loads use the updated format
                self._save_groups()
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
                'groups': self.groups,
                'connections': self.connections,
                'root_connections': self.root_connections,
                'identity_schema_version': 1,
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

    def create_group(self, name: str, parent_id: Optional[str] = None, color: Optional[str] = None) -> str:
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
            'order': len(self.groups),
            'color': color,
        }

        if parent_id and parent_id in self.groups:
            self.groups[parent_id]['children'].append(group_id)

        self._save_groups()
        return group_id

    def set_group_color(self, group_id: str, color: Optional[str]):
        """Update a group's color and persist the change."""
        if group_id not in self.groups:
            return

        # Normalise empty strings to None for consistency
        self.groups[group_id]['color'] = color or None
        self._save_groups()

    def rename_group(self, group_id: str, new_name: str):
        """Rename a group and persist the change."""
        if group_id not in self.groups:
            return
        self.groups[group_id]['name'] = new_name
        self._save_groups()

    def delete_group(self, group_id: str):
        """Delete a group and move its contents to parent or root"""
        if group_id not in self.groups:
            return

        group = self.groups[group_id]
        parent_id = group.get('parent_id')

        # Move connections to parent or root. A connection may belong to more
        # than one group, so it only becomes ungrouped when no other group
        # still references it.
        for conn_nickname in list(group.get('connections', [])):
            other_groups = [
                gid for gid in self.get_connection_groups(conn_nickname)
                if gid != group_id
            ]
            if parent_id and parent_id in self.groups:
                parent_conns = self.groups[parent_id].setdefault('connections', [])
                if conn_nickname not in parent_conns:
                    parent_conns.append(conn_nickname)
                if self.connections.get(conn_nickname) in (None, group_id):
                    self.connections[conn_nickname] = parent_id
            elif other_groups:
                # Still a member of another group; just repoint the primary
                if self.connections.get(conn_nickname) in (None, group_id):
                    self.connections[conn_nickname] = other_groups[0]
            else:
                self.connections[conn_nickname] = None
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
        self._save_groups()

    def move_connection(self, connection_nickname: str, target_group_id: Optional[str] = None):
        """Move a connection to a different group"""
        connection_nickname = self.connection_key(connection_nickname)
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

    def copy_connection_to_group(self, connection_nickname: str, target_group_id: str):
        """Add a connection to ``target_group_id`` without removing it from any
        group it already belongs to.

        Unlike :meth:`move_connection`, this keeps existing memberships so the
        same connection can appear in several groups at once.
        """
        connection_nickname = self.connection_key(connection_nickname)
        if not target_group_id or target_group_id not in self.groups:
            return

        target_conns = self.groups[target_group_id].setdefault('connections', [])
        if connection_nickname not in target_conns:
            target_conns.append(connection_nickname)

        # A grouped connection is never part of the ungrouped/root list
        if connection_nickname in self.root_connections:
            self.root_connections.remove(connection_nickname)

        # Record the first group as the "primary" one for legacy single-group
        # lookups (used for colours, etc.) without overriding an existing one.
        if not self.connections.get(connection_nickname):
            self.connections[connection_nickname] = target_group_id

        self._save_groups()

    def remove_connection_from_group(self, connection_nickname: str, group_id: str):
        """Remove a connection from a single group.

        The connection stays in any other groups it belongs to. If it ends up
        without any group it returns to the ungrouped/root list.
        """
        connection_nickname = self.connection_key(connection_nickname)
        group = self.groups.get(group_id)
        if group and connection_nickname in group.get('connections', []):
            group['connections'] = [
                n for n in group['connections'] if n != connection_nickname
            ]

        remaining = self.get_connection_groups(connection_nickname)
        if remaining:
            if self.connections.get(connection_nickname) not in remaining:
                self.connections[connection_nickname] = remaining[0]
            if connection_nickname in self.root_connections:
                self.root_connections.remove(connection_nickname)
        else:
            self.connections[connection_nickname] = None
            if connection_nickname not in self.root_connections:
                self.root_connections.append(connection_nickname)

        self._save_groups()

    def forget_connection(self, reference) -> None:
        """Remove every persisted group reference for a deleted connection."""

        connection_key = self.connection_key(reference)
        self.connections.pop(connection_key, None)
        self.root_connections = [
            item for item in self.root_connections
            if item != connection_key
        ]
        for group in self.groups.values():
            group['connections'] = [
                item for item in group.get('connections', [])
                if item != connection_key
            ]
        nickname = self._nickname_by_uuid.pop(connection_key, None)
        if nickname:
            self._uuid_by_nickname.pop(nickname, None)
        self._save_groups()

    def rename_connection(self, old_nickname: str, new_nickname: str):
        """Rename a connection while preserving all of its group memberships."""
        if old_nickname == new_nickname:
            return

        stable_key = self._uuid_by_nickname.pop(old_nickname, None)
        if stable_key:
            self._uuid_by_nickname[new_nickname] = stable_key
            self._nickname_by_uuid[stable_key] = new_nickname
            return

        primary_group = self.connections.pop(old_nickname, None)
        self.connections[new_nickname] = primary_group

        # Replace the nickname in every group's ordered list, preserving its
        # position so multi-group membership and ordering survive the rename.
        for group in self.groups.values():
            conns = group.get('connections', [])
            if old_nickname in conns or new_nickname in conns:
                renamed = [new_nickname if n == old_nickname else n for n in conns]
                seen = set()
                group['connections'] = [n for n in renamed if not (n in seen or seen.add(n))]

        # Replace in the root (ungrouped) list as well
        if old_nickname in self.root_connections or new_nickname in self.root_connections:
            renamed_root = [new_nickname if n == old_nickname else n for n in self.root_connections]
            seen = set()
            self.root_connections = [n for n in renamed_root if not (n in seen or seen.add(n))]

        # A connection that belongs to a group must not linger in the root list
        if any(new_nickname in g.get('connections', []) for g in self.groups.values()):
            self.root_connections = [n for n in self.root_connections if n != new_nickname]

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
        """Get the primary group ID for a connection (or ``None``).

        A connection may belong to several groups; this returns a single
        representative group for legacy callers (e.g. colour resolution).
        """
        connection_nickname = self.connection_key(connection_nickname)
        primary = self.connections.get(connection_nickname)
        if primary and connection_nickname in self.groups.get(primary, {}).get('connections', []):
            return primary
        # Fall back to the authoritative group lists if the primary pointer is
        # stale or unset.
        groups = self.get_connection_groups(connection_nickname)
        return groups[0] if groups else None

    def resolve_display_group_id(
        self, connection_nickname: str, context_group_id: Optional[str] = None
    ) -> Optional[str]:
        """Resolve which group ID should drive UI for a connection row.

        When a connection appears in several groups, each sidebar row carries a
        display context (``context_group_id``). Prefer that over the primary
        group so colours match the group the row is listed under.
        """
        connection_nickname = self.connection_key(connection_nickname)
        if context_group_id:
            group = self.groups.get(context_group_id)
            if group and connection_nickname in group.get('connections', []):
                return context_group_id
        return self.get_connection_group(connection_nickname)

    def get_connection_groups(self, connection_nickname: str) -> List[str]:
        """Return every group ID that contains the connection."""
        connection_nickname = self.connection_key(connection_nickname)
        return [
            group_id
            for group_id, group in self.groups.items()
            if connection_nickname in group.get('connections', [])
        ]

    def set_group_expanded(self, group_id: str, expanded: bool):
        """Set whether a group is expanded"""
        if group_id in self.groups:
            self.groups[group_id]['expanded'] = expanded
            self._save_groups()

    def reorder_connection_in_group(self, connection_nickname: str, target_connection_nickname: str, position: str):
        """Reorder a connection within the same group relative to another connection"""
        connection_nickname = self.connection_key(connection_nickname)
        target_connection_nickname = self.connection_key(target_connection_nickname)
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
    
    def get_ordered_siblings(self, parent_id: Optional[str]) -> List[str]:
        """Return ordered child group ids for ``parent_id`` (``None`` = root level)."""
        if parent_id:
            parent = self.groups.get(parent_id)
            if not parent:
                return []
            return list(parent.get('children', []))
        root_ids = [
            gid for gid, ginfo in self.groups.items()
            if ginfo.get('parent_id') is None
        ]
        return sorted(root_ids, key=lambda gid: self.groups[gid].get('order', 0))

    def sibling_index(self, group_id: str) -> tuple:
        """Return ``(parent_id, index)`` of ``group_id`` among its siblings."""
        group = self.groups.get(group_id)
        if not group:
            raise ValueError(f"Unknown group '{group_id}'")
        parent_id = group.get('parent_id')
        siblings = self.get_ordered_siblings(parent_id)
        return parent_id, siblings.index(group_id)

    def _write_sibling_order(self, parent_id: Optional[str], ordered_ids: List[str]) -> None:
        if parent_id:
            if parent_id in self.groups:
                self.groups[parent_id]['children'] = list(ordered_ids)
        else:
            self._update_group_orders(ordered_ids, None)

    def _is_descendant(self, ancestor_id: str, node_id: Optional[str]) -> bool:
        """True if ``node_id`` is ``ancestor_id`` or nested under it."""
        if not node_id:
            return False
        cur = node_id
        seen = set()
        while cur and cur not in seen:
            if cur == ancestor_id:
                return True
            seen.add(cur)
            cur = self.groups.get(cur, {}).get('parent_id')
        return False

    def place_group(self, group_id: str, parent_id: Optional[str], index: int) -> bool:
        """Move ``group_id`` to ``index`` among ``parent_id``'s children (root if ``None``).

        The flattened sidebar is a view; all placement is tree-relative.
        """
        if group_id not in self.groups:
            return False
        if parent_id is not None and parent_id not in self.groups:
            return False
        if parent_id is not None and self._is_descendant(group_id, parent_id):
            return False

        group = self.groups[group_id]
        old_parent = group.get('parent_id')

        old_siblings = self.get_ordered_siblings(old_parent)
        old_index = (
            old_siblings.index(group_id) if group_id in old_siblings else None
        )

        if group_id in old_siblings:
            old_siblings.remove(group_id)
            self._write_sibling_order(old_parent, old_siblings)
        if old_parent and old_parent in self.groups:
            children = self.groups[old_parent].get('children', [])
            if group_id in children:
                children.remove(group_id)

        group['parent_id'] = parent_id

        new_siblings = self.get_ordered_siblings(parent_id)
        if group_id in new_siblings:
            new_siblings.remove(group_id)

        if old_parent == parent_id and old_index is not None and old_index < index:
            index -= 1
        index = max(0, min(int(index), len(new_siblings)))
        new_siblings.insert(index, group_id)
        self._write_sibling_order(parent_id, new_siblings)

        self._save_groups()
        return True

    def reorder_group(self, source_group_id: str, target_group_id: str, position: str):
        """Reorder a group relative to another group at the same level."""
        try:
            parent_id, target_index = self.sibling_index(target_group_id)
        except ValueError:
            return
        if position == 'below':
            target_index += 1
        self.place_group(source_group_id, parent_id, target_index)
    
    def _update_group_orders(self, groups_list, parent_id):
        """Update the order field for groups at a given level"""
        for i, group_id in enumerate(groups_list):
            if group_id in self.groups:
                self.groups[group_id]['order'] = i
