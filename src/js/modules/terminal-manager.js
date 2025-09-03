import { invoke } from '@tauri-apps/api/tauri';
import { EventEmitter } from './event-emitter.js';

class TerminalManager extends EventEmitter {
    constructor() {
        super();
        this.terminals = new Map();
        this.activeTerminalId = null;
        this.terminalTabs = document.getElementById('terminal-tabs');
        this.terminalContainer = document.getElementById('terminal-container');
        this.welcomeScreen = document.getElementById('welcome-screen');
        
        this.setupEventListeners();
    }

    setupEventListeners() {
        // Global terminal events
        document.addEventListener('keydown', (e) => {
            if (e.ctrlKey && e.key === 't') {
                e.preventDefault();
                this.createLocalTerminal();
            }
            if (e.ctrlKey && e.key === 'w') {
                e.preventDefault();
                this.closeActiveTerminal();
            }
            if (e.ctrlKey && e.key === 'Tab') {
                e.preventDefault();
                this.nextTerminal();
            }
            if (e.ctrlKey && e.shiftKey && e.key === 'Tab') {
                e.preventDefault();
                this.previousTerminal();
            }
        });
    }

    async createTerminal(connectionId = null, title = null) {
        try {
            const terminalId = crypto.randomUUID();
            const terminalTitle = title || (connectionId ? `SSH: ${connectionId}` : 'Local Terminal');
            
            // Create terminal in backend
            const terminal = await invoke('create_terminal', {
                connectionId,
                title: terminalTitle
            });
            
            // Create terminal UI
            const terminalElement = this.createTerminalUI(terminalId, terminalTitle, connectionId);
            
            // Store terminal info
            this.terminals.set(terminalId, {
                id: terminalId,
                connectionId,
                title: terminalTitle,
                element: terminalElement,
                xterm: null,
                isActive: false,
                createdAt: new Date()
            });
            
            // Create xterm.js instance
            await this.initializeXTerm(terminalId);
            
            // Add terminal tab
            this.addTerminalTab(terminalId, terminalTitle);
            
            // Set as active if first terminal
            if (this.terminals.size === 1) {
                this.setActiveTerminal(terminalId);
            }
            
            // Hide welcome screen
            if (this.welcomeScreen) {
                this.welcomeScreen.style.display = 'none';
            }
            
            this.emit('terminalCreated', { terminalId, connectionId, title: terminalTitle });
            return terminalId;
            
        } catch (error) {
            console.error('Failed to create terminal:', error);
            throw error;
        }
    }

    createTerminalUI(terminalId, title, connectionId) {
        const terminalElement = document.createElement('div');
        terminalElement.className = 'terminal-instance';
        terminalElement.id = `terminal-${terminalId}`;
        terminalElement.dataset.terminalId = terminalId;
        
        // Create xterm container
        const xtermContainer = document.createElement('div');
        xtermContainer.className = 'xterm-container';
        xtermContainer.id = `xterm-${terminalId}`;
        
        // Create toolbar
        const toolbar = this.createTerminalToolbar(terminalId);
        
        // Create status bar
        const statusBar = this.createTerminalStatusBar(terminalId, connectionId);
        
        terminalElement.appendChild(xtermContainer);
        terminalElement.appendChild(toolbar);
        terminalElement.appendChild(statusBar);
        
        this.terminalContainer.appendChild(terminalElement);
        
        return terminalElement;
    }

    createTerminalToolbar(terminalId) {
        const toolbar = document.createElement('div');
        toolbar.className = 'terminal-toolbar';
        
        const settingsBtn = document.createElement('button');
        settingsBtn.title = 'Terminal Settings';
        settingsBtn.innerHTML = `
            <svg viewBox="0 0 16 16">
                <path d="M8 4.754a3.246 3.246 0 1 0 0 6.492 3.246 3.246 0 0 0 0-6.492zM5.754 8a2.246 2.246 0 1 1 4.492 0 2.246 2.246 0 0 1-4.492 0z"/>
                <path d="M9.796 1.343c-.527-1.79-3.065-1.79-3.592 0l-.094.319a.873.873 0 0 1-1.255.52l-.292-.16c-1.64-.892-3.433.902-2.54 2.541l.159.292a.873.873 0 0 1-.52 1.255l-.319.094c-1.79.527-1.79 3.065 0 3.592l.319.094a.873.873 0 0 1 .52 1.255l-.16.292c-.892 1.64.901 3.434 2.541 2.54l.292-.159a.873.873 0 0 1 1.255.52l.094.319c.527 1.79 3.065 1.79 3.592 0l.319-.094a.873.873 0 0 1 1.255-.52l.292.16c1.64.893 3.434-.902 2.54-2.541l-.159-.292a.873.873 0 0 1 .52-1.255l.319-.094c1.79-.527 1.79-3.065 0-3.592l-.319-.094a.873.873 0 0 1-.52-1.255l.16-.292c.893-1.64-.902-3.433 2.54-2.541l.292.159a.873.873 0 0 1 1.255-.52l.094-.319zM8 2.994a.5.5 0 1 1-1 0 .5.5 0 0 1 1 0zM8 13a.5.5 0 1 1-1 0 .5.5 0 0 1 1 0z"/>
            </svg>
        `;
        settingsBtn.addEventListener('click', () => this.showTerminalSettings(terminalId));
        
        const clearBtn = document.createElement('button');
        clearBtn.title = 'Clear Terminal';
        clearBtn.innerHTML = `
            <svg viewBox="0 0 16 16">
                <path d="M5.5 5.5A.5.5 0 0 1 6 6v6a.5.5 0 0 1-1 0V6a.5.5 0 0 1 .5-.5zm2.5 0a.5.5 0 0 1 .5.5v6a.5.5 0 0 1-1 0V6a.5.5 0 0 1 .5-.5zm3 .5a.5.5 0 0 0-1 0v6a.5.5 0 0 0 1 0V6z"/>
                <path fill-rule="evenodd" d="M14.5 3a1 1 0 0 1-1 1H13v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V4h-.5a1 1 0 0 1-1-1V2a1 1 0 0 1 1-1H6a1 1 0 0 1 1-1h2a1 1 0 0 1 1 1h3.5a1 1 0 0 1 1 1v1zM4.118 4 4 4.059V13a1 1 0 0 0 1 1h6a1 1 0 0 0 1-1V4.059L11.882 4H4.118zM2.5 3V2h11v1h-11z"/>
            </svg>
        `;
        clearBtn.addEventListener('click', () => this.clearTerminal(terminalId));
        
        const copyBtn = document.createElement('button');
        copyBtn.title = 'Copy Selection';
        copyBtn.innerHTML = `
            <svg viewBox="0 0 16 16">
                <path d="M4 2a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V2zM2 4a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V6a2 2 0 0 0-2-2H2z"/>
            </svg>
        `;
        copyBtn.addEventListener('click', () => this.copyTerminalSelection(terminalId));
        
        const pasteBtn = document.createElement('button');
        pasteBtn.title = 'Paste';
        pasteBtn.innerHTML = `
            <svg viewBox="0 0 16 16">
                <path d="M4 1.5H3a2 2 0 0 0-2 2V14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V3.5a2 2 0 0 0-2-2h-1h1a2 2 0 0 1 2 2V14a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V3.5a2 2 0 0 1 2-2h1z"/>
                <path d="M9.5 1a.5.5 0 0 1 .5.5v1a.5.5 0 0 1-.5.5h-3a.5.5 0 0 1-.5-.5v-1a.5.5 0 0 1 .5-.5h3zM3 2.5a.5.5 0 0 1 .5-.5h7a.5.5 0 0 1 .5.5v1a.5.5 0 0 1-.5.5h-7a.5.5 0 0 1-.5-.5v-1z"/>
            </svg>
        `;
        pasteBtn.addEventListener('click', () => this.pasteToTerminal(terminalId));
        
        toolbar.appendChild(settingsBtn);
        toolbar.appendChild(clearBtn);
        toolbar.appendChild(copyBtn);
        toolbar.appendChild(pasteBtn);
        
        return toolbar;
    }

    createTerminalStatusBar(terminalId, connectionId) {
        const statusBar = document.createElement('div');
        statusBar.className = 'terminal-status-bar';
        
        const statusItem = document.createElement('div');
        statusItem.className = 'terminal-status-item';
        statusItem.innerHTML = `
            <svg viewBox="0 0 16 16">
                <path d="M8 16A8 8 0 1 0 8 0a8 8 0 0 0 0 16zm.93-9.412-1 4.705c-.07.34.029.533.304.533.194 0 .487-.07.686-.246l-.088.416c-.287.346-.92.598-1.465.598-.703 0-1.002-.422-.808-1.319l.738-3.468c.064-.293.006-.399-.287-.47l-.451-.081.082-.381 2.29-.287zM8 5.5a1 1 0 1 1 0-2 1 1 0 0 1 0 2z"/>
            </svg>
            <span>${connectionId ? 'SSH' : 'Local'}</span>
        `;
        
        statusBar.appendChild(statusItem);
        return statusBar;
    }

    async initializeXTerm(terminalId) {
        const terminal = this.terminals.get(terminalId);
        if (!terminal) return;
        
        try {
            // Import xterm.js dynamically
            const { Terminal } = await import('xterm');
            const { FitAddon } = await import('xterm-addon-fit');
            const { WebLinksAddon } = await import('xterm-addon-web-links');
            const { WebglAddon } = await import('xterm-addon-webgl');
            
            // Create xterm instance
            const xterm = new Terminal({
                cursorBlink: true,
                cursorStyle: 'block',
                fontSize: 14,
                fontFamily: 'Monaco, Menlo, "Ubuntu Mono", monospace',
                theme: {
                    background: getComputedStyle(document.documentElement).getPropertyValue('--terminal-bg') || '#1e1e1e',
                    foreground: getComputedStyle(document.documentElement).getPropertyValue('--terminal-fg') || '#ffffff',
                    cursor: getComputedStyle(document.documentElement).getPropertyValue('--primary-color') || '#007acc',
                    selection: 'rgba(0, 122, 204, 0.3)',
                    black: '#000000',
                    red: '#cd3131',
                    green: '#0dbc79',
                    yellow: '#e5e510',
                    blue: '#2472c8',
                    magenta: '#bc3fbc',
                    cyan: '#11a8cd',
                    white: '#e5e5e5',
                    brightBlack: '#666666',
                    brightRed: '#f14c4c',
                    brightGreen: '#23d18b',
                    brightYellow: '#f5f543',
                    brightBlue: '#3b8eea',
                    brightMagenta: '#d670d6',
                    brightCyan: '#29b8db',
                    brightWhite: '#ffffff'
                }
            });
            
            // Add addons
            const fitAddon = new FitAddon();
            const webLinksAddon = new WebLinksAddon();
            
            xterm.loadAddon(fitAddon);
            xterm.loadAddon(webLinksAddon);
            
            // Try to load WebGL addon for better performance
            try {
                const webglAddon = new WebglAddon();
                xterm.loadAddon(webglAddon);
            } catch (e) {
                console.log('WebGL addon not available, falling back to canvas');
            }
            
            // Open terminal
            const container = document.getElementById(`xterm-${terminalId}`);
            xterm.open(container);
            
            // Fit to container
            fitAddon.fit();
            
            // Handle window resize
            const resizeObserver = new ResizeObserver(() => {
                fitAddon.fit();
            });
            resizeObserver.observe(container);
            
            // Handle input
            xterm.onData((data) => {
                this.handleTerminalInput(terminalId, data);
            });
            
            // Handle selection change
            xterm.onSelectionChange(() => {
                this.updateCopyButtonState(terminalId);
            });
            
            // Store xterm instance
            terminal.xterm = xterm;
            terminal.fitAddon = fitAddon;
            terminal.resizeObserver = resizeObserver;
            
            // Write welcome message
            if (!terminal.connectionId) {
                xterm.writeln('Welcome to SSHPilot Terminal');
                xterm.writeln('Type "help" for available commands');
                xterm.writeln('');
                xterm.write('$ ');
            }
            
        } catch (error) {
            console.error('Failed to initialize xterm.js:', error);
            // Fallback to simple text area
            this.createFallbackTerminal(terminalId);
        }
    }

    createFallbackTerminal(terminalId) {
        const terminal = this.terminals.get(terminalId);
        if (!terminal) return;
        
        const container = document.getElementById(`xterm-${terminalId}`);
        container.innerHTML = `
            <div style="padding: 20px; color: var(--on-surface-color);">
                <h3>Terminal Emulation Unavailable</h3>
                <p>Failed to load xterm.js. Please check your internet connection and refresh the page.</p>
                <button onclick="location.reload()" class="primary-button">Refresh Page</button>
            </div>
        `;
    }

    addTerminalTab(terminalId, title) {
        const tab = document.createElement('div');
        tab.className = 'terminal-tab';
        tab.dataset.terminalId = terminalId;
        
        const status = document.createElement('div');
        status.className = 'terminal-tab-status disconnected';
        
        const titleSpan = document.createElement('span');
        titleSpan.className = 'terminal-tab-title';
        titleSpan.textContent = title;
        
        const closeBtn = document.createElement('button');
        closeBtn.className = 'terminal-tab-close';
        closeBtn.innerHTML = `
            <svg viewBox="0 0 16 16">
                <path d="M4.646 4.646a.5.5 0 0 1 .708 0L8 7.293l2.646-2.647a.5.5 0 0 1 .708.708L8.707 8l2.647 2.646a.5.5 0 0 1-.708.708L8 8.707l-2.646 2.647a.5.5 0 0 1-.708-.708L7.293 8 4.646 5.354a.5.5 0 0 1 0-.708z"/>
            </svg>
        `;
        
        closeBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            this.closeTerminal(terminalId);
        });
        
        tab.appendChild(status);
        tab.appendChild(titleSpan);
        tab.appendChild(closeBtn);
        
        tab.addEventListener('click', () => {
            this.setActiveTerminal(terminalId);
        });
        
        this.terminalTabs.appendChild(tab);
    }

    setActiveTerminal(terminalId) {
        if (this.activeTerminalId === terminalId) return;
        
        // Deactivate current terminal
        if (this.activeTerminalId) {
            const currentTerminal = this.terminals.get(this.activeTerminalId);
            if (currentTerminal) {
                currentTerminal.element.classList.remove('active');
                currentTerminal.isActive = false;
            }
            
            // Update tab
            const currentTab = this.terminalTabs.querySelector(`[data-terminal-id="${this.activeTerminalId}"]`);
            if (currentTab) {
                currentTab.classList.remove('active');
            }
        }
        
        // Activate new terminal
        this.activeTerminalId = terminalId;
        const terminal = this.terminals.get(terminalId);
        if (terminal) {
            terminal.element.classList.add('active');
            terminal.isActive = true;
            
            // Focus xterm
            if (terminal.xterm) {
                terminal.xterm.focus();
            }
        }
        
        // Update tab
        const tab = this.terminalTabs.querySelector(`[data-terminal-id="${terminalId}"]`);
        if (tab) {
            tab.classList.add('active');
        }
        
        // Update backend
        invoke('set_active_terminal', { terminalId }).catch(console.error);
        
        this.emit('activeTerminalChanged', { terminalId });
    }

    async closeTerminal(terminalId) {
        try {
            const terminal = this.terminals.get(terminalId);
            if (!terminal) return;
            
            // Close in backend
            await invoke('close_terminal', { terminalId });
            
            // Remove from DOM
            if (terminal.element) {
                terminal.element.remove();
            }
            
            // Remove tab
            const tab = this.terminalTabs.querySelector(`[data-terminal-id="${terminalId}"]`);
            if (tab) {
                tab.remove();
            }
            
            // Cleanup xterm
            if (terminal.xterm) {
                terminal.xterm.dispose();
            }
            if (terminal.resizeObserver) {
                terminal.resizeObserver.disconnect();
            }
            
            // Remove from map
            this.terminals.delete(terminalId);
            
            // Set new active terminal if needed
            if (this.activeTerminalId === terminalId) {
                const remainingTerminals = Array.from(this.terminals.keys());
                if (remainingTerminals.length > 0) {
                    this.setActiveTerminal(remainingTerminals[0]);
                } else {
                    this.activeTerminalId = null;
                    // Show welcome screen
                    if (this.welcomeScreen) {
                        this.welcomeScreen.style.display = 'flex';
                    }
                }
            }
            
            this.emit('terminalClosed', { terminalId });
            
        } catch (error) {
            console.error('Failed to close terminal:', error);
        }
    }

    async handleTerminalInput(terminalId, data) {
        const terminal = this.terminals.get(terminalId);
        if (!terminal || !terminal.xterm) return;
        
        try {
            if (terminal.connectionId) {
                // SSH connection - send to backend
                const result = await invoke('execute_command', {
                    connectionId: terminal.connectionId,
                    command: data
                });
                
                if (result.success) {
                    terminal.xterm.write(result.output || '');
                } else {
                    terminal.xterm.write(`\r\nError: ${result.error}\r\n`);
                }
            } else {
                // Local terminal - handle locally
                this.handleLocalCommand(terminalId, data);
            }
        } catch (error) {
            console.error('Terminal input error:', error);
            terminal.xterm.write(`\r\nError: ${error.message}\r\n`);
        }
    }

    handleLocalCommand(terminalId, data) {
        const terminal = this.terminals.get(terminalId);
        if (!terminal || !terminal.xterm) return;
        
        // Handle special keys
        if (data === '\r') {
            terminal.xterm.write('\r\n');
            // Process command line
            const line = terminal.xterm.buffer.active.getLine(terminal.xterm.buffer.active.baseY + terminal.xterm.buffer.active.cursorY);
            if (line) {
                const command = line.translateToString().trim();
                if (command) {
                    this.processLocalCommand(terminalId, command);
                }
            }
            terminal.xterm.write('$ ');
        } else if (data === '\u007F') { // Backspace
            // Handle backspace
            terminal.xterm.write('\b \b');
        } else {
            // Echo input
            terminal.xterm.write(data);
        }
    }

    processLocalCommand(terminalId, command) {
        const terminal = this.terminals.get(terminalId);
        if (!terminal || !terminal.xterm) return;
        
        const args = command.split(' ');
        const cmd = args[0];
        
        switch (cmd) {
            case 'help':
                terminal.xterm.writeln('Available commands:');
                terminal.xterm.writeln('  help     - Show this help');
                terminal.xterm.writeln('  clear    - Clear terminal');
                terminal.xterm.writeln('  date     - Show current date/time');
                terminal.xterm.writeln('  echo     - Echo arguments');
                terminal.xterm.writeln('  exit     - Close terminal');
                break;
            case 'clear':
                terminal.xterm.clear();
                break;
            case 'date':
                terminal.xterm.writeln(new Date().toString());
                break;
            case 'echo':
                terminal.xterm.writeln(args.slice(1).join(' '));
                break;
            case 'exit':
                this.closeTerminal(terminalId);
                return;
            default:
                terminal.xterm.writeln(`Command not found: ${cmd}`);
        }
    }

    clearTerminal(terminalId) {
        const terminal = this.terminals.get(terminalId);
        if (terminal && terminal.xterm) {
            terminal.xterm.clear();
            if (!terminal.connectionId) {
                terminal.xterm.write('$ ');
            }
        }
    }

    copyTerminalSelection(terminalId) {
        const terminal = this.terminals.get(terminalId);
        if (terminal && terminal.xterm) {
            const selection = terminal.xterm.getSelection();
            if (selection) {
                navigator.clipboard.writeText(selection).catch(console.error);
            }
        }
    }

    async pasteToTerminal(terminalId) {
        const terminal = this.terminals.get(terminalId);
        if (terminal && terminal.xterm) {
            try {
                const text = await navigator.clipboard.readText();
                terminal.xterm.paste(text);
            } catch (error) {
                console.error('Failed to paste:', error);
            }
        }
    }

    updateCopyButtonState(terminalId) {
        const terminal = this.terminals.get(terminalId);
        if (terminal && terminal.xterm) {
            const hasSelection = terminal.xterm.hasSelection();
            const copyBtn = terminal.element.querySelector('.terminal-toolbar button[title="Copy Selection"]');
            if (copyBtn) {
                copyBtn.disabled = !hasSelection;
            }
        }
    }

    showTerminalSettings(terminalId) {
        // TODO: Implement terminal settings panel
        console.log('Terminal settings for:', terminalId);
    }

    // Navigation methods
    nextTerminal() {
        const terminalIds = Array.from(this.terminals.keys());
        if (terminalIds.length <= 1) return;
        
        const currentIndex = terminalIds.indexOf(this.activeTerminalId);
        const nextIndex = (currentIndex + 1) % terminalIds.length;
        this.setActiveTerminal(terminalIds[nextIndex]);
    }

    previousTerminal() {
        const terminalIds = Array.from(this.terminals.keys());
        if (terminalIds.length <= 1) return;
        
        const currentIndex = terminalIds.indexOf(this.activeTerminalId);
        const prevIndex = currentIndex === 0 ? terminalIds.length - 1 : currentIndex - 1;
        this.setActiveTerminal(terminalIds[prevIndex]);
    }

    // Public API methods
    async createLocalTerminal() {
        return this.createTerminal();
    }

    async createSSHTerminal(connectionId, title) {
        return this.createTerminal(connectionId, title);
    }

    async closeActiveTerminal() {
        if (this.activeTerminalId) {
            await this.closeTerminal(this.activeTerminalId);
        }
    }

    getActiveTerminal() {
        return this.terminals.get(this.activeTerminalId);
    }

    listTerminals() {
        return Array.from(this.terminals.values());
    }

    // Event emitter methods
    on(event, callback) {
        return super.on(event, callback);
    }

    emit(event, data) {
        return super.emit(event, data);
    }
}

// Export for module usage
export default TerminalManager;
