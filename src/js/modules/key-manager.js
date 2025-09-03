// SSH Key Manager Module
import { invoke } from '@tauri-apps/api/tauri';

export class KeyManager {
    constructor() {
        this.keys = new Map();
        this.isInitialized = false;
    }

    async initialize() {
        try {
            console.log('Initializing Key Manager...');
            
            // Load SSH keys from Tauri backend
            await this.loadKeys();
            
            this.isInitialized = true;
            console.log('Key Manager initialized');
            
        } catch (error) {
            console.error('Failed to initialize Key Manager:', error);
            throw error;
        }
    }

    async loadKeys() {
        try {
            const keys = await invoke('list_keys');
            this.keys.clear();
            
            keys.forEach(key => {
                this.keys.set(key.id, {
                    ...key,
                    created_at: new Date(key.created_at),
                    last_used: key.last_used ? new Date(key.last_used) : null
                });
            });
            
            console.log(`Loaded ${this.keys.size} SSH keys`);
            
        } catch (error) {
            console.error('Failed to load SSH keys:', error);
            throw error;
        }
    }

    async generateKey(keyData) {
        try {
            const key = await invoke('generate_key', {
                name: keyData.name,
                key_type: keyData.key_type,
                key_size: keyData.key_size || null,
                comment: keyData.comment || null,
                passphrase: keyData.passphrase || null
            });
            
            // Add to local cache
            const newKey = {
                ...key,
                created_at: new Date(key.created_at),
                last_used: key.last_used ? new Date(key.last_used) : null
            };
            
            this.keys.set(key.id, newKey);
            
            console.log('SSH key generated:', key.id);
            return key.id;
            
        } catch (error) {
            console.error('Failed to generate SSH key:', error);
            throw error;
        }
    }

    async deleteKey(keyId) {
        try {
            await invoke('delete_key', { key_id: keyId });
            
            // Remove from local cache
            this.keys.delete(keyId);
            
            console.log('SSH key deleted:', keyId);
            
        } catch (error) {
            console.error('Failed to delete SSH key:', error);
            throw error;
        }
    }

    async importKey(privatePath, publicPath = null) {
        try {
            const key = await invoke('import_key', {
                private_path: privatePath,
                public_path: publicPath
            });
            
            // Add to local cache
            const importedKey = {
                ...key,
                created_at: new Date(key.created_at),
                last_used: key.last_used ? new Date(key.last_used) : null
            };
            
            this.keys.set(key.id, importedKey);
            
            console.log('SSH key imported:', key.id);
            return key.id;
            
        } catch (error) {
            console.error('Failed to import SSH key:', error);
            throw error;
        }
    }

    getKey(keyId) {
        return this.keys.get(keyId);
    }

    getAllKeys() {
        return Array.from(this.keys.values());
    }

    getKeysByType(keyType) {
        return Array.from(this.keys.values()).filter(key => key.key_type === keyType);
    }

    getKeysWithPassphrase() {
        return Array.from(this.keys.values()).filter(key => key.has_passphrase);
    }

    getKeysWithoutPassphrase() {
        return Array.from(this.keys.values()).filter(key => !key.has_passphrase);
    }

    searchKeys(query) {
        if (!query || query.trim() === '') {
            return this.getAllKeys();
        }

        const lowerQuery = query.toLowerCase();
        return this.getAllKeys().filter(key => {
            return key.name.toLowerCase().includes(lowerQuery) ||
                   key.key_type.toLowerCase().includes(lowerQuery) ||
                   (key.comment && key.comment.toLowerCase().includes(lowerQuery));
        });
    }

    // Key validation
    validateKeyName(name) {
        if (!name || name.trim() === '') {
            return { valid: false, error: 'Key name is required' };
        }
        
        if (name.includes('/') || name.includes('\\')) {
            return { valid: false, error: 'Key name cannot contain path separators' };
        }
        
        if (name.startsWith('.')) {
            return { valid: false, error: 'Key name cannot start with a dot' };
        }
        
        if (this.keys.has(name)) {
            return { valid: false, error: 'Key name already exists' };
        }
        
        return { valid: true };
    }

    validateKeyType(keyType) {
        const validTypes = ['ed25519', 'rsa', 'ecdsa', 'dsa'];
        if (!validTypes.includes(keyType)) {
            return { valid: false, error: 'Invalid key type' };
        }
        
        return { valid: true };
    }

    validateKeySize(keyType, keySize) {
        if (keyType === 'rsa') {
            if (!keySize || keySize < 1024 || keySize > 8192) {
                return { valid: false, error: 'RSA key size must be between 1024 and 8192 bits' };
            }
        }
        
        return { valid: true };
    }

    // Key utilities
    getKeyTypeDisplayName(keyType) {
        const displayNames = {
            'ed25519': 'Ed25519 (Recommended)',
            'rsa': 'RSA',
            'ecdsa': 'ECDSA',
            'dsa': 'DSA'
        };
        
        return displayNames[keyType] || keyType;
    }

    getKeyTypeDescription(keyType) {
        const descriptions = {
            'ed25519': 'Modern, secure, and fast. Recommended for most use cases.',
            'rsa': 'Widely supported but requires larger key sizes for security.',
            'ecdsa': 'Good security with smaller key sizes than RSA.',
            'dsa': 'Legacy algorithm, not recommended for new keys.'
        };
        
        return descriptions[keyType] || 'No description available.';
    }

    getRecommendedKeySize(keyType) {
        const recommendations = {
            'ed25519': null, // Ed25519 has fixed size
            'rsa': 3072,
            'ecdsa': 256,
            'dsa': 1024
        };
        
        return recommendations[keyType];
    }

    // Key file operations
    async getKeyFileContents(keyId, fileType = 'public') {
        try {
            const key = this.keys.get(keyId);
            if (!key) {
                throw new Error('Key not found');
            }
            
            const filePath = fileType === 'public' ? key.public_path : key.private_path;
            
            // In a real implementation, you'd read the file contents
            // For now, we'll return a placeholder
            return `# ${fileType.toUpperCase()} key file for ${key.name}\n# This is a placeholder - actual file contents would be loaded here`;
            
        } catch (error) {
            console.error('Failed to get key file contents:', error);
            throw error;
        }
    }

    async copyKeyToClipboard(keyId, fileType = 'public') {
        try {
            const contents = await this.getKeyFileContents(keyId, fileType);
            
            // Copy to clipboard
            await navigator.clipboard.writeText(contents);
            
            console.log(`Key ${fileType} copied to clipboard`);
            return true;
            
        } catch (error) {
            console.error('Failed to copy key to clipboard:', error);
            throw error;
        }
    }

    // Key statistics
    getKeyStatistics() {
        const totalKeys = this.keys.size;
        const keysWithPassphrase = this.getKeysWithPassphrase().length;
        const keysWithoutPassphrase = this.getKeysWithoutPassphrase().length;
        
        const typeBreakdown = {};
        this.keys.forEach(key => {
            typeBreakdown[key.key_type] = (typeBreakdown[key.key_type] || 0) + 1;
        });
        
        return {
            total: totalKeys,
            withPassphrase: keysWithPassphrase,
            withoutPassphrase: keysWithoutPassphrase,
            typeBreakdown,
            recentKeys: this.getRecentKeys(5)
        };
    }

    getRecentKeys(count = 5) {
        return Array.from(this.keys.values())
            .sort((a, b) => b.last_used - a.last_used)
            .slice(0, count);
    }

    // Key backup and restore
    async exportKeyData() {
        try {
            const keyData = Array.from(this.keys.values()).map(key => ({
                id: key.id,
                name: key.name,
                key_type: key.key_type,
                key_size: key.key_size,
                comment: key.comment,
                has_passphrase: key.has_passphrase,
                created_at: key.created_at.toISOString(),
                last_used: key.last_used ? key.last_used.toISOString() : null
            }));
            
            return JSON.stringify(keyData, null, 2);
            
        } catch (error) {
            console.error('Failed to export key data:', error);
            throw error;
        }
    }

    // Event emitters
    emitKeyAdded(key) {
        const event = new CustomEvent('keyAdded', { detail: { key } });
        document.dispatchEvent(event);
    }

    emitKeyRemoved(keyId) {
        const event = new CustomEvent('keyRemoved', { detail: { keyId } });
        document.dispatchEvent(event);
    }

    emitKeyUpdated(key) {
        const event = new CustomEvent('keyUpdated', { detail: { key } });
        document.dispatchEvent(event);
    }

    // Event listeners for external use
    onKeyAdded(callback) {
        document.addEventListener('keyAdded', callback);
    }

    onKeyRemoved(callback) {
        document.addEventListener('keyRemoved', callback);
    }

    onKeyUpdated(callback) {
        document.addEventListener('keyUpdated', callback);
    }

    // Utility methods
    getKeyCount() {
        return this.keys.size;
    }

    getKeyCountByType(keyType) {
        return this.getKeysByType(keyType).length;
    }

    hasKeys() {
        return this.keys.size > 0;
    }

    // Cleanup
    destroy() {
        this.keys.clear();
        this.isInitialized = false;
    }
}

