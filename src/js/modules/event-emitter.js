/**
 * Simple Event Emitter implementation
 * Provides basic event handling functionality for modules
 */
class EventEmitter {
    constructor() {
        this.events = new Map();
    }

    /**
     * Register an event listener
     * @param {string} event - Event name
     * @param {Function} callback - Callback function
     * @returns {Function} - Unsubscribe function
     */
    on(event, callback) {
        if (!this.events.has(event)) {
            this.events.set(event, []);
        }
        
        this.events.get(event).push(callback);
        
        // Return unsubscribe function
        return () => {
            const callbacks = this.events.get(event);
            if (callbacks) {
                const index = callbacks.indexOf(callback);
                if (index > -1) {
                    callbacks.splice(index, 1);
                }
            }
        };
    }

    /**
     * Register a one-time event listener
     * @param {string} event - Event name
     * @param {Function} callback - Callback function
     */
    once(event, callback) {
        const onceCallback = (...args) => {
            callback(...args);
            this.off(event, onceCallback);
        };
        
        this.on(event, onceCallback);
    }

    /**
     * Remove an event listener
     * @param {string} event - Event name
     * @param {Function} callback - Callback function to remove
     */
    off(event, callback) {
        const callbacks = this.events.get(event);
        if (callbacks) {
            const index = callbacks.indexOf(callback);
            if (index > -1) {
                callbacks.splice(index, 1);
            }
        }
    }

    /**
     * Remove all listeners for an event
     * @param {string} event - Event name
     */
    removeAllListeners(event) {
        if (event) {
            this.events.delete(event);
        } else {
            this.events.clear();
        }
    }

    /**
     * Emit an event
     * @param {string} event - Event name
     * @param {...any} args - Arguments to pass to callbacks
     * @returns {boolean} - True if event had listeners
     */
    emit(event, ...args) {
        const callbacks = this.events.get(event);
        if (callbacks && callbacks.length > 0) {
            callbacks.forEach(callback => {
                try {
                    callback(...args);
                } catch (error) {
                    console.error(`Error in event listener for ${event}:`, error);
                }
            });
            return true;
        }
        return false;
    }

    /**
     * Get the number of listeners for an event
     * @param {string} event - Event name
     * @returns {number} - Number of listeners
     */
    listenerCount(event) {
        const callbacks = this.events.get(event);
        return callbacks ? callbacks.length : 0;
    }

    /**
     * Get all event names that have listeners
     * @returns {string[]} - Array of event names
     */
    eventNames() {
        return Array.from(this.events.keys());
    }
}

export default EventEmitter;

