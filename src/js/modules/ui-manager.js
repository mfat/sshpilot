// UI Manager Module
export class UIManager {
    constructor() {
        this.modals = new Map();
        this.isInitialized = false;
    }

    async initialize() {
        try {
            console.log('Initializing UI Manager...');
            
            // Set up modal event listeners
            this.setupModalEventListeners();
            
            // Set up form event listeners
            this.setupFormEventListeners();
            
            this.isInitialized = true;
            console.log('UI Manager initialized');
            
        } catch (error) {
            console.error('Failed to initialize UI Manager:', error);
            throw error;
        }
    }

    setupModalEventListeners() {
        // Modal overlay click to close
        const modalOverlay = document.getElementById('modal-overlay');
        if (modalOverlay) {
            modalOverlay.addEventListener('click', (e) => {
                if (e.target === modalOverlay) {
                    this.hideAllModals();
                }
            });
        }

        // Modal close buttons
        document.querySelectorAll('.modal-close').forEach(closeBtn => {
            closeBtn.addEventListener('click', () => {
                const modal = closeBtn.closest('.modal');
                if (modal) {
                    this.hideModal(modal.id);
                }
            });
        });
    }

    setupFormEventListeners() {
        // Connection form authentication method change
        const authMethodSelect = document.getElementById('connection-auth-method');
        if (authMethodSelect) {
            authMethodSelect.addEventListener('change', (e) => {
                this.toggleAuthMethodFields(e.target.value);
            });
        }

        // Key type change for key size field
        const keyTypeSelect = document.getElementById('key-type');
        if (keyTypeSelect) {
            keyTypeSelect.addEventListener('change', (e) => {
                this.toggleKeySizeField(e.target.value);
            });
        }
    }

    // Modal Management
    showModal(modalId) {
        const modal = document.getElementById(modalId);
        const overlay = document.getElementById('modal-overlay');
        
        if (modal && overlay) {
            overlay.classList.add('active');
            modal.style.display = 'block';
            
            // Focus first input
            const firstInput = modal.querySelector('input, select, textarea');
            if (firstInput) {
                firstInput.focus();
            }
            
            this.modals.set(modalId, true);
        }
    }

    hideModal(modalId) {
        const modal = document.getElementById(modalId);
        const overlay = document.getElementById('modal-overlay');
        
        if (modal) {
            modal.style.display = 'none';
            
            // Clear form if exists
            const form = modal.querySelector('form');
            if (form) {
                form.reset();
            }
        }
        
        // Hide overlay if no modals are visible
        if (overlay && this.getVisibleModalCount() === 0) {
            overlay.classList.remove('active');
        }
        
        this.modals.delete(modalId);
    }

    hideAllModals() {
        this.modals.forEach((_, modalId) => {
            this.hideModal(modalId);
        });
    }

    getVisibleModalCount() {
        return this.modals.size;
    }

    // Connection Dialog
    showConnectionDialog() {
        this.showModal('connection-dialog');
    }

    hideConnectionDialog() {
        this.hideModal('connection-dialog');
    }

    // Key Dialog
    showKeyDialog() {
        this.showModal('key-dialog');
    }

    hideKeyDialog() {
        this.hideModal('key-dialog');
    }

    // Group Dialog
    showGroupDialog() {
        // Implementation for group dialog
        console.log('Group dialog not yet implemented');
    }

    // Preferences Dialog
    showPreferencesDialog() {
        // Implementation for preferences dialog
        console.log('Preferences dialog not yet implemented');
    }

    // Form Field Management
    toggleAuthMethodFields(authMethod) {
        const keyPathGroup = document.getElementById('key-path-group');
        const passwordGroup = document.getElementById('password-group');
        
        if (authMethod === 'key') {
            if (keyPathGroup) keyPathGroup.style.display = 'block';
            if (passwordGroup) passwordGroup.style.display = 'none';
        } else {
            if (keyPathGroup) keyPathGroup.style.display = 'none';
            if (passwordGroup) passwordGroup.style.display = 'block';
        }
    }

    toggleKeySizeField(keyType) {
        const keySizeGroup = document.getElementById('key-size-group');
        
        if (keyType === 'rsa' && keySizeGroup) {
            keySizeGroup.style.display = 'block';
        } else if (keySizeGroup) {
            keySizeGroup.style.display = 'none';
        }
    }

    // Sidebar Management
    toggleSidebar() {
        const sidebar = document.getElementById('sidebar');
        if (sidebar) {
            sidebar.classList.toggle('collapsed');
        }
    }

    showSidebar() {
        const sidebar = document.getElementById('sidebar');
        if (sidebar) {
            sidebar.classList.remove('collapsed');
        }
    }

    hideSidebar() {
        const sidebar = document.getElementById('sidebar');
        if (sidebar) {
            sidebar.classList.add('collapsed');
        }
    }

    // Connection List Management
    updateConnectionList() {
        const connectionList = document.getElementById('connection-list');
        if (!connectionList) return;
        
        const connections = window.sshpilotApp.getConnectionManager().getAllConnections();
        
        connectionList.innerHTML = '';
        
        if (connections.length === 0) {
            connectionList.innerHTML = '<div class="empty-state">No connections yet</div>';
            return;
        }
        
        connections.forEach(connection => {
            const connectionItem = this.createConnectionItem(connection);
            connectionList.appendChild(connectionItem);
        });
    }

    createConnectionItem(connection) {
        const item = document.createElement('div');
        item.className = 'connection-item';
        item.dataset.connectionId = connection.id;
        
        const statusClass = connection.isConnected ? 'connected' : 'disconnected';
        
        item.innerHTML = `
            <div class="connection-info">
                <div class="connection-name">${connection.nickname}</div>
                <div class="connection-details">${connection.username}@${connection.host}:${connection.port}</div>
            </div>
            <div class="connection-actions">
                <span class="status-indicator ${statusClass}"></span>
                <button class="action-btn" title="Connect" data-action="connect">▶</button>
                <button class="action-btn" title="Edit" data-action="edit">✏</button>
                <button class="action-btn" title="Delete" data-action="delete">🗑</button>
            </div>
        `;
        
        // Add event listeners
        this.setupConnectionItemEventListeners(item, connection);
        
        return item;
    }

    setupConnectionItemEventListeners(item, connection) {
        const connectBtn = item.querySelector('[data-action="connect"]');
        const editBtn = item.querySelector('[data-action="edit"]');
        const deleteBtn = item.querySelector('[data-action="delete"]');
        
        if (connectBtn) {
            connectBtn.addEventListener('click', async (e) => {
                e.stopPropagation();
                await this.handleConnectionAction(connection.id, 'connect');
            });
        }
        
        if (editBtn) {
            editBtn.addEventListener('click', async (e) => {
                e.stopPropagation();
                await this.handleConnectionAction(connection.id, 'edit');
            });
        }
        
        if (deleteBtn) {
            deleteBtn.addEventListener('click', async (e) => {
                e.stopPropagation();
                await this.handleConnectionAction(connection.id, 'delete');
            });
        }
        
        // Click on item to open terminal
        item.addEventListener('click', async () => {
            await this.openTerminalForConnection(connection.id);
        });
    }

    async handleConnectionAction(connectionId, action) {
        try {
            switch (action) {
                case 'connect':
                    await this.connectToServer(connectionId);
                    break;
                case 'edit':
                    await this.editConnection(connectionId);
                    break;
                case 'delete':
                    await this.deleteConnection(connectionId);
                    break;
            }
        } catch (error) {
            console.error(`Failed to handle connection action ${action}:`, error);
            window.sshpilotApp.showError(`Failed to ${action} connection`);
        }
    }

    async connectToServer(connectionId) {
        try {
            await window.sshpilotApp.getConnectionManager().connectToServer(connectionId);
            window.sshpilotApp.showSuccess('Connected to server successfully');
            
            // Update connection list
            this.updateConnectionList();
            
        } catch (error) {
            window.sshpilotApp.showError('Failed to connect to server', error);
        }
    }

    async editConnection(connectionId) {
        const connection = window.sshpilotApp.getConnectionManager().getConnection(connectionId);
        if (!connection) return;
        
        // Populate form with connection data
        this.populateConnectionForm(connection);
        
        // Show connection dialog
        this.showConnectionDialog();
    }

    async deleteConnection(connectionId) {
        if (confirm('Are you sure you want to delete this connection?')) {
            try {
                await window.sshpilotApp.getConnectionManager().deleteConnection(connectionId);
                window.sshpilotApp.showSuccess('Connection deleted successfully');
                
                // Update connection list
                this.updateConnectionList();
                
            } catch (error) {
                window.sshpilotApp.showError('Failed to delete connection', error);
            }
        }
    }

    async openTerminalForConnection(connectionId) {
        try {
            // Create new terminal for this connection
            const terminalId = await window.sshpilotApp.getTerminalManager().createNewTerminal(connectionId);
            
            // Connect to server if not already connected
            const connection = window.sshpilotApp.getConnectionManager().getConnection(connectionId);
            if (connection && !connection.isConnected) {
                await this.connectToServer(connectionId);
            }
            
        } catch (error) {
            window.sshpilotApp.showError('Failed to open terminal', error);
        }
    }

    // Key List Management
    updateKeyList() {
        const keyList = document.getElementById('key-list');
        if (!keyList) return;
        
        const keys = window.sshpilotApp.getKeyManager().getAllKeys();
        
        keyList.innerHTML = '';
        
        if (keys.length === 0) {
            keyList.innerHTML = '<div class="empty-state">No SSH keys found</div>';
            return;
        }
        
        keys.forEach(key => {
            const keyItem = this.createKeyItem(key);
            keyList.appendChild(keyItem);
        });
    }

    createKeyItem(key) {
        const item = document.createElement('div');
        item.className = 'key-item';
        item.dataset.keyId = key.id;
        
        item.innerHTML = `
            <div class="key-info">
                <div class="key-name">${key.name}</div>
                <div class="key-details">${key.key_type}${key.key_size ? ` (${key.key_size} bits)` : ''}</div>
            </div>
            <div class="key-actions">
                <button class="action-btn" title="Copy Public Key" data-action="copy-public">📋</button>
                <button class="action-btn" title="Delete" data-action="delete">🗑</button>
            </div>
        `;
        
        // Add event listeners
        this.setupKeyItemEventListeners(item, key);
        
        return item;
    }

    setupKeyItemEventListeners(item, key) {
        const copyPublicBtn = item.querySelector('[data-action="copy-public"]');
        const deleteBtn = item.querySelector('[data-action="delete"]');
        
        if (copyPublicBtn) {
            copyPublicBtn.addEventListener('click', async (e) => {
                e.stopPropagation();
                await this.copyPublicKey(key.id);
            });
        }
        
        if (deleteBtn) {
            deleteBtn.addEventListener('click', async (e) => {
                e.stopPropagation();
                await this.deleteKey(key.id);
            });
        }
    }

    async copyPublicKey(keyId) {
        try {
            await window.sshpilotApp.getKeyManager().copyKeyToClipboard(keyId, 'public');
            window.sshpilotApp.showSuccess('Public key copied to clipboard');
        } catch (error) {
            window.sshpilotApp.showError('Failed to copy public key', error);
        }
    }

    async deleteKey(keyId) {
        if (confirm('Are you sure you want to delete this SSH key?')) {
            try {
                await window.sshpilotApp.getKeyManager().deleteKey(keyId);
                window.sshpilotApp.showSuccess('SSH key deleted successfully');
                
                // Update key list
                this.updateKeyList();
                
            } catch (error) {
                window.sshpilotApp.showError('Failed to delete SSH key', error);
            }
        }
    }

    // Terminal Management
    updateTerminalTabs() {
        // This is handled by the TerminalManager
        // Just a placeholder for future UI updates
    }

    // Search Results
    showSearchResults(results) {
        // Implementation for search results dropdown
        console.log('Search results:', results);
    }

    clearSearchResults() {
        // Implementation for clearing search results
        console.log('Search results cleared');
    }

    // Welcome Screen
    showWelcomeScreen() {
        const welcomeScreen = document.getElementById('welcome-screen');
        if (welcomeScreen) {
            welcomeScreen.style.display = 'flex';
        }
    }

    hideWelcomeScreen() {
        const welcomeScreen = document.getElementById('welcome-screen');
        if (welcomeScreen) {
            welcomeScreen.style.display = 'none';
        }
    }

    // Form Population
    populateConnectionForm(connection) {
        const form = document.getElementById('connection-form');
        if (!form) return;
        
        // Populate form fields
        const fields = ['nickname', 'host', 'port', 'username', 'password', 'key_path', 'group'];
        fields.forEach(field => {
            const input = form.querySelector(`[name="${field}"]`);
            if (input && connection[field]) {
                input.value = connection[field];
            }
        });
        
        // Set authentication method
        const authMethodSelect = form.querySelector('[name="auth_method"]');
        if (authMethodSelect) {
            const method = connection.key_path ? 'key' : 'password';
            authMethodSelect.value = method;
            this.toggleAuthMethodFields(method);
        }
    }

    // Utility Methods
    showLoading(elementId) {
        const element = document.getElementById(elementId);
        if (element) {
            element.classList.add('loading');
        }
    }

    hideLoading(elementId) {
        const element = document.getElementById(elementId);
        if (element) {
            element.classList.remove('loading');
        }
    }

    showError(elementId, message) {
        const element = document.getElementById(elementId);
        if (element) {
            element.classList.add('error');
            element.textContent = message;
        }
    }

    clearError(elementId) {
        const element = document.getElementById(elementId);
        if (element) {
            element.classList.remove('error');
            element.textContent = '';
        }
    }

    // Cleanup
    destroy() {
        this.hideAllModals();
        this.modals.clear();
        this.isInitialized = false;
    }
}

