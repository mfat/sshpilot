import { invoke } from '../tauri.js';

// Copyright 2019-2023 Tauri Programme within The Commons Conservancy
// SPDX-License-Identifier: Apache-2.0
// SPDX-License-Identifier: MIT
/** @ignore */
async function invokeTauriCommand(command) {
    return invoke('tauri', command);
}

export { invokeTauriCommand };
