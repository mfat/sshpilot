// Theme Manager Module
import { invoke } from '@tauri-apps/api/tauri';

export class ThemeManager {
    constructor() {
        this.currentTheme = 'system';
        this.availableThemes = [
            'system',
            'light',
            'dark',
            'high-contrast',
            'solarized-light',
            'solarized-dark',
            'dracula',
            'nord'
        ];
        this.isInitialized = false;
    }

    async initialize() {
        try {
            console.log('Initializing Theme Manager...');
            
            // Load saved theme preference
            await this.loadThemePreference();
            
            // Apply theme
            this.applyTheme(this.currentTheme);
            
            // Set up theme change listener
            this.setupThemeChangeListener();
            
            this.isInitialized = true;
            console.log('Theme Manager initialized');
            
        } catch (error) {
            console.error('Failed to initialize Theme Manager:', error);
            // Fall back to system theme
            this.currentTheme = 'system';
            this.applyTheme('system');
        }
    }

    async loadThemePreference() {
        try {
            const savedTheme = await invoke('get_setting', { key: 'theme' });
            if (savedTheme && this.availableThemes.includes(savedTheme)) {
                this.currentTheme = savedTheme;
            } else {
                // Default to system theme
                this.currentTheme = 'system';
            }
        } catch (error) {
            console.error('Failed to load theme preference:', error);
            this.currentTheme = 'system';
        }
    }

    async saveThemePreference(theme) {
        try {
            await invoke('set_setting', { key: 'theme', value: theme });
        } catch (error) {
            console.error('Failed to save theme preference:', error);
        }
    }

    setupThemeChangeListener() {
        // Listen for system theme changes
        if (window.matchMedia) {
            const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)');
            mediaQuery.addEventListener('change', (e) => {
                if (this.currentTheme === 'system') {
                    this.applySystemTheme();
                }
            });
        }
    }

    async setTheme(theme) {
        if (!this.availableThemes.includes(theme)) {
            throw new Error(`Invalid theme: ${theme}`);
        }

        try {
            // Save theme preference
            await this.saveThemePreference(theme);
            
            // Update current theme
            this.currentTheme = theme;
            
            // Apply theme
            this.applyTheme(theme);
            
            // Emit theme change event
            this.emitThemeChanged(theme);
            
            console.log('Theme changed to:', theme);
            
        } catch (error) {
            console.error('Failed to set theme:', error);
            throw error;
        }
    }

    applyTheme(theme) {
        // Remove all theme classes
        document.documentElement.removeAttribute('data-theme');
        document.documentElement.classList.remove(...this.availableThemes);
        
        if (theme === 'system') {
            this.applySystemTheme();
        } else {
            // Apply specific theme
            document.documentElement.setAttribute('data-theme', theme);
            document.documentElement.classList.add(theme);
        }
        
        // Update CSS custom properties for terminal colors
        this.updateTerminalColors(theme);
    }

    applySystemTheme() {
        if (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) {
            document.documentElement.setAttribute('data-theme', 'dark');
        } else {
            document.documentElement.setAttribute('data-theme', 'light');
        }
    }

    updateTerminalColors(theme) {
        const root = document.documentElement;
        
        // Define terminal color schemes for different themes
        const terminalColors = {
            'light': {
                '--terminal-bg': '#ffffff',
                '--terminal-fg': '#1c1c1c',
                '--terminal-cursor': '#1c1c1c',
                '--terminal-prompt': '#0066cc',
                '--terminal-command': '#1c1c1c',
                '--terminal-stdout': '#1c1c1c',
                '--terminal-stderr': '#cc0000',
                '--terminal-error': '#cc0000'
            },
            'dark': {
                '--terminal-bg': '#000000',
                '--terminal-fg': '#ffffff',
                '--terminal-cursor': '#ffffff',
                '--terminal-prompt': '#00ff00',
                '--terminal-command': '#ffffff',
                '--terminal-stdout': '#ffffff',
                '--terminal-stderr': '#ff6b6b',
                '--terminal-error': '#ff6b6b'
            },
            'solarized-light': {
                '--terminal-bg': '#fdf6e3',
                '--terminal-fg': '#586e75',
                '--terminal-cursor': '#586e75',
                '--terminal-prompt': '#859900',
                '--terminal-command': '#586e75',
                '--terminal-stdout': '#586e75',
                '--terminal-stderr': '#dc322f',
                '--terminal-error': '#dc322f'
            },
            'solarized-dark': {
                '--terminal-bg': '#002b36',
                '--terminal-fg': '#839496',
                '--terminal-cursor': '#839496',
                '--terminal-prompt': '#859900',
                '--terminal-command': '#839496',
                '--terminal-stdout': '#839496',
                '--terminal-stderr': '#dc322f',
                '--terminal-error': '#dc322f'
            },
            'dracula': {
                '--terminal-bg': '#282a36',
                '--terminal-fg': '#f8f8f2',
                '--terminal-cursor': '#f8f8f2',
                '--terminal-prompt': '#50fa7b',
                '--terminal-command': '#f8f8f2',
                '--terminal-stdout': '#f8f8f2',
                '--terminal-stderr': '#ff5555',
                '--terminal-error': '#ff5555'
            },
            'nord': {
                '--terminal-bg': '#2e3440',
                '--terminal-fg': '#eceff4',
                '--terminal-cursor': '#eceff4',
                '--terminal-prompt': '#a3be8c',
                '--terminal-command': '#eceff4',
                '--terminal-stdout': '#eceff4',
                '--terminal-stderr': '#bf616a',
                '--terminal-error': '#bf616a'
            },
            'high-contrast': {
                '--terminal-bg': '#000000',
                '--terminal-fg': '#ffffff',
                '--terminal-cursor': '#ffffff',
                '--terminal-prompt': '#ffff00',
                '--terminal-command': '#ffffff',
                '--terminal-stdout': '#ffffff',
                '--terminal-stderr': '#ff0000',
                '--terminal-error': '#ff0000'
            }
        };
        
        // Apply terminal colors
        if (terminalColors[theme]) {
            Object.entries(terminalColors[theme]).forEach(([property, value]) => {
                root.style.setProperty(property, value);
            });
        }
    }

    getCurrentTheme() {
        return this.currentTheme;
    }

    getAvailableThemes() {
        return [...this.availableThemes];
    }

    getThemeDisplayName(theme) {
        const displayNames = {
            'system': 'System Default',
            'light': 'Light',
            'dark': 'Dark',
            'high-contrast': 'High Contrast',
            'solarized-light': 'Solarized Light',
            'solarized-dark': 'Solarized Dark',
            'dracula': 'Dracula',
            'nord': 'Nord'
        };
        
        return displayNames[theme] || theme;
    }

    getThemeDescription(theme) {
        const descriptions = {
            'system': 'Automatically follows your system theme preference',
            'light': 'Clean, bright interface for well-lit environments',
            'dark': 'Easy on the eyes for low-light conditions',
            'high-contrast': 'Maximum contrast for accessibility',
            'solarized-light': 'Carefully selected colors for reduced eye strain',
            'solarized-dark': 'Dark variant of the Solarized color scheme',
            'dracula': 'Dark theme with vibrant accent colors',
            'nord': 'Arctic-inspired color palette with cool tones'
        };
        
        return descriptions[theme] || 'No description available.';
    }

    getThemePreview(theme) {
        // Return CSS color values for theme preview
        const previewColors = {
            'system': { bg: '#fafafa', fg: '#1c1c1c', accent: '#3584e4' },
            'light': { bg: '#ffffff', fg: '#1c1c1c', accent: '#3584e4' },
            'dark': { bg: '#1c1c1c', fg: '#ffffff', accent: '#3584e4' },
            'high-contrast': { bg: '#000000', fg: '#ffffff', accent: '#ffff00' },
            'solarized-light': { bg: '#fdf6e3', fg: '#586e75', accent: '#268bd2' },
            'solarized-dark': { bg: '#002b36', fg: '#839496', accent: '#268bd2' },
            'dracula': { bg: '#282a36', fg: '#f8f8f2', accent: '#bd93f9' },
            'nord': { bg: '#2e3440', fg: '#eceff4', accent: '#88c0d0' }
        };
        
        return previewColors[theme] || previewColors['system'];
    }

    // Theme cycling
    async cycleTheme() {
        const currentIndex = this.availableThemes.indexOf(this.currentTheme);
        const nextIndex = (currentIndex + 1) % this.availableThemes.length;
        const nextTheme = this.availableThemes[nextIndex];
        
        await this.setTheme(nextTheme);
        return nextTheme;
    }

    async previousTheme() {
        const currentIndex = this.availableThemes.indexOf(this.currentTheme);
        const prevIndex = currentIndex === 0 ? this.availableThemes.length - 1 : currentIndex - 1;
        const prevTheme = this.availableThemes[prevIndex];
        
        await this.setTheme(prevTheme);
        return prevTheme;
    }

    // Theme validation
    isValidTheme(theme) {
        return this.availableThemes.includes(theme);
    }

    // Theme import/export
    async exportThemeSettings() {
        try {
            const themeData = {
                currentTheme: this.currentTheme,
                availableThemes: this.availableThemes,
                timestamp: new Date().toISOString()
            };
            
            return JSON.stringify(themeData, null, 2);
            
        } catch (error) {
            console.error('Failed to export theme settings:', error);
            throw error;
        }
    }

    async importThemeSettings(themeData) {
        try {
            const parsed = JSON.parse(themeData);
            
            if (parsed.currentTheme && this.isValidTheme(parsed.currentTheme)) {
                await this.setTheme(parsed.currentTheme);
                return true;
            }
            
            return false;
            
        } catch (error) {
            console.error('Failed to import theme settings:', error);
            throw error;
        }
    }

    // Accessibility features
    isHighContrastTheme() {
        return this.currentTheme === 'high-contrast';
    }

    isDarkTheme() {
        if (this.currentTheme === 'system') {
            return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
        }
        return ['dark', 'solarized-dark', 'dracula', 'nord'].includes(this.currentTheme);
    }

    isLightTheme() {
        if (this.currentTheme === 'system') {
            return window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches;
        }
        return ['light', 'solarized-light'].includes(this.currentTheme);
    }

    // Event emitters
    emitThemeChanged(theme) {
        const event = new CustomEvent('themeChanged', { detail: { theme } });
        document.dispatchEvent(event);
    }

    // Event listeners for external use
    onThemeChanged(callback) {
        document.addEventListener('themeChanged', callback);
    }

    // Utility methods
    getThemeInfo(theme) {
        return {
            name: theme,
            displayName: this.getThemeDisplayName(theme),
            description: this.getThemeDescription(theme),
            preview: this.getThemePreview(theme),
            isDark: this.isDarkTheme(),
            isHighContrast: this.isHighContrastTheme()
        };
    }

    // Cleanup
    destroy() {
        this.isInitialized = false;
    }
}

