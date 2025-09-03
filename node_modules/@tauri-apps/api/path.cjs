'use strict';

var tauri = require('./helpers/tauri.cjs');
var fs = require('./fs.cjs');
var osCheck = require('./helpers/os-check.cjs');

// Copyright 2019-2023 Tauri Programme within The Commons Conservancy
// SPDX-License-Identifier: Apache-2.0
// SPDX-License-Identifier: MIT
/**
 * The path module provides utilities for working with file and directory paths.
 *
 * This package is also accessible with `window.__TAURI__.path` when [`build.withGlobalTauri`](https://tauri.app/v1/api/config/#buildconfig.withglobaltauri) in `tauri.conf.json` is set to `true`.
 *
 * The APIs must be added to [`tauri.allowlist.path`](https://tauri.app/v1/api/config/#allowlistconfig.path) in `tauri.conf.json`:
 * ```json
 * {
 *   "tauri": {
 *     "allowlist": {
 *       "path": {
 *         "all": true, // enable all Path APIs
 *       }
 *     }
 *   }
 * }
 * ```
 * It is recommended to allowlist only the APIs you use for optimal bundle size and security.
 * @module
 */
/**
 * Returns the path to the suggested directory for your app config files.
 *
 * @deprecated since 1.2.0: Will be removed in 2.0.0. Use {@link appConfigDir} or {@link appDataDir} instead.
 * @since 1.0.0
 */
async function appDir() {
    return appConfigDir();
}
/**
 * Returns the path to the suggested directory for your app's config files.
 * Resolves to `${configDir}/${bundleIdentifier}`, where `bundleIdentifier` is the value [`tauri.bundle.identifier`](https://tauri.app/v1/api/config/#bundleconfig.identifier) is configured in `tauri.conf.json`.
 * @example
 * ```typescript
 * import { appConfigDir } from '@tauri-apps/api/path';
 * const appConfigDirPath = await appConfigDir();
 * ```
 *
 * @since 1.2.0
 */
async function appConfigDir() {
    return tauri.invokeTauriCommand({
        __tauriModule: 'Path',
        message: {
            cmd: 'resolvePath',
            path: '',
            directory: fs.BaseDirectory.AppConfig
        }
    });
}
/**
 * Returns the path to the suggested directory for your app's data files.
 * Resolves to `${dataDir}/${bundleIdentifier}`, where `bundleIdentifier` is the value [`tauri.bundle.identifier`](https://tauri.app/v1/api/config/#bundleconfig.identifier) is configured in `tauri.conf.json`.
 * @example
 * ```typescript
 * import { appDataDir } from '@tauri-apps/api/path';
 * const appDataDirPath = await appDataDir();
 * ```
 *
 * @since 1.2.0
 */
async function appDataDir() {
    return tauri.invokeTauriCommand({
        __tauriModule: 'Path',
        message: {
            cmd: 'resolvePath',
            path: '',
            directory: fs.BaseDirectory.AppData
        }
    });
}
/**
 * Returns the path to the suggested directory for your app's local data files.
 * Resolves to `${localDataDir}/${bundleIdentifier}`, where `bundleIdentifier` is the value [`tauri.bundle.identifier`](https://tauri.app/v1/api/config/#bundleconfig.identifier) is configured in `tauri.conf.json`.
 * @example
 * ```typescript
 * import { appLocalDataDir } from '@tauri-apps/api/path';
 * const appLocalDataDirPath = await appLocalDataDir();
 * ```
 *
 * @since 1.2.0
 */
async function appLocalDataDir() {
    return tauri.invokeTauriCommand({
        __tauriModule: 'Path',
        message: {
            cmd: 'resolvePath',
            path: '',
            directory: fs.BaseDirectory.AppLocalData
        }
    });
}
/**
 * Returns the path to the suggested directory for your app's cache files.
 * Resolves to `${cacheDir}/${bundleIdentifier}`, where `bundleIdentifier` is the value [`tauri.bundle.identifier`](https://tauri.app/v1/api/config/#bundleconfig.identifier) is configured in `tauri.conf.json`.
 * @example
 * ```typescript
 * import { appCacheDir } from '@tauri-apps/api/path';
 * const appCacheDirPath = await appCacheDir();
 * ```
 *
 * @since 1.2.0
 */
async function appCacheDir() {
    return tauri.invokeTauriCommand({
        __tauriModule: 'Path',
        message: {
            cmd: 'resolvePath',
            path: '',
            directory: fs.BaseDirectory.AppCache
        }
    });
}
/**
 * Returns the path to the user's audio directory.
 *
 * #### Platform-specific
 *
 * - **Linux:** Resolves to [`xdg-user-dirs`](https://www.freedesktop.org/wiki/Software/xdg-user-dirs/)' `XDG_MUSIC_DIR`.
 * - **macOS:** Resolves to `$HOME/Music`.
 * - **Windows:** Resolves to `{FOLDERID_Music}`.
 * @example
 * ```typescript
 * import { audioDir } from '@tauri-apps/api/path';
 * const audioDirPath = await audioDir();
 * ```
 *
 * @since 1.0.0
 */
async function audioDir() {
    return tauri.invokeTauriCommand({
        __tauriModule: 'Path',
        message: {
            cmd: 'resolvePath',
            path: '',
            directory: fs.BaseDirectory.Audio
        }
    });
}
/**
 * Returns the path to the user's cache directory.
 *
 * #### Platform-specific
 *
 * - **Linux:** Resolves to `$XDG_CACHE_HOME` or `$HOME/.cache`.
 * - **macOS:** Resolves to `$HOME/Library/Caches`.
 * - **Windows:** Resolves to `{FOLDERID_LocalAppData}`.
 * @example
 * ```typescript
 * import { cacheDir } from '@tauri-apps/api/path';
 * const cacheDirPath = await cacheDir();
 * ```
 *
 * @since 1.0.0
 */
async function cacheDir() {
    return tauri.invokeTauriCommand({
        __tauriModule: 'Path',
        message: {
            cmd: 'resolvePath',
            path: '',
            directory: fs.BaseDirectory.Cache
        }
    });
}
/**
 * Returns the path to the user's config directory.
 *
 * #### Platform-specific
 *
 * - **Linux:** Resolves to `$XDG_CONFIG_HOME` or `$HOME/.config`.
 * - **macOS:** Resolves to `$HOME/Library/Application Support`.
 * - **Windows:** Resolves to `{FOLDERID_RoamingAppData}`.
 * @example
 * ```typescript
 * import { configDir } from '@tauri-apps/api/path';
 * const configDirPath = await configDir();
 * ```
 *
 * @since 1.0.0
 */
async function configDir() {
    return tauri.invokeTauriCommand({
        __tauriModule: 'Path',
        message: {
            cmd: 'resolvePath',
            path: '',
            directory: fs.BaseDirectory.Config
        }
    });
}
/**
 * Returns the path to the user's data directory.
 *
 * #### Platform-specific
 *
 * - **Linux:** Resolves to `$XDG_DATA_HOME` or `$HOME/.local/share`.
 * - **macOS:** Resolves to `$HOME/Library/Application Support`.
 * - **Windows:** Resolves to `{FOLDERID_RoamingAppData}`.
 * @example
 * ```typescript
 * import { dataDir } from '@tauri-apps/api/path';
 * const dataDirPath = await dataDir();
 * ```
 *
 * @since 1.0.0
 */
async function dataDir() {
    return tauri.invokeTauriCommand({
        __tauriModule: 'Path',
        message: {
            cmd: 'resolvePath',
            path: '',
            directory: fs.BaseDirectory.Data
        }
    });
}
/**
 * Returns the path to the user's desktop directory.
 *
 * #### Platform-specific
 *
 * - **Linux:** Resolves to [`xdg-user-dirs`](https://www.freedesktop.org/wiki/Software/xdg-user-dirs/)' `XDG_DESKTOP_DIR`.
 * - **macOS:** Resolves to `$HOME/Desktop`.
 * - **Windows:** Resolves to `{FOLDERID_Desktop}`.
 * @example
 * ```typescript
 * import { desktopDir } from '@tauri-apps/api/path';
 * const desktopPath = await desktopDir();
 * ```
 *
 * @since 1.0.0
 */
async function desktopDir() {
    return tauri.invokeTauriCommand({
        __tauriModule: 'Path',
        message: {
            cmd: 'resolvePath',
            path: '',
            directory: fs.BaseDirectory.Desktop
        }
    });
}
/**
 * Returns the path to the user's document directory.
 * @example
 * ```typescript
 * import { documentDir } from '@tauri-apps/api/path';
 * const documentDirPath = await documentDir();
 * ```
 *
 * #### Platform-specific
 *
 * - **Linux:** Resolves to [`xdg-user-dirs`](https://www.freedesktop.org/wiki/Software/xdg-user-dirs/)' `XDG_DOCUMENTS_DIR`.
 * - **macOS:** Resolves to `$HOME/Documents`.
 * - **Windows:** Resolves to `{FOLDERID_Documents}`.
 *
 * @since 1.0.0
 */
async function documentDir() {
    return tauri.invokeTauriCommand({
        __tauriModule: 'Path',
        message: {
            cmd: 'resolvePath',
            path: '',
            directory: fs.BaseDirectory.Document
        }
    });
}
/**
 * Returns the path to the user's download directory.
 *
 * #### Platform-specific
 *
 * - **Linux**: Resolves to [`xdg-user-dirs`](https://www.freedesktop.org/wiki/Software/xdg-user-dirs/)' `XDG_DOWNLOAD_DIR`.
 * - **macOS**: Resolves to `$HOME/Downloads`.
 * - **Windows**: Resolves to `{FOLDERID_Downloads}`.
 * @example
 * ```typescript
 * import { downloadDir } from '@tauri-apps/api/path';
 * const downloadDirPath = await downloadDir();
 * ```
 *
 * @since 1.0.0
 */
async function downloadDir() {
    return tauri.invokeTauriCommand({
        __tauriModule: 'Path',
        message: {
            cmd: 'resolvePath',
            path: '',
            directory: fs.BaseDirectory.Download
        }
    });
}
/**
 * Returns the path to the user's executable directory.
 *
 * #### Platform-specific
 *
 * - **Linux:** Resolves to `$XDG_BIN_HOME/../bin` or `$XDG_DATA_HOME/../bin` or `$HOME/.local/bin`.
 * - **macOS:** Not supported.
 * - **Windows:** Not supported.
 * @example
 * ```typescript
 * import { executableDir } from '@tauri-apps/api/path';
 * const executableDirPath = await executableDir();
 * ```
 *
 * @since 1.0.0
 */
async function executableDir() {
    return tauri.invokeTauriCommand({
        __tauriModule: 'Path',
        message: {
            cmd: 'resolvePath',
            path: '',
            directory: fs.BaseDirectory.Executable
        }
    });
}
/**
 * Returns the path to the user's font directory.
 *
 * #### Platform-specific
 *
 * - **Linux:** Resolves to `$XDG_DATA_HOME/fonts` or `$HOME/.local/share/fonts`.
 * - **macOS:** Resolves to `$HOME/Library/Fonts`.
 * - **Windows:** Not supported.
 * @example
 * ```typescript
 * import { fontDir } from '@tauri-apps/api/path';
 * const fontDirPath = await fontDir();
 * ```
 *
 * @since 1.0.0
 */
async function fontDir() {
    return tauri.invokeTauriCommand({
        __tauriModule: 'Path',
        message: {
            cmd: 'resolvePath',
            path: '',
            directory: fs.BaseDirectory.Font
        }
    });
}
/**
 * Returns the path to the user's home directory.
 *
 * #### Platform-specific
 *
 * - **Linux:** Resolves to `$HOME`.
 * - **macOS:** Resolves to `$HOME`.
 * - **Windows:** Resolves to `{FOLDERID_Profile}`.
 * @example
 * ```typescript
 * import { homeDir } from '@tauri-apps/api/path';
 * const homeDirPath = await homeDir();
 * ```
 *
 * @since 1.0.0
 */
async function homeDir() {
    return tauri.invokeTauriCommand({
        __tauriModule: 'Path',
        message: {
            cmd: 'resolvePath',
            path: '',
            directory: fs.BaseDirectory.Home
        }
    });
}
/**
 * Returns the path to the user's local data directory.
 *
 * #### Platform-specific
 *
 * - **Linux:** Resolves to `$XDG_DATA_HOME` or `$HOME/.local/share`.
 * - **macOS:** Resolves to `$HOME/Library/Application Support`.
 * - **Windows:** Resolves to `{FOLDERID_LocalAppData}`.
 * @example
 * ```typescript
 * import { localDataDir } from '@tauri-apps/api/path';
 * const localDataDirPath = await localDataDir();
 * ```
 *
 * @since 1.0.0
 */
async function localDataDir() {
    return tauri.invokeTauriCommand({
        __tauriModule: 'Path',
        message: {
            cmd: 'resolvePath',
            path: '',
            directory: fs.BaseDirectory.LocalData
        }
    });
}
/**
 * Returns the path to the user's picture directory.
 *
 * #### Platform-specific
 *
 * - **Linux:** Resolves to [`xdg-user-dirs`](https://www.freedesktop.org/wiki/Software/xdg-user-dirs/)' `XDG_PICTURES_DIR`.
 * - **macOS:** Resolves to `$HOME/Pictures`.
 * - **Windows:** Resolves to `{FOLDERID_Pictures}`.
 * @example
 * ```typescript
 * import { pictureDir } from '@tauri-apps/api/path';
 * const pictureDirPath = await pictureDir();
 * ```
 *
 * @since 1.0.0
 */
async function pictureDir() {
    return tauri.invokeTauriCommand({
        __tauriModule: 'Path',
        message: {
            cmd: 'resolvePath',
            path: '',
            directory: fs.BaseDirectory.Picture
        }
    });
}
/**
 * Returns the path to the user's public directory.
 *
 * #### Platform-specific
 *
 * - **Linux:** Resolves to [`xdg-user-dirs`](https://www.freedesktop.org/wiki/Software/xdg-user-dirs/)' `XDG_PUBLICSHARE_DIR`.
 * - **macOS:** Resolves to `$HOME/Public`.
 * - **Windows:** Resolves to `{FOLDERID_Public}`.
 * @example
 * ```typescript
 * import { publicDir } from '@tauri-apps/api/path';
 * const publicDirPath = await publicDir();
 * ```
 *
 * @since 1.0.0
 */
async function publicDir() {
    return tauri.invokeTauriCommand({
        __tauriModule: 'Path',
        message: {
            cmd: 'resolvePath',
            path: '',
            directory: fs.BaseDirectory.Public
        }
    });
}
/**
 * Returns the path to the application's resource directory.
 * To resolve a resource path, see the [[resolveResource | `resolveResource API`]].
 * @example
 * ```typescript
 * import { resourceDir } from '@tauri-apps/api/path';
 * const resourceDirPath = await resourceDir();
 * ```
 *
 * @since 1.0.0
 */
async function resourceDir() {
    return tauri.invokeTauriCommand({
        __tauriModule: 'Path',
        message: {
            cmd: 'resolvePath',
            path: '',
            directory: fs.BaseDirectory.Resource
        }
    });
}
/**
 * Resolve the path to a resource file.
 * @example
 * ```typescript
 * import { resolveResource } from '@tauri-apps/api/path';
 * const resourcePath = await resolveResource('script.sh');
 * ```
 *
 * @param resourcePath The path to the resource.
 * Must follow the same syntax as defined in `tauri.conf.json > tauri > bundle > resources`, i.e. keeping subfolders and parent dir components (`../`).
 * @returns The full path to the resource.
 *
 * @since 1.0.0
 */
async function resolveResource(resourcePath) {
    return tauri.invokeTauriCommand({
        __tauriModule: 'Path',
        message: {
            cmd: 'resolvePath',
            path: resourcePath,
            directory: fs.BaseDirectory.Resource
        }
    });
}
/**
 * Returns the path to the user's runtime directory.
 *
 * #### Platform-specific
 *
 * - **Linux:** Resolves to `$XDG_RUNTIME_DIR`.
 * - **macOS:** Not supported.
 * - **Windows:** Not supported.
 * @example
 * ```typescript
 * import { runtimeDir } from '@tauri-apps/api/path';
 * const runtimeDirPath = await runtimeDir();
 * ```
 *
 * @since 1.0.0
 */
async function runtimeDir() {
    return tauri.invokeTauriCommand({
        __tauriModule: 'Path',
        message: {
            cmd: 'resolvePath',
            path: '',
            directory: fs.BaseDirectory.Runtime
        }
    });
}
/**
 * Returns the path to the user's template directory.
 *
 * #### Platform-specific
 *
 * - **Linux:** Resolves to [`xdg-user-dirs`](https://www.freedesktop.org/wiki/Software/xdg-user-dirs/)' `XDG_TEMPLATES_DIR`.
 * - **macOS:** Not supported.
 * - **Windows:** Resolves to `{FOLDERID_Templates}`.
 * @example
 * ```typescript
 * import { templateDir } from '@tauri-apps/api/path';
 * const templateDirPath = await templateDir();
 * ```
 *
 * @since 1.0.0
 */
async function templateDir() {
    return tauri.invokeTauriCommand({
        __tauriModule: 'Path',
        message: {
            cmd: 'resolvePath',
            path: '',
            directory: fs.BaseDirectory.Template
        }
    });
}
/**
 * Returns the path to the user's video directory.
 *
 * #### Platform-specific
 *
 * - **Linux:** Resolves to [`xdg-user-dirs`](https://www.freedesktop.org/wiki/Software/xdg-user-dirs/)' `XDG_VIDEOS_DIR`.
 * - **macOS:** Resolves to `$HOME/Movies`.
 * - **Windows:** Resolves to `{FOLDERID_Videos}`.
 * @example
 * ```typescript
 * import { videoDir } from '@tauri-apps/api/path';
 * const videoDirPath = await videoDir();
 * ```
 *
 * @since 1.0.0
 */
async function videoDir() {
    return tauri.invokeTauriCommand({
        __tauriModule: 'Path',
        message: {
            cmd: 'resolvePath',
            path: '',
            directory: fs.BaseDirectory.Video
        }
    });
}
/**
 * Returns the path to the suggested log directory.
 *
 * @deprecated since 1.2.0: Will be removed in 2.0.0. Use {@link appLogDir} instead.
 * @since 1.0.0
 */
async function logDir() {
    return appLogDir();
}
/**
 * Returns the path to the suggested directory for your app's log files.
 *
 * #### Platform-specific
 *
 * - **Linux:** Resolves to `${configDir}/${bundleIdentifier}/logs`.
 * - **macOS:** Resolves to `${homeDir}/Library/Logs/{bundleIdentifier}`
 * - **Windows:** Resolves to `${configDir}/${bundleIdentifier}/logs`.
 * @example
 * ```typescript
 * import { appLogDir } from '@tauri-apps/api/path';
 * const appLogDirPath = await appLogDir();
 * ```
 *
 * @since 1.2.0
 */
async function appLogDir() {
    return tauri.invokeTauriCommand({
        __tauriModule: 'Path',
        message: {
            cmd: 'resolvePath',
            path: '',
            directory: fs.BaseDirectory.AppLog
        }
    });
}
/**
 * Provides the platform-specific path segment separator:
 * - `\` on Windows
 * - `/` on POSIX
 *
 * @since 1.0.0
 */
const sep = osCheck.isWindows() ? '\\' : '/';
/**
 * Provides the platform-specific path segment delimiter:
 * - `;` on Windows
 * - `:` on POSIX
 *
 * @since 1.0.0
 */
const delimiter = osCheck.isWindows() ? ';' : ':';
/**
 * Resolves a sequence of `paths` or `path` segments into an absolute path.
 * @example
 * ```typescript
 * import { resolve, appDataDir } from '@tauri-apps/api/path';
 * const appDataDirPath = await appDataDir();
 * const path = await resolve(appDataDirPath, '..', 'users', 'tauri', 'avatar.png');
 * ```
 *
 * @since 1.0.0
 */
async function resolve(...paths) {
    return tauri.invokeTauriCommand({
        __tauriModule: 'Path',
        message: {
            cmd: 'resolve',
            paths
        }
    });
}
/**
 * Normalizes the given `path`, resolving `'..'` and `'.'` segments and resolve symbolic links.
 * @example
 * ```typescript
 * import { normalize, appDataDir } from '@tauri-apps/api/path';
 * const appDataDirPath = await appDataDir();
 * const path = await normalize(appDataDirPath, '..', 'users', 'tauri', 'avatar.png');
 * ```
 *
 * @since 1.0.0
 */
async function normalize(path) {
    return tauri.invokeTauriCommand({
        __tauriModule: 'Path',
        message: {
            cmd: 'normalize',
            path
        }
    });
}
/**
 *  Joins all given `path` segments together using the platform-specific separator as a delimiter, then normalizes the resulting path.
 * @example
 * ```typescript
 * import { join, appDataDir } from '@tauri-apps/api/path';
 * const appDataDirPath = await appDataDir();
 * const path = await join(appDataDirPath, 'users', 'tauri', 'avatar.png');
 * ```
 *
 * @since 1.0.0
 */
async function join(...paths) {
    return tauri.invokeTauriCommand({
        __tauriModule: 'Path',
        message: {
            cmd: 'join',
            paths
        }
    });
}
/**
 * Returns the directory name of a `path`. Trailing directory separators are ignored.
 * @example
 * ```typescript
 * import { dirname } from '@tauri-apps/api/path';
 * const dir = await dirname('/path/to/somedir/');
 * assert(dir === 'somedir');
 * ```
 *
 * @since 1.0.0
 */
async function dirname(path) {
    return tauri.invokeTauriCommand({
        __tauriModule: 'Path',
        message: {
            cmd: 'dirname',
            path
        }
    });
}
/**
 * Returns the extension of the `path`.
 * @example
 * ```typescript
 * import { extname } from '@tauri-apps/api/path';
 * const ext = await extname('/path/to/file.html');
 * assert(ext === 'html');
 * ```
 *
 * @since 1.0.0
 */
async function extname(path) {
    return tauri.invokeTauriCommand({
        __tauriModule: 'Path',
        message: {
            cmd: 'extname',
            path
        }
    });
}
/**
 * Returns the last portion of a `path`. Trailing directory separators are ignored.
 * @example
 * ```typescript
 * import { basename } from '@tauri-apps/api/path';
 * const base = await basename('path/to/app.conf');
 * assert(base === 'app.conf');
 * ```
 *
 * @param ext An optional file extension to be removed from the returned path.
 *
 * @since 1.0.0
 */
async function basename(path, ext) {
    return tauri.invokeTauriCommand({
        __tauriModule: 'Path',
        message: {
            cmd: 'basename',
            path,
            ext
        }
    });
}
/**
 * Returns whether the path is absolute or not.
 * @example
 * ```typescript
 * import { isAbsolute } from '@tauri-apps/api/path';
 * assert(await isAbsolute('/home/tauri'));
 * ```
 *
 * @since 1.0.0
 */
async function isAbsolute(path) {
    return tauri.invokeTauriCommand({
        __tauriModule: 'Path',
        message: {
            cmd: 'isAbsolute',
            path
        }
    });
}

Object.defineProperty(exports, 'BaseDirectory', {
    enumerable: true,
    get: function () { return fs.BaseDirectory; }
});
exports.appCacheDir = appCacheDir;
exports.appConfigDir = appConfigDir;
exports.appDataDir = appDataDir;
exports.appDir = appDir;
exports.appLocalDataDir = appLocalDataDir;
exports.appLogDir = appLogDir;
exports.audioDir = audioDir;
exports.basename = basename;
exports.cacheDir = cacheDir;
exports.configDir = configDir;
exports.dataDir = dataDir;
exports.delimiter = delimiter;
exports.desktopDir = desktopDir;
exports.dirname = dirname;
exports.documentDir = documentDir;
exports.downloadDir = downloadDir;
exports.executableDir = executableDir;
exports.extname = extname;
exports.fontDir = fontDir;
exports.homeDir = homeDir;
exports.isAbsolute = isAbsolute;
exports.join = join;
exports.localDataDir = localDataDir;
exports.logDir = logDir;
exports.normalize = normalize;
exports.pictureDir = pictureDir;
exports.publicDir = publicDir;
exports.resolve = resolve;
exports.resolveResource = resolveResource;
exports.resourceDir = resourceDir;
exports.runtimeDir = runtimeDir;
exports.sep = sep;
exports.templateDir = templateDir;
exports.videoDir = videoDir;
