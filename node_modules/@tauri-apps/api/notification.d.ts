/**
 * Options to send a notification.
 *
 * @since 1.0.0
 */
interface Options {
    /** Notification title. */
    title: string;
    /** Optional notification body. */
    body?: string;
    /**
     * Optional notification icon.
     *
     * #### Platform-specific
     *
     * - **Windows**: The app must be installed for this to have any effect.
     *
     */
    icon?: string;
    /**
     * Optional notification sound.
     *
     * #### Platform-specific
     *
     * Each OS has a different sound name so you will need to conditionally specify an appropriate sound
     * based on the OS in use, 'default' represents the default system sound. For a list of sounds see:
     * - **Linux**: can be one of the sounds listed in {@link https://0pointer.de/public/sound-naming-spec.html}
     * - **Windows**: can be one of the sounds listed in {@link https://learn.microsoft.com/en-us/uwp/schemas/tiles/toastschema/element-audio}
     *   but without the prefix, for example, if `ms-winsoundevent:Notification.Default` you would use `Default` and
     *   if `ms-winsoundevent:Notification.Looping.Alarm2`, you would use `Alarm2`.
     *   Windows 7 is not supported, if a sound is provided, it will play the default sound, otherwise it will be silent.
     * - **macOS**: you can specify the name of the sound you'd like to play when the notification is shown.
     * Any of the default sounds (under System Preferences > Sound) can be used, in addition to custom sound files.
     * Be sure that the sound file is copied under the app bundle (e.g., `YourApp.app/Contents/Resources`), or one of the following locations:
     *   - `~/Library/Sounds`
     *   - `/Library/Sounds`
     *   - `/Network/Library/Sounds`
     *   - `/System/Library/Sounds`
     *
     *   See the {@link https://developer.apple.com/documentation/appkit/nssound | NSSound} docs for more information.
     *
     * @since 1.5.0
     */
    sound?: 'default' | string;
}
/** Possible permission values. */
type Permission = 'granted' | 'denied' | 'default';
/**
 * Checks if the permission to send notifications is granted.
 * @example
 * ```typescript
 * import { isPermissionGranted } from '@tauri-apps/api/notification';
 * const permissionGranted = await isPermissionGranted();
 * ```
 *
 * @since 1.0.0
 */
declare function isPermissionGranted(): Promise<boolean>;
/**
 * Requests the permission to send notifications.
 * @example
 * ```typescript
 * import { isPermissionGranted, requestPermission } from '@tauri-apps/api/notification';
 * let permissionGranted = await isPermissionGranted();
 * if (!permissionGranted) {
 *   const permission = await requestPermission();
 *   permissionGranted = permission === 'granted';
 * }
 * ```
 *
 * @returns A promise resolving to whether the user granted the permission or not.
 *
 * @since 1.0.0
 */
declare function requestPermission(): Promise<Permission>;
/**
 * Sends a notification to the user.
 * @example
 * ```typescript
 * import { isPermissionGranted, requestPermission, sendNotification } from '@tauri-apps/api/notification';
 * let permissionGranted = await isPermissionGranted();
 * if (!permissionGranted) {
 *   const permission = await requestPermission();
 *   permissionGranted = permission === 'granted';
 * }
 * if (permissionGranted) {
 *   sendNotification('Tauri is awesome!');
 *   sendNotification({ title: 'TAURI', body: 'Tauri is awesome!' });
 * }
 * ```
 *
 * @since 1.0.0
 */
declare function sendNotification(options: Options | string): void;
export type { Options, Permission };
export { sendNotification, requestPermission, isPermissionGranted };
