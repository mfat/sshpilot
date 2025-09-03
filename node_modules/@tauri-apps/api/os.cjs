'use strict';

var osCheck = require('./helpers/os-check.cjs');
var tauri = require('./helpers/tauri.cjs');

// Copyright 2019-2023 Tauri Programme within The Commons Conservancy
// SPDX-License-Identifier: Apache-2.0
// SPDX-License-Identifier: MIT
/**
 * Provides operating system-related utility methods and properties.
 *
 * This package is also accessible with `window.__TAURI__.os` when [`build.withGlobalTauri`](https://tauri.app/v1/api/config/#buildconfig.withglobaltauri) in `tauri.conf.json` is set to `true`.
 *
 * The APIs must be added to [`tauri.allowlist.os`](https://tauri.app/v1/api/config/#allowlistconfig.os) in `tauri.conf.json`:
 * ```json
 * {
 *   "tauri": {
 *     "allowlist": {
 *       "os": {
 *         "all": true, // enable all Os APIs
 *       }
 *     }
 *   }
 * }
 * ```
 * It is recommended to allowlist only the APIs you use for optimal bundle size and security.
 * @module
 */
/**
 * The operating system-specific end-of-line marker.
 * - `\n` on POSIX
 * - `\r\n` on Windows
 *
 * @since 1.0.0
 * */
const EOL = osCheck.isWindows() ? '\r\n' : '\n';
/**
 * Returns a string identifying the operating system platform.
 * The value is set at compile time. Possible values are `'linux'`, `'darwin'`, `'ios'`, `'freebsd'`, `'dragonfly'`, `'netbsd'`, `'openbsd'`, `'solaris'`, `'android'`, `'win32'`
 * @example
 * ```typescript
 * import { platform } from '@tauri-apps/api/os';
 * const platformName = await platform();
 * ```
 *
 * @since 1.0.0
 *
 */
async function platform() {
    return tauri.invokeTauriCommand({
        __tauriModule: 'Os',
        message: {
            cmd: 'platform'
        }
    });
}
/**
 * Returns a string identifying the kernel version.
 * @example
 * ```typescript
 * import { version } from '@tauri-apps/api/os';
 * const osVersion = await version();
 * ```
 *
 * @since 1.0.0
 */
async function version() {
    return tauri.invokeTauriCommand({
        __tauriModule: 'Os',
        message: {
            cmd: 'version'
        }
    });
}
/**
 * Returns `'Linux'` on Linux, `'Darwin'` on macOS, and `'Windows_NT'` on Windows.
 * @example
 * ```typescript
 * import { type } from '@tauri-apps/api/os';
 * const osType = await type();
 * ```
 *
 * @since 1.0.0
 */
async function type() {
    return tauri.invokeTauriCommand({
        __tauriModule: 'Os',
        message: {
            cmd: 'osType'
        }
    });
}
/**
 * Returns the operating system CPU architecture for which the tauri app was compiled.
 * Possible values are `'x86'`, `'x86_64'`, `'arm'`, `'aarch64'`, `'mips'`, `'mips64'`, `'powerpc'`, `'powerpc64'`, `'riscv64'`, `'s390x'`, `'sparc64'`.
 * @example
 * ```typescript
 * import { arch } from '@tauri-apps/api/os';
 * const archName = await arch();
 * ```
 *
 * @since 1.0.0
 */
async function arch() {
    return tauri.invokeTauriCommand({
        __tauriModule: 'Os',
        message: {
            cmd: 'arch'
        }
    });
}
/**
 * Returns the operating system's default directory for temporary files as a string.
 * @example
 * ```typescript
 * import { tempdir } from '@tauri-apps/api/os';
 * const tempdirPath = await tempdir();
 * ```
 *
 * @since 1.0.0
 */
async function tempdir() {
    return tauri.invokeTauriCommand({
        __tauriModule: 'Os',
        message: {
            cmd: 'tempdir'
        }
    });
}
/**
 * Returns a String with a `BCP-47` language tag inside. If the locale couldn’t be obtained, `null` is returned instead.
 * @example
 * ```typescript
 * import { locale } from '@tauri-apps/api/os';
 * const locale = await locale();
 * if (locale) {
 *    // use the locale string here
 * }
 * ```
 *
 * @since 1.4.0
 */
async function locale() {
    return tauri.invokeTauriCommand({
        __tauriModule: 'Os',
        message: {
            cmd: 'locale'
        }
    });
}

exports.EOL = EOL;
exports.arch = arch;
exports.locale = locale;
exports.platform = platform;
exports.tempdir = tempdir;
exports.type = type;
exports.version = version;
