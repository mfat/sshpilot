'use strict';

var tauri = require('./helpers/tauri.cjs');

// Copyright 2019-2023 Tauri Programme within The Commons Conservancy
// SPDX-License-Identifier: Apache-2.0
// SPDX-License-Identifier: MIT
/**
 * Parse arguments from your Command Line Interface.
 *
 * This package is also accessible with `window.__TAURI__.cli` when [`build.withGlobalTauri`](https://tauri.app/v1/api/config/#buildconfig.withglobaltauri) in `tauri.conf.json` is set to `true`.
 * @module
 */
/**
 * Parse the arguments provided to the current process and get the matches using the configuration defined [`tauri.cli`](https://tauri.app/v1/api/config/#tauriconfig.cli) in `tauri.conf.json`
 * @example
 * ```typescript
 * import { getMatches } from '@tauri-apps/api/cli';
 * const matches = await getMatches();
 * if (matches.subcommand?.name === 'run') {
 *   // `./your-app run $ARGS` was executed
 *   const args = matches.subcommand?.matches.args
 *   if ('debug' in args) {
 *     // `./your-app run --debug` was executed
 *   }
 * } else {
 *   const args = matches.args
 *   // `./your-app $ARGS` was executed
 * }
 * ```
 *
 * @since 1.0.0
 */
async function getMatches() {
    return tauri.invokeTauriCommand({
        __tauriModule: 'Cli',
        message: {
            cmd: 'cliMatches'
        }
    });
}

exports.getMatches = getMatches;
