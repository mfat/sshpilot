// Notification Manager Module
export class NotificationManager {
    constructor() {
        this.notifications = new Map();
        this.notificationCounter = 0;
        this.defaultDuration = 5000; // 5 seconds
        this.maxNotifications = 5;
        this.isInitialized = false;
    }

    async initialize() {
        try {
            console.log('Initializing Notification Manager...');
            
            // Create notification container if it doesn't exist
            this.createNotificationContainer();
            
            // Set up event listeners
            this.setupEventListeners();
            
            this.isInitialized = true;
            console.log('Notification Manager initialized');
            
        } catch (error) {
            console.error('Failed to initialize Notification Manager:', error);
            throw error;
        }
    }

    createNotificationContainer() {
        // Check if container already exists
        if (document.getElementById('notification-container')) {
            return;
        }
        
        const container = document.createElement('div');
        container.id = 'notification-container';
        container.className = 'notification-container';
        
        // Add styles
        container.style.cssText = `
            position: fixed;
            top: 20px;
            right: 20px;
            z-index: 10000;
            max-width: 400px;
            pointer-events: none;
        `;
        
        document.body.appendChild(container);
    }

    setupEventListeners() {
        // Listen for notification events
        document.addEventListener('showNotification', (e) => {
            this.show(e.detail.message, e.detail.type, e.detail.duration);
        });
    }

    show(message, type = 'info', duration = null) {
        try {
            // Create notification element
            const notification = this.createNotificationElement(message, type);
            
            // Add to container
            const container = document.getElementById('notification-container');
            if (container) {
                container.appendChild(notification);
                
                // Trigger animation
                requestAnimationFrame(() => {
                    notification.classList.add('show');
                });
                
                // Set up auto-remove
                const autoDuration = duration !== null ? duration : this.defaultDuration;
                if (autoDuration > 0) {
                    setTimeout(() => {
                        this.remove(notification.id);
                    }, autoDuration);
                }
                
                // Store notification reference
                this.notifications.set(notification.id, notification);
                
                // Limit number of notifications
                this.limitNotifications();
                
                console.log(`Notification shown: ${message} (${type})`);
            }
            
        } catch (error) {
            console.error('Failed to show notification:', error);
        }
    }

    createNotificationElement(message, type) {
        const notification = document.createElement('div');
        const id = `notification-${++this.notificationCounter}`;
        
        notification.id = id;
        notification.className = `notification notification-${type}`;
        notification.style.cssText = `
            background-color: var(--card-bg);
            color: var(--card-fg);
            border: 1px solid var(--border-color);
            border-radius: var(--border-radius-md);
            padding: var(--spacing-md) var(--spacing-lg);
            margin-bottom: var(--spacing-sm);
            box-shadow: var(--shadow-lg);
            border-left: 4px solid var(--${type}-color, var(--accent-color));
            transform: translateX(100%);
            opacity: 0;
            transition: all var(--transition-normal);
            pointer-events: auto;
            cursor: pointer;
            max-width: 100%;
            word-wrap: break-word;
        `;
        
        // Set border color based on type
        const borderColors = {
            'success': 'var(--success-color)',
            'warning': 'var(--warning-color)',
            'error': 'var(--error-color)',
            'info': 'var(--info-color)'
        };
        
        notification.style.borderLeftColor = borderColors[type] || 'var(--accent-color)';
        
        // Create notification content
        notification.innerHTML = `
            <div class="notification-content">
                <div class="notification-message">${this.escapeHtml(message)}</div>
                <button class="notification-close" title="Close">×</button>
            </div>
        `;
        
        // Add event listeners
        this.setupNotificationEventListeners(notification);
        
        return notification;
    }

    setupNotificationEventListeners(notification) {
        // Click to dismiss
        notification.addEventListener('click', (e) => {
            if (!e.target.classList.contains('notification-close')) {
                this.remove(notification.id);
            }
        });
        
        // Close button
        const closeBtn = notification.querySelector('.notification-close');
        if (closeBtn) {
            closeBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                this.remove(notification.id);
            });
        }
        
        // Hover effects
        notification.addEventListener('mouseenter', () => {
            notification.style.transform = 'translateX(0) scale(1.02)';
        });
        
        notification.addEventListener('mouseleave', () => {
            notification.style.transform = 'translateX(0) scale(1)';
        });
    }

    remove(notificationId) {
        const notification = this.notifications.get(notificationId);
        if (!notification) return;
        
        try {
            // Add hide animation
            notification.classList.add('hiding');
            notification.style.transform = 'translateX(100%)';
            notification.style.opacity = '0';
            
            // Remove after animation
            setTimeout(() => {
                if (notification.parentNode) {
                    notification.parentNode.removeChild(notification);
                }
                this.notifications.delete(notificationId);
            }, 300);
            
        } catch (error) {
            console.error('Failed to remove notification:', error);
            // Force remove if animation fails
            if (notification.parentNode) {
                notification.parentNode.removeChild(notification);
            }
            this.notifications.delete(notificationId);
        }
    }

    removeAll() {
        this.notifications.forEach((_, notificationId) => {
            this.remove(notificationId);
        });
    }

    limitNotifications() {
        if (this.notifications.size > this.maxNotifications) {
            const oldestNotification = this.notifications.values().next().value;
            if (oldestNotification) {
                this.remove(oldestNotification.id);
            }
        }
    }

    // Convenience methods for different notification types
    success(message, duration = null) {
        this.show(message, 'success', duration);
    }

    warning(message, duration = null) {
        this.show(message, 'warning', duration);
    }

    error(message, duration = null) {
        this.show(message, 'error', duration);
    }

    info(message, duration = null) {
        this.show(message, 'info', duration);
    }

    // Progress notifications
    showProgress(message, progress = 0) {
        const notification = this.createProgressNotification(message, progress);
        
        const container = document.getElementById('notification-container');
        if (container) {
            container.appendChild(notification);
            requestAnimationFrame(() => {
                notification.classList.add('show');
            });
            
            this.notifications.set(notification.id, notification);
            this.limitNotifications();
        }
        
        return notification.id;
    }

    createProgressNotification(message, progress) {
        const notification = document.createElement('div');
        const id = `notification-${++this.notificationCounter}`;
        
        notification.id = id;
        notification.className = 'notification notification-progress';
        notification.style.cssText = `
            background-color: var(--card-bg);
            color: var(--card-fg);
            border: 1px solid var(--border-color);
            border-radius: var(--border-radius-md);
            padding: var(--spacing-md) var(--spacing-lg);
            margin-bottom: var(--spacing-sm);
            box-shadow: var(--shadow-lg);
            border-left: 4px solid var(--accent-color);
            transform: translateX(100%);
            opacity: 0;
            transition: all var(--transition-normal);
            pointer-events: auto;
            max-width: 100%;
            word-wrap: break-word;
        `;
        
        notification.innerHTML = `
            <div class="notification-content">
                <div class="notification-message">${this.escapeHtml(message)}</div>
                <div class="notification-progress-bar">
                    <div class="notification-progress-fill" style="width: ${progress}%"></div>
                </div>
                <button class="notification-close" title="Close">×</button>
            </div>
        `;
        
        // Add progress bar styles
        const progressBar = notification.querySelector('.notification-progress-bar');
        const progressFill = notification.querySelector('.notification-progress-fill');
        
        if (progressBar && progressFill) {
            progressBar.style.cssText = `
                width: 100%;
                height: 4px;
                background-color: var(--border-color);
                border-radius: 2px;
                margin: var(--spacing-sm) 0;
                overflow: hidden;
            `;
            
            progressFill.style.cssText = `
                height: 100%;
                background-color: var(--accent-color);
                border-radius: 2px;
                transition: width var(--transition-normal);
            `;
        }
        
        // Add event listeners
        this.setupNotificationEventListeners(notification);
        
        return notification;
    }

    updateProgress(notificationId, progress) {
        const notification = this.notifications.get(notificationId);
        if (!notification) return;
        
        const progressFill = notification.querySelector('.notification-progress-fill');
        if (progressFill) {
            progressFill.style.width = `${Math.min(100, Math.max(0, progress))}%`;
        }
    }

    completeProgress(notificationId, message = null) {
        const notification = this.notifications.get(notificationId);
        if (!notification) return;
        
        if (message) {
            const messageElement = notification.querySelector('.notification-message');
            if (messageElement) {
                messageElement.textContent = message;
            }
        }
        
        // Change to success notification
        notification.className = 'notification notification-success';
        notification.style.borderLeftColor = 'var(--success-color)';
        
        // Remove progress bar
        const progressBar = notification.querySelector('.notification-progress-bar');
        if (progressBar) {
            progressBar.remove();
        }
        
        // Auto-remove after delay
        setTimeout(() => {
            this.remove(notificationId);
        }, 3000);
    }

    // Toast notifications (simpler, shorter)
    toast(message, type = 'info') {
        this.show(message, type, 3000); // 3 seconds for toasts
    }

    // Success toast
    successToast(message) {
        this.toast(message, 'success');
    }

    // Error toast
    errorToast(message) {
        this.toast(message, 'error');
    }

    // Warning toast
    warningToast(message) {
        this.toast(message, 'warning');
    }

    // Info toast
    infoToast(message) {
        this.toast(message, 'info');
    }

    // Utility methods
    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    getNotificationCount() {
        return this.notifications.size;
    }

    hasNotifications() {
        return this.notifications.size > 0;
    }

    // Event emitters
    emitNotificationShown(notificationId, message, type) {
        const event = new CustomEvent('notificationShown', {
            detail: { notificationId, message, type }
        });
        document.dispatchEvent(event);
    }

    emitNotificationRemoved(notificationId) {
        const event = new CustomEvent('notificationRemoved', {
            detail: { notificationId }
        });
        document.dispatchEvent(event);
    }

    // Event listeners for external use
    onNotificationShown(callback) {
        document.addEventListener('notificationShown', callback);
    }

    onNotificationRemoved(callback) {
        document.addEventListener('notificationRemoved', callback);
    }

    // Cleanup
    destroy() {
        this.removeAll();
        this.notifications.clear();
        this.isInitialized = false;
    }
}

