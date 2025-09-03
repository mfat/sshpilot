'use strict';

var tauri = require('./helpers/tauri.cjs');

// Copyright 2019-2023 Tauri Programme within The Commons Conservancy
// SPDX-License-Identifier: Apache-2.0
// SPDX-License-Identifier: MIT
/**
 * Access the file system.
 *
 * This package is also accessible with `window.__TAURI__.fs` when [`build.withGlobalTauri`](https://tauri.app/v1/api/config/#buildconfig.withglobaltauri) in `tauri.conf.json` is set to `true`.
 *
 * The APIs must be added to [`tauri.allowlist.fs`](https://tauri.app/v1/api/config/#allowlistconfig.fs) in `tauri.conf.json`:
 * ```json
 * {
 *   "tauri": {
 *     "allowlist": {
 *       "fs": {
 *         "all": true, // enable all FS APIs
 *         "readFile": true,
 *         "writeFile": true,
 *         "readDir": true,
 *         "copyFile": true,
 *         "createDir": true,
 *         "removeDir": true,
 *         "removeFile": true,
 *         "renameFile": true,
 *         "exists": true
 *       }
 *     }
 *   }
 * }
 * ```
 * It is recommended to allowlist only the APIs you use for optimal bundle size and security.
 *
 * ## Security
 *
 * This module prevents path traversal, not allowing absolute paths or parent dir components
 * (i.e. "/usr/path/to/file" or "../path/to/file" paths are not allowed).
 * Paths accessed with this API must be relative to one of the {@link BaseDirectory | base directories}
 * so if you need access to arbitrary filesystem paths, you must write such logic on the core layer instead.
 *
 * The API has a scope configuration that forces you to restrict the paths that can be accessed using glob patterns.
 *
 * The scope configuration is an array of glob patterns describing folder paths that are allowed.
 * For instance, this scope configuration only allows accessing files on the
 * *databases* folder of the {@link path.appDataDir | $APPDATA directory}:
 * ```json
 * {
 *   "tauri": {
 *     "allowlist": {
 *       "fs": {
 *         "scope": ["$APPDATA/databases/*"]
 *       }
 *     }
 *   }
 * }
 * ```
 *
 * Notice the use of the `$APPDATA` variable. The value is injected at runtime, resolving to the {@link path.appDataDir | app data directory}.
 * The available variables are:
 * {@link path.appConfigDir | `$APPCONFIG`}, {@link path.appDataDir | `$APPDATA`}, {@link path.appLocalDataDir | `$APPLOCALDATA`},
 * {@link path.appCacheDir | `$APPCACHE`}, {@link path.appLogDir | `$APPLOG`},
 * {@link path.audioDir | `$AUDIO`}, {@link path.cacheDir | `$CACHE`}, {@link path.configDir | `$CONFIG`}, {@link path.dataDir | `$DATA`},
 * {@link path.localDataDir | `$LOCALDATA`}, {@link path.desktopDir | `$DESKTOP`}, {@link path.documentDir | `$DOCUMENT`},
 * {@link path.downloadDir | `$DOWNLOAD`}, {@link path.executableDir | `$EXE`}, {@link path.fontDir | `$FONT`}, {@link path.homeDir | `$HOME`},
 * {@link path.pictureDir | `$PICTURE`}, {@link path.publicDir | `$PUBLIC`}, {@link path.runtimeDir | `$RUNTIME`},
 * {@link path.templateDir | `$TEMPLATE`}, {@link path.videoDir | `$VIDEO`}, {@link path.resourceDir | `$RESOURCE`}, {@link path.appDir | `$APP`},
 * {@link path.logDir | `$LOG`}, {@link os.tempdir | `$TEMP`}.
 *
 * Trying to execute any API with a URL not configured on the scope results in a promise rejection due to denied access.
 *
 * Note that this scope applies to **all** APIs on this module.
 *
 * @module
 */
/**
 * @since 1.0.0
 */
exports.Dir = void 0;
(function (BaseDirectory) {
    BaseDirectory[BaseDirectory["Audio"] = 1] = "Audio";
    BaseDirectory[BaseDirectory["Cache"] = 2] = "Cache";
    BaseDirectory[BaseDirectory["Config"] = 3] = "Config";
    BaseDirectory[BaseDirectory["Data"] = 4] = "Data";
    BaseDirectory[BaseDirectory["LocalData"] = 5] = "LocalData";
    BaseDirectory[BaseDirectory["Desktop"] = 6] = "Desktop";
    BaseDirectory[BaseDirectory["Document"] = 7] = "Document";
    BaseDirectory[BaseDirectory["Download"] = 8] = "Download";
    BaseDirectory[BaseDirectory["Executable"] = 9] = "Executable";
    BaseDirectory[BaseDirectory["Font"] = 10] = "Font";
    BaseDirectory[BaseDirectory["Home"] = 11] = "Home";
    BaseDirectory[BaseDirectory["Picture"] = 12] = "Picture";
    BaseDirectory[BaseDirectory["Public"] = 13] = "Public";
    BaseDirectory[BaseDirectory["Runtime"] = 14] = "Runtime";
    BaseDirectory[BaseDirectory["Template"] = 15] = "Template";
    BaseDirectory[BaseDirectory["Video"] = 16] = "Video";
    BaseDirectory[BaseDirectory["Resource"] = 17] = "Resource";
    BaseDirectory[BaseDirectory["App"] = 18] = "App";
    BaseDirectory[BaseDirectory["Log"] = 19] = "Log";
    BaseDirectory[BaseDirectory["Temp"] = 20] = "Temp";
    BaseDirectory[BaseDirectory["AppConfig"] = 21] = "AppConfig";
    BaseDirectory[BaseDirectory["AppData"] = 22] = "AppData";
    BaseDirectory[BaseDirectory["AppLocalData"] = 23] = "AppLocalData";
    BaseDirectory[BaseDirectory["AppCache"] = 24] = "AppCache";
    BaseDirectory[BaseDirectory["AppLog"] = 25] = "AppLog";
})(exports.Dir || (exports.Dir = {}));
/**
 * Reads a file as an UTF-8 encoded string.
 * @example
 * ```typescript
 * import { readTextFile, BaseDirectory } from '@tauri-apps/api/fs';
 * // Read the text file in the `$APPCONFIG/app.conf` path
 * const contents = await readTextFile('app.conf', { dir: BaseDirectory.AppConfig });
 * ```
 *
 * @since 1.0.0
 */
async function readTextFile(filePath, options = {}) {
    return tauri.invokeTauriCommand({
        __tauriModule: 'Fs',
        message: {
            cmd: 'readTextFile',
            path: filePath,
            options
        }
    });
}
/**
 * Reads a file as byte array.
 * @example
 * ```typescript
 * import { readBinaryFile, BaseDirectory } from '@tauri-apps/api/fs';
 * // Read the image file in the `$RESOURCEDIR/avatar.png` path
 * const contents = await readBinaryFile('avatar.png', { dir: BaseDirectory.Resource });
 * ```
 *
 * @since 1.0.0
 */
async function readBinaryFile(filePath, options = {}) {
    const arr = await tauri.invokeTauriCommand({
        __tauriModule: 'Fs',
        message: {
            cmd: 'readFile',
            path: filePath,
            options
        }
    });
    return Uint8Array.from(arr);
}
/**
 * Writes a UTF-8 text file.
 *
 * @returns A promise indicating the success or failure of the operation.
 *
 * @since 1.0.0
 */
async function writeTextFile(path, contents, options) {
    if (typeof options === 'object') {
        Object.freeze(options);
    }
    if (typeof path === 'object') {
        Object.freeze(path);
    }
    const file = { path: '', contents: '' };
    let fileOptions = options;
    if (typeof path === 'string') {
        file.path = path;
    }
    else {
        file.path = path.path;
        file.contents = path.contents;
    }
    if (typeof contents === 'string') {
        file.contents = contents !== null && contents !== void 0 ? contents : '';
    }
    else {
        fileOptions = contents;
    }
    return tauri.invokeTauriCommand({
        __tauriModule: 'Fs',
        message: {
            cmd: 'writeFile',
            path: file.path,
            contents: Array.from(new TextEncoder().encode(file.contents)),
            options: fileOptions
        }
    });
}
/**
 * Writes a byte array content to a file.
 *
 * @returns A promise indicating the success or failure of the operation.
 *
 * @since 1.0.0
 */
async function writeBinaryFile(path, contents, options) {
    if (typeof options === 'object') {
        Object.freeze(options);
    }
    if (typeof path === 'object') {
        Object.freeze(path);
    }
    const file = { path: '', contents: [] };
    let fileOptions = options;
    if (typeof path === 'string') {
        file.path = path;
    }
    else {
        file.path = path.path;
        file.contents = path.contents;
    }
    if (contents && 'dir' in contents) {
        fileOptions = contents;
    }
    else if (typeof path === 'string') {
        // @ts-expect-error in this case `contents` is always a BinaryFileContents
        file.contents = contents !== null && contents !== void 0 ? contents : [];
    }
    return tauri.invokeTauriCommand({
        __tauriModule: 'Fs',
        message: {
            cmd: 'writeFile',
            path: file.path,
            contents: Array.from(file.contents instanceof ArrayBuffer
                ? new Uint8Array(file.contents)
                : file.contents),
            options: fileOptions
        }
    });
}
/**
 * List directory files.
 * @example
 * ```typescript
 * import { readDir, BaseDirectory } from '@tauri-apps/api/fs';
 * // Reads the `$APPDATA/users` directory recursively
 * const entries = await readDir('users', { dir: BaseDirectory.AppData, recursive: true });
 *
 * function processEntries(entries) {
 *   for (const entry of entries) {
 *     console.log(`Entry: ${entry.path}`);
 *     if (entry.children) {
 *       processEntries(entry.children)
 *     }
 *   }
 * }
 * ```
 *
 * @since 1.0.0
 */
async function readDir(dir, options = {}) {
    return tauri.invokeTauriCommand({
        __tauriModule: 'Fs',
        message: {
            cmd: 'readDir',
            path: dir,
            options
        }
    });
}
/**
 * Creates a directory.
 * If one of the path's parent components doesn't exist
 * and the `recursive` option isn't set to true, the promise will be rejected.
 * @example
 * ```typescript
 * import { createDir, BaseDirectory } from '@tauri-apps/api/fs';
 * // Create the `$APPDATA/users` directory
 * await createDir('users', { dir: BaseDirectory.AppData, recursive: true });
 * ```
 *
 * @returns A promise indicating the success or failure of the operation.
 *
 * @since 1.0.0
 */
async function createDir(dir, options = {}) {
    return tauri.invokeTauriCommand({
        __tauriModule: 'Fs',
        message: {
            cmd: 'createDir',
            path: dir,
            options
        }
    });
}
/**
 * Removes a directory.
 * If the directory is not empty and the `recursive` option isn't set to true, the promise will be rejected.
 * @example
 * ```typescript
 * import { removeDir, BaseDirectory } from '@tauri-apps/api/fs';
 * // Remove the directory `$APPDATA/users`
 * await removeDir('users', { dir: BaseDirectory.AppData });
 * ```
 *
 * @returns A promise indicating the success or failure of the operation.
 *
 * @since 1.0.0
 */
async function removeDir(dir, options = {}) {
    return tauri.invokeTauriCommand({
        __tauriModule: 'Fs',
        message: {
            cmd: 'removeDir',
            path: dir,
            options
        }
    });
}
/**
 * Copies a file to a destination.
 * @example
 * ```typescript
 * import { copyFile, BaseDirectory } from '@tauri-apps/api/fs';
 * // Copy the `$APPCONFIG/app.conf` file to `$APPCONFIG/app.conf.bk`
 * await copyFile('app.conf', 'app.conf.bk', { dir: BaseDirectory.AppConfig });
 * ```
 *
 * @returns A promise indicating the success or failure of the operation.
 *
 * @since 1.0.0
 */
async function copyFile(source, destination, options = {}) {
    return tauri.invokeTauriCommand({
        __tauriModule: 'Fs',
        message: {
            cmd: 'copyFile',
            source,
            destination,
            options
        }
    });
}
/**
 * Removes a file.
 * @example
 * ```typescript
 * import { removeFile, BaseDirectory } from '@tauri-apps/api/fs';
 * // Remove the `$APPConfig/app.conf` file
 * await removeFile('app.conf', { dir: BaseDirectory.AppConfig });
 * ```
 *
 * @returns A promise indicating the success or failure of the operation.
 *
 * @since 1.0.0
 */
async function removeFile(file, options = {}) {
    return tauri.invokeTauriCommand({
        __tauriModule: 'Fs',
        message: {
            cmd: 'removeFile',
            path: file,
            options
        }
    });
}
/**
 * Renames a file.
 * @example
 * ```typescript
 * import { renameFile, BaseDirectory } from '@tauri-apps/api/fs';
 * // Rename the `$APPDATA/avatar.png` file
 * await renameFile('avatar.png', 'deleted.png', { dir: BaseDirectory.AppData });
 * ```
 *
 * @returns A promise indicating the success or failure of the operation.
 *
 * @since 1.0.0
 */
async function renameFile(oldPath, newPath, options = {}) {
    return tauri.invokeTauriCommand({
        __tauriModule: 'Fs',
        message: {
            cmd: 'renameFile',
            oldPath,
            newPath,
            options
        }
    });
}
/**
 * Check if a path exists.
 * @example
 * ```typescript
 * import { exists, BaseDirectory } from '@tauri-apps/api/fs';
 * // Check if the `$APPDATA/avatar.png` file exists
 * await exists('avatar.png', { dir: BaseDirectory.AppData });
 * ```
 *
 * @since 1.1.0
 */
async function exists(path, options = {}) {
    return tauri.invokeTauriCommand({
        __tauriModule: 'Fs',
        message: {
            cmd: 'exists',
            path,
            options
        }
    });
}

exports.BaseDirectory = exports.Dir;
exports.copyFile = copyFile;
exports.createDir = createDir;
exports.exists = exists;
exports.readBinaryFile = readBinaryFile;
exports.readDir = readDir;
exports.readTextFile = readTextFile;
exports.removeDir = removeDir;
exports.removeFile = removeFile;
exports.renameFile = renameFile;
exports.writeBinaryFile = writeBinaryFile;
exports.writeFile = writeTextFile;
exports.writeTextFile = writeTextFile;
