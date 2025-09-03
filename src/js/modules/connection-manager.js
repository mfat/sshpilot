// Connection Manager Module
import { invoke } from '@tauri-apps/api/tauri';

export class ConnectionManager {
    constructor() {
        this.connections = new Map();
        this.activeConnections = new Map();
        this.groups = new Map();
        this.isInitialized = false;
    }

    async initialize() {
        try {
            console.log('Initializing Connection Manager...');
            
            // Load connections from Tauri backend
            await this.loadConnections();
            
            // Set up groups
            this.setupGroups();
            
            this.isInitialized = true;
            console.log('Connection Manager initialized');
            
        } catch (error) {
            console.error('Failed to initialize Connection Manager:', error);
            throw error;
        }
    }

    async loadConnections() {
        try {
            const connections = await invoke('get_connections');
            this.connections.clear();
            
            connections.forEach(conn => {
                this.connections.set(conn.id, {
                    ...conn,
                    isConnected: false,
                    lastUsed: conn.last_used ? new Date(conn.last_used) : null,
                    created_at: new Date(conn.created_at)
                });
            });
            
            console.log(`Loaded ${this.connections.size} connections`);
            
        } catch (error) {
            console.error('Failed to load connections:', error);
            throw error;
        }
    }

    async saveConnection(connectionData) {
        try {
            const connectionId = await invoke('save_connection', {
                connection: {
                    ...connectionData,
                    id: connectionData.id || '',
                    created_at: new Date().toISOString(),
                    last_used: null
                }
            });
            
            // Add to local cache
            const connection = {
                ...connectionData,
                id: connectionId,
                isConnected: false,
                created_at: new Date(),
                last_used: null
            };
            
            this.connections.set(connectionId, connection);
            
            // Update groups
            if (connection.group) {
                this.addToGroup(connection.group, connectionId);
            }
            
            console.log('Connection saved:', connectionId);
            return connectionId;
            
        } catch (error) {
            console.error('Failed to save connection:', error);
            throw error;
        }
    }

    async deleteConnection(connectionId) {
        try {
            await invoke('delete_connection', { connection_id: connectionId });
            
            // Remove from local cache
            const connection = this.connections.get(connectionId);
            if (connection && connection.group) {
                this.removeFromGroup(connection.group, connectionId);
            }
            
            this.connections.delete(connectionId);
            this.activeConnections.delete(connectionId);
            
            console.log('Connection deleted:', connectionId);
            
        } catch (error) {
            console.error('Failed to delete connection:', error);
            throw error;
        }
    }

    async connectToServer(connectionId) {
        try {
            const connection = this.connections.get(connectionId);
            if (!connection) {
                throw new Error('Connection not found');
            }

            // Mark as connecting
            connection.isConnected = 'connecting';
            this.updateConnectionStatus(connectionId, 'connecting');

            // Connect via Tauri backend
            const sshConnectionId = await invoke('connect_ssh', {
                host: connection.host,
                port: connection.port,
                username: connection.username,
                password: connection.password || null,
                key_path: connection.key_path || null,
                key_passphrase: connection.key_passphrase || null,
                nickname: connection.nickname
            });

            // Update connection status
            connection.isConnected = true;
            connection.lastUsed = new Date();
            connection.sshConnectionId = sshConnectionId;
            
            this.activeConnections.set(connectionId, sshConnectionId);
            this.updateConnectionStatus(connectionId, 'connected');

            console.log('Connected to server:', connectionId);
            return sshConnectionId;

        } catch (error) {
            console.error('Failed to connect to server:', error);
            
            // Update connection status to disconnected
            const connection = this.connections.get(connectionId);
            if (connection) {
                connection.isConnected = false;
                this.updateConnectionStatus(connectionId, 'disconnected');
            }
            
            throw error;
        }
    }

    async disconnectFromServer(connectionId) {
        try {
            const sshConnectionId = this.activeConnections.get(connectionId);
            if (sshConnectionId) {
                await invoke('disconnect_ssh', { connection_id: sshConnectionId });
                this.activeConnections.delete(connectionId);
            }

            const connection = this.connections.get(connectionId);
            if (connection) {
                connection.isConnected = false;
                this.updateConnectionStatus(connectionId, 'disconnected');
            }

            console.log('Disconnected from server:', connectionId);

        } catch (error) {
            console.error('Failed to disconnect from server:', error);
            throw error;
        }
    }

    async executeCommand(connectionId, command) {
        try {
            const sshConnectionId = this.activeConnections.get(connectionId);
            if (!sshConnectionId) {
                throw new Error('Connection not active');
            }

            const result = await invoke('execute_command', {
                connection_id: sshConnectionId,
                command: command
            });

            return result;

        } catch (error) {
            console.error('Failed to execute command:', error);
            throw error;
        }
    }

    async testConnection(connectionData) {
        try {
            const result = await invoke('test_connection', {
                host: connectionData.host,
                port: connectionData.port,
                username: connectionData.username,
                password: connectionData.password || null,
                key_path: connectionData.key_path || null
            });

            return result;

        } catch (error) {
            console.error('Failed to test connection:', error);
            throw error;
        }
    }

    getConnection(connectionId) {
        return this.connections.get(connectionId);
    }

    getAllConnections() {
        return Array.from(this.connections.values());
    }

    getActiveConnections() {
        return Array.from(this.activeConnections.keys());
    }

    getConnectionsByGroup(groupName) {
        const group = this.groups.get(groupName);
        if (!group) return [];
        
        return group.connections.map(connId => this.connections.get(connId)).filter(Boolean);
    }

    searchConnections(query) {
        if (!query || query.trim() === '') {
            return this.getAllConnections();
        }

        const lowerQuery = query.toLowerCase();
        return this.getAllConnections().filter(connection => {
            return connection.nickname.toLowerCase().includes(lowerQuery) ||
                   connection.host.toLowerCase().includes(lowerQuery) ||
                   connection.username.toLowerCase().includes(lowerQuery) ||
                   (connection.group && connection.group.toLowerCase().includes(lowerQuery));
        });
    }

    setupGroups() {
        this.groups.clear();
        
        // Group connections by their group property
        this.connections.forEach((connection, connectionId) => {
            if (connection.group) {
                this.addToGroup(connection.group, connectionId);
            }
        });
    }

    addToGroup(groupName, connectionId) {
        if (!this.groups.has(groupName)) {
            this.groups.set(groupName, {
                name: groupName,
                connections: new Set(),
                created_at: new Date()
            });
        }
        
        this.groups.get(groupName).connections.add(connectionId);
    }

    removeFromGroup(groupName, connectionId) {
        const group = this.groups.get(groupName);
        if (group) {
            group.connections.delete(connectionId);
            
            // Remove group if empty
            if (group.connections.size === 0) {
                this.groups.delete(groupName);
            }
        }
    }

    getAllGroups() {
        return Array.from(this.groups.values()).map(group => ({
            ...group,
            connections: Array.from(group.connections)
        }));
    }

    updateConnectionStatus(connectionId, status) {
        const connection = this.connections.get(connectionId);
        if (connection) {
            connection.isConnected = status;
            
            // Emit custom event for UI updates
            const event = new CustomEvent('connectionStatusChanged', {
                detail: { connectionId, status, connection }
            });
            document.dispatchEvent(event);
        }
    }

    async refreshConnectionStatuses() {
        // This would typically ping active connections to check if they're still alive
        // For now, we'll just log the current status
        console.log('Refreshing connection statuses...');
        
        this.activeConnections.forEach((sshConnectionId, connectionId) => {
            // In a real implementation, you'd check if the SSH connection is still alive
            console.log(`Connection ${connectionId} is active`);
        });
    }

    // Utility methods
    isConnected(connectionId) {
        const connection = this.connections.get(connectionId);
        return connection ? connection.isConnected === true : false;
    }

    isConnecting(connectionId) {
        const connection = this.connections.get(connectionId);
        return connection ? connection.isConnected === 'connecting' : false;
    }

    getConnectionCount() {
        return this.connections.size;
    }

    getActiveConnectionCount() {
        return this.activeConnections.size;
    }

    getGroupCount() {
        return this.groups.size;
    }

    // Event listeners for external use
    onConnectionStatusChanged(callback) {
        document.addEventListener('connectionStatusChanged', callback);
    }

    onConnectionAdded(callback) {
        document.addEventListener('connectionAdded', callback);
    }

    onConnectionRemoved(callback) {
        document.addEventListener('connectionRemoved', callback);
    }

    // Cleanup
    destroy() {
        // Disconnect all active connections
        this.activeConnections.forEach((sshConnectionId, connectionId) => {
            this.disconnectFromServer(connectionId).catch(console.error);
        });
        
        this.connections.clear();
        this.activeConnections.clear();
        this.groups.clear();
        this.isInitialized = false;
    }
}

