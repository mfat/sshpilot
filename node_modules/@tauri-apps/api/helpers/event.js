import { invokeTauriCommand } from './tauri.js';
import { transformCallback } from '../tauri.js';

// Copyright 2019-2023 Tauri Programme within The Commons Conservancy
// SPDX-License-Identifier: Apache-2.0
// SPDX-License-Identifier: MIT
/**
 * Unregister the event listener associated with the given name and id.
 *
 * @ignore
 * @param event The event name
 * @param eventId Event identifier
 * @returns
 */
async function _unlisten(event, eventId) {
    return invokeTauriCommand({
        __tauriModule: 'Event',
        message: {
            cmd: 'unlisten',
            event,
            eventId
        }
    });
}
/**
 * Emits an event to the backend.
 *
 * @param event Event name. Must include only alphanumeric characters, `-`, `/`, `:` and `_`.
 * @param [windowLabel] The label of the window to which the event is sent, if null/undefined the event will be sent to all windows
 * @param [payload] Event payload
 * @returns
 */
async function emit(event, windowLabel, payload) {
    await invokeTauriCommand({
        __tauriModule: 'Event',
        message: {
            cmd: 'emit',
            event,
            windowLabel,
            payload
        }
    });
}
/**
 * Listen to an event from the backend.
 *
 * @param event Event name. Must include only alphanumeric characters, `-`, `/`, `:` and `_`.
 * @param handler Event handler callback.
 * @return A promise resolving to a function to unlisten to the event.
 */
async function listen(event, windowLabel, handler) {
    return invokeTauriCommand({
        __tauriModule: 'Event',
        message: {
            cmd: 'listen',
            event,
            windowLabel,
            handler: transformCallback(handler)
        }
    }).then((eventId) => {
        return async () => _unlisten(event, eventId);
    });
}
/**
 * Listen to an one-off event from the backend.
 *
 * @param event Event name. Must include only alphanumeric characters, `-`, `/`, `:` and `_`.
 * @param handler Event handler callback.
 * @returns A promise resolving to a function to unlisten to the event.
 */
async function once(event, windowLabel, handler) {
    return listen(event, windowLabel, (eventData) => {
        handler(eventData);
        _unlisten(event, eventData.id).catch(() => { });
    });
}

export { emit, listen, once };
