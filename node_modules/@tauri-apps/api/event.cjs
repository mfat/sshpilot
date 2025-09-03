'use strict';

var event = require('./helpers/event.cjs');

// Copyright 2019-2023 Tauri Programme within The Commons Conservancy
// SPDX-License-Identifier: Apache-2.0
// SPDX-License-Identifier: MIT
/**
 * The event system allows you to emit events to the backend and listen to events from it.
 *
 * This package is also accessible with `window.__TAURI__.event` when [`build.withGlobalTauri`](https://tauri.app/v1/api/config/#buildconfig.withglobaltauri) in `tauri.conf.json` is set to `true`.
 * @module
 */
/**
 * @since 1.1.0
 */
exports.TauriEvent = void 0;
(function (TauriEvent) {
    TauriEvent["WINDOW_RESIZED"] = "tauri://resize";
    TauriEvent["WINDOW_MOVED"] = "tauri://move";
    TauriEvent["WINDOW_CLOSE_REQUESTED"] = "tauri://close-requested";
    TauriEvent["WINDOW_CREATED"] = "tauri://window-created";
    TauriEvent["WINDOW_DESTROYED"] = "tauri://destroyed";
    TauriEvent["WINDOW_FOCUS"] = "tauri://focus";
    TauriEvent["WINDOW_BLUR"] = "tauri://blur";
    TauriEvent["WINDOW_SCALE_FACTOR_CHANGED"] = "tauri://scale-change";
    TauriEvent["WINDOW_THEME_CHANGED"] = "tauri://theme-changed";
    TauriEvent["WINDOW_FILE_DROP"] = "tauri://file-drop";
    TauriEvent["WINDOW_FILE_DROP_HOVER"] = "tauri://file-drop-hover";
    TauriEvent["WINDOW_FILE_DROP_CANCELLED"] = "tauri://file-drop-cancelled";
    TauriEvent["MENU"] = "tauri://menu";
    TauriEvent["CHECK_UPDATE"] = "tauri://update";
    TauriEvent["UPDATE_AVAILABLE"] = "tauri://update-available";
    TauriEvent["INSTALL_UPDATE"] = "tauri://update-install";
    TauriEvent["STATUS_UPDATE"] = "tauri://update-status";
    TauriEvent["DOWNLOAD_PROGRESS"] = "tauri://update-download-progress";
})(exports.TauriEvent || (exports.TauriEvent = {}));
/**
 * Listen to an event. The event can be either global or window-specific.
 * See {@link Event.windowLabel} to check the event source.
 *
 * @example
 * ```typescript
 * import { listen } from '@tauri-apps/api/event';
 * const unlisten = await listen<string>('error', (event) => {
 *   console.log(`Got error in window ${event.windowLabel}, payload: ${event.payload}`);
 * });
 *
 * // you need to call unlisten if your handler goes out of scope e.g. the component is unmounted
 * unlisten();
 * ```
 *
 * @param event Event name. Must include only alphanumeric characters, `-`, `/`, `:` and `_`.
 * @param handler Event handler callback.
 * @returns A promise resolving to a function to unlisten to the event.
 * Note that removing the listener is required if your listener goes out of scope e.g. the component is unmounted.
 *
 * @since 1.0.0
 */
async function listen(event$1, handler) {
    return event.listen(event$1, null, handler);
}
/**
 * Listen to an one-off event. See {@link listen} for more information.
 *
 * @example
 * ```typescript
 * import { once } from '@tauri-apps/api/event';
 * interface LoadedPayload {
 *   loggedIn: boolean,
 *   token: string
 * }
 * const unlisten = await once<LoadedPayload>('loaded', (event) => {
 *   console.log(`App is loaded, loggedIn: ${event.payload.loggedIn}, token: ${event.payload.token}`);
 * });
 *
 * // you need to call unlisten if your handler goes out of scope e.g. the component is unmounted
 * unlisten();
 * ```
 *
 * @param event Event name. Must include only alphanumeric characters, `-`, `/`, `:` and `_`.
 * @returns A promise resolving to a function to unlisten to the event.
 * Note that removing the listener is required if your listener goes out of scope e.g. the component is unmounted.
 *
 * @since 1.0.0
 */
async function once(event$1, handler) {
    return event.once(event$1, null, handler);
}
/**
 * Emits an event to the backend and all Tauri windows.
 * @example
 * ```typescript
 * import { emit } from '@tauri-apps/api/event';
 * await emit('frontend-loaded', { loggedIn: true, token: 'authToken' });
 * ```
 *
 * @param event Event name. Must include only alphanumeric characters, `-`, `/`, `:` and `_`.
 *
 * @since 1.0.0
 */
async function emit(event$1, payload) {
    return event.emit(event$1, undefined, payload);
}

exports.emit = emit;
exports.listen = listen;
exports.once = once;
