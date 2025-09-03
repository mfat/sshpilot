import * as app from './app.js';
export { app };
import * as cli from './cli.js';
export { cli };
import * as clipboard from './clipboard.js';
export { clipboard };
import * as dialog from './dialog.js';
export { dialog };
import * as event from './event.js';
export { event };
import * as fs from './fs.js';
export { fs };
import * as globalShortcut from './globalShortcut.js';
export { globalShortcut };
import * as http from './http.js';
export { http };
import * as notification from './notification.js';
export { notification };
import * as path from './path.js';
export { path };
import * as process from './process.js';
export { process };
import * as shell from './shell.js';
export { shell };
import { invoke as invoke$1 } from './tauri.js';
import * as tauri from './tauri.js';
export { tauri };
import * as updater from './updater.js';
export { updater };
import * as window from './window.js';
export { window };
import * as os from './os.js';
export { os };

// Copyright 2019-2023 Tauri Programme within The Commons Conservancy
// SPDX-License-Identifier: Apache-2.0
// SPDX-License-Identifier: MIT
/**
 * The Tauri API allows you to interface with the backend layer.
 *
 * This module exposes all other modules as an object where the key is the module name, and the value is the module exports.
 * @example
 * ```typescript
 * import { app, dialog, event, fs, globalShortcut } from '@tauri-apps/api'
 * ```
 * @module
 */
/** @ignore */
const invoke = invoke$1;

export { invoke };
