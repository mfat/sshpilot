import { invoke } from '@tauri-apps/api/tauri';
import ConnectionManager from './modules/connection-manager.js';
import TerminalManager from './modules/terminal-manager.js';
import KeyManager from './modules/key-manager.js';
import UIManager from './modules/ui-manager.js';
import ThemeManager from './modules/theme-manager.js';
import NotificationManager from './modules/notification-manager.js';

class SSHPilotApp {
    constructor() {
        this.connectionManager = new ConnectionManager();
        this.terminalManager = new TerminalManager();
        this.keyManager = new KeyManager();
        this.uiManager = new UIManager();
        this.themeManager = new ThemeManager();
        this.notificationManager = new NotificationManager();
        
        this.isInitialized = false;
        
        this.setupEventListeners();
        this.initialize();
    }

    setupEventListeners() {
        // Sidebar toggle
        const sidebarToggle = document.getElementById('sidebar-toggle');
        if (sidebarToggle) {
            sidebarToggle.addEventListener('click', () => {
                this.uiManager.toggleSidebar();
            });
        }

        // New connection button
        const newConnectionBtn = document.getElementById('new-connection-btn');
        if (newConnectionBtn) {
            newConnectionBtn.addEventListener('click', () => {
                this.uiManager.showModal('connection-modal');
            });
        }

        // New terminal button
        const newTerminalBtn = document.getElementById('new-terminal-btn');
        if (newTerminalBtn) {
            newTerminalBtn.addEventListener('click', () => {
                this.terminalManager.createLocalTerminal();
            });
        }

        // New key button
        const newKeyBtn = document.getElementById('new-key-btn');
        if (newKeyBtn) {
            newKeyBtn.addEventListener('click', () => {
                this.uiManager.showModal('key-modal');
            });
        }

        // Preferences button
        const preferencesBtn = document.getElementById('preferences-btn');
        if (preferencesBtn) {
            preferencesBtn.addEventListener('click', () => {
                this.showPreferences();
            });
        }

        // Theme toggle button
        const themeToggle = document.getElementById('theme-toggle');
        if (themeToggle) {
            themeToggle.addEventListener('click', () => {
                this.themeManager.cycleTheme();
            });
        }

        // Search functionality
        const searchInput = document.getElementById('search-input');
        const searchButton = document.getElementById('search-button');
        
        if (searchInput) {
            searchInput.addEventListener('input', (e) => {
                this.handleSearch(e.target.value);
            });
            
            searchInput.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') {
                    this.handleSearch(e.target.value);
                }
            });
        }
        
        if (searchButton) {
            searchButton.addEventListener('click', () => {
                this.handleSearch(searchInput.value);
            });
        }

        // Welcome screen actions
        const welcomeNewConnection = document.getElementById('welcome-new-connection');
        const welcomeNewTerminal = document.getElementById('welcome-new-terminal');
        const welcomeGenerateKey = document.getElementById('welcome-generate-key');
        
        if (welcomeNewConnection) {
            welcomeNewConnection.addEventListener('click', () => {
                this.uiManager.showModal('connection-modal');
            });
        }
        
        if (welcomeNewTerminal) {
            welcomeNewTerminal.addEventListener('click', () => {
                this.terminalManager.createLocalTerminal();
            });
        }
        
        if (welcomeGenerateKey) {
            welcomeGenerateKey.addEventListener('click', () => {
                this.uiManager.showModal('key-modal');
            });
        }

        // Keyboard shortcuts
        document.addEventListener('keydown', (e) => {
            if (e.ctrlKey || e.metaKey) {
                switch (e.key) {
                    case 'n':
                        e.preventDefault();
                        this.uiManager.showModal('connection-modal');
                        break;
                    case 't':
                        e.preventDefault();
                        this.terminalManager.createLocalTerminal();
                        break;
                    case 'k':
                        e.preventDefault();
                        this.uiManager.showModal('key-modal');
                        break;
                    case 'p':
                        e.preventDefault();
                        this.showPreferences();
                        break;
                    case 'f':
                        e.preventDefault();
                        searchInput?.focus();
                        break;
                    case 'w':
                        e.preventDefault();
                        this.terminalManager.closeActiveTerminal();
                        break;
                }
            }
        });

        // Window focus events
        window.addEventListener('focus', () => {
            this.onWindowFocus();
        });

        // Before unload
        window.addEventListener('beforeunload', (e) => {
            this.onBeforeUnload(e);
        });

        // Connection form submission
        const connectionForm = document.getElementById('connection-form');
        if (connectionForm) {
            connectionForm.addEventListener('submit', (e) => {
                e.preventDefault();
                this.handleConnectionSubmit();
            });
        }

        // Key form submission
        const keyForm = document.getElementById('key-form');
        if (keyForm) {
            keyForm.addEventListener('submit', (e) => {
                e.preventDefault();
                this.handleKeySubmit();
            });
        }

        // Authentication method change
        const authMethodSelect = document.getElementById('connection-auth-method');
        if (authMethodSelect) {
            authMethodSelect.addEventListener('change', (e) => {
                this.handleAuthMethodChange(e.target.value);
            });
        }

        // Key type change
        const keyTypeSelect = document.getElementById('key-type');
        if (keyTypeSelect) {
            keyTypeSelect.addEventListener('change', (e) => {
                this.handleKeyTypeChange(e.target.value);
            });
        }

        // Browse key button
        const browseKeyBtn = document.getElementById('browse-key-btn');
        if (browseKeyBtn) {
            browseKeyBtn.addEventListener('click', () => {
                this.browseForKeyFile();
            });
        }
    }

    async initialize() {
        try {
            this.notificationManager.show('Initializing SSHPilot...', 'info');
            
            // Initialize theme
            await this.themeManager.initialize();
            
            // Load initial data
            await this.loadInitialData();
            
            // Setup connection manager events
            this.setupConnectionManagerEvents();
            
            // Setup terminal manager events
            this.setupTerminalManagerEvents();
            
            // Setup key manager events
            this.setupKeyManagerEvents();
            
            this.isInitialized = true;
            this.notificationManager.show('SSHPilot ready!', 'success');
            
            // Show welcome screen
            this.showWelcomeScreen();
            
        } catch (error) {
            console.error('Failed to initialize SSHPilot:', error);
            this.notificationManager.show('Failed to initialize SSHPilot', 'error');
        }
    }

    async loadInitialData() {
        try {
            // Load connections
            await this.connectionManager.loadConnections();
            
            // Load keys
            await this.keyManager.loadKeys();
            
            // Update UI
            this.uiManager.updateConnectionList(this.connectionManager.getConnections());
            this.uiManager.updateKeyList(this.keyManager.getKeys());
            
        } catch (error) {
            console.error('Failed to load initial data:', error);
        }
    }

    setupConnectionManagerEvents() {
        this.connectionManager.on('connectionStatusChanged', ({ connectionId, status }) => {
            this.uiManager.updateConnectionStatus(connectionId, status);
        });

        this.connectionManager.on('connectionAdded', ({ connection }) => {
            this.uiManager.addConnectionToList(connection);
        });

        this.connectionManager.on('connectionUpdated', ({ connection }) => {
            this.uiManager.updateConnectionInList(connection);
        });

        this.connectionManager.on('connectionRemoved', ({ connectionId }) => {
            this.uiManager.removeConnectionFromList(connectionId);
        });
    }

    setupTerminalManagerEvents() {
        this.terminalManager.on('terminalCreated', ({ terminalId, connectionId, title }) => {
            this.notificationManager.show(`Terminal created: ${title}`, 'success');
        });

        this.terminalManager.on('terminalClosed', ({ terminalId }) => {
            this.notificationManager.show('Terminal closed', 'info');
        });

        this.terminalManager.on('activeTerminalChanged', ({ terminalId }) => {
            // Update UI to reflect active terminal
            console.log('Active terminal changed to:', terminalId);
        });
    }

    setupKeyManagerEvents() {
        this.keyManager.on('keyGenerated', ({ key }) => {
            this.notificationManager.show(`SSH key generated: ${key.name}`, 'success');
            this.uiManager.updateKeyList(this.keyManager.getKeys());
        });

        this.keyManager.on('keyDeleted', ({ keyId }) => {
            this.notificationManager.show('SSH key deleted', 'info');
            this.uiManager.updateKeyList(this.keyManager.getKeys());
        });
    }

    async handleConnectionSubmit() {
        try {
            const formData = new FormData(document.getElementById('connection-form'));
            const connectionData = {
                nickname: formData.get('nickname') || document.getElementById('connection-nickname').value,
                host: formData.get('host') || document.getElementById('connection-host').value,
                port: parseInt(formData.get('port') || document.getElementById('connection-port').value),
                username: formData.get('username') || document.getElementById('connection-username').value,
                authMethod: document.getElementById('connection-auth-method').value,
                password: document.getElementById('connection-password').value,
                keyPath: document.getElementById('connection-key-path').value,
                passphrase: document.getElementById('connection-passphrase').value,
                commands: document.getElementById('connection-commands').value,
                group: document.getElementById('connection-group').value
            };

            // Validate required fields
            if (!connectionData.nickname || !connectionData.host || !connectionData.username) {
                this.notificationManager.show('Please fill in all required fields', 'warning');
                return;
            }

            // Save connection
            const connection = await this.connectionManager.saveConnection(connectionData);
            
            // Close modal
            this.uiManager.hideModal('connection-modal');
            
            // Clear form
            document.getElementById('connection-form').reset();
            
            // Show success message
            this.notificationManager.show(`Connection "${connection.nickname}" saved successfully`, 'success');
            
            // Optionally connect immediately
            if (connectionData.authMethod === 'password' && connectionData.password) {
                await this.connectToSSH(connection.id);
            }
            
        } catch (error) {
            console.error('Failed to save connection:', error);
            this.notificationManager.show(`Failed to save connection: ${error.message}`, 'error');
        }
    }

    async handleKeySubmit() {
        try {
            const formData = new FormData(document.getElementById('key-form'));
            const keyData = {
                name: formData.get('name') || document.getElementById('key-name').value,
                type: document.getElementById('key-type').value,
                passphrase: document.getElementById('key-passphrase').value,
                comment: document.getElementById('key-comment').value
            };

            // Validate required fields
            if (!keyData.name) {
                this.notificationManager.show('Please enter a key name', 'warning');
                return;
            }

            // Generate key
            await this.keyManager.generateKey(keyData);
            
            // Close modal
            this.uiManager.hideModal('key-modal');
            
            // Clear form
            document.getElementById('key-form').reset();
            
        } catch (error) {
            console.error('Failed to generate key:', error);
            this.notificationManager.show(`Failed to generate key: ${error.message}`, 'error');
        }
    }

    handleAuthMethodChange(authMethod) {
        const passwordGroup = document.getElementById('password-group');
        const keyGroup = document.getElementById('key-group');
        const passphraseGroup = document.getElementById('passphrase-group');

        // Hide all groups first
        passwordGroup.style.display = 'none';
        keyGroup.style.display = 'none';
        passphraseGroup.style.display = 'none';

        // Show relevant groups
        switch (authMethod) {
            case 'password':
                passwordGroup.style.display = 'block';
                break;
            case 'key':
                keyGroup.style.display = 'block';
                break;
            case 'key-with-passphrase':
                keyGroup.style.display = 'block';
                passphraseGroup.style.display = 'block';
                break;
        }
    }

    handleKeyTypeChange(keyType) {
        // Key type specific logic can be added here
        console.log('Key type changed to:', keyType);
    }

    async browseForKeyFile() {
        try {
            // Use Tauri dialog to browse for key file
            const result = await invoke('open_file_dialog', {
                title: 'Select SSH Key File',
                filters: [['SSH Keys', '*.pem,*.key,id_*']],
                defaultPath: '~/.ssh'
            });
            
            if (result) {
                document.getElementById('connection-key-path').value = result;
            }
        } catch (error) {
            console.error('Failed to browse for key file:', error);
            this.notificationManager.show('Failed to browse for key file', 'error');
        }
    }

    async connectToSSH(connectionId) {
        try {
            this.notificationManager.show('Connecting to SSH server...', 'info');
            
            const result = await this.connectionManager.connectSSH(connectionId);
            
            if (result.success) {
                this.notificationManager.show('SSH connection established', 'success');
                
                // Create terminal for this connection
                const connection = this.connectionManager.getConnection(connectionId);
                await this.terminalManager.createSSHTerminal(connectionId, connection.nickname);
                
            } else {
                this.notificationManager.show(`SSH connection failed: ${result.error}`, 'error');
            }
            
        } catch (error) {
            console.error('SSH connection error:', error);
            this.notificationManager.show(`SSH connection error: ${error.message}`, 'error');
        }
    }

    handleSearch(query) {
        if (!query.trim()) {
            // Clear search results
            this.uiManager.clearSearchResults();
            return;
        }

        try {
            // Search connections
            const connectionResults = this.connectionManager.searchConnections(query);
            
            // Search keys
            const keyResults = this.keyManager.searchKeys(query);
            
            // Display search results
            this.uiManager.showSearchResults({
                connections: connectionResults,
                keys: keyResults,
                query: query
            });
            
        } catch (error) {
            console.error('Search error:', error);
        }
    }

    showPreferences() {
        // TODO: Implement preferences dialog
        this.notificationManager.show('Preferences dialog not yet implemented', 'info');
    }

    showWelcomeScreen() {
        if (this.welcomeScreen) {
            this.welcomeScreen.style.display = 'flex';
        }
    }

    onWindowFocus() {
        // Refresh connection statuses when window gains focus
        this.connectionManager.refreshConnectionStatuses();
    }

    onBeforeUnload(e) {
        // Cleanup before closing
        if (this.terminalManager) {
            this.terminalManager.terminals.forEach(terminal => {
                if (terminal.xterm) {
                    terminal.xterm.dispose();
                }
            });
        }
    }

    // Public API methods
    getConnectionManager() {
        return this.connectionManager;
    }

    getTerminalManager() {
        return this.terminalManager;
    }

    getKeyManager() {
        return this.keyManager;
    }

    getUIManager() {
        return this.uiManager;
    }

    getThemeManager() {
        return this.themeManager;
    }

    getNotificationManager() {
        return this.notificationManager;
    }
}

// Initialize the application when DOM is loaded
document.addEventListener('DOMContentLoaded', () => {
    window.sshPilotApp = new SSHPilotApp();
});

// Export for module usage
export default SSHPilotApp;
