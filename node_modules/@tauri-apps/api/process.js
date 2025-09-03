import { invokeTauriCommand } from './helpers/tauri.js';

// Copyright 2019-2023 Tauri Programme within The Commons Conservancy
// SPDX-License-Identifier: Apache-2.0
// SPDX-License-Identifier: MIT
/**
 * Perform operations on the current process.
 *
 * This package is also accessible with `window.__TAURI__.process` when [`build.withGlobalTauri`](https://tauri.app/v1/api/config/#buildconfig.withglobaltauri) in `tauri.conf.json` is set to `true`.
 * @module
 */
/**
 * Exits immediately with the given `exitCode`.
 * @example
 * ```typescript
 * import { exit } from '@tauri-apps/api/process';
 * await exit(1);
 * ```
 *
 * @param exitCode The exit code to use.
 * @returns A promise indicating the success or failure of the operation.
 *
 * @since 1.0.0
 */
async function exit(exitCode = 0) {
    return invokeTauriCommand({
        __tauriModule: 'Process',
        message: {
            cmd: 'exit',
            exitCode
        }
    });
}
/**
 * Exits the current instance of the app then relaunches it.
 * @example
 * ```typescript
 * import { relaunch } from '@tauri-apps/api/process';
 * await relaunch();
 * ```
 *
 * @returns A promise indicating the success or failure of the operation.
 *
 * @since 1.0.0
 */
async function relaunch() {
    return invokeTauriCommand({
        __tauriModule: 'Process',
        message: {
            cmd: 'relaunch'
        }
    });
}

export { exit, relaunch };
