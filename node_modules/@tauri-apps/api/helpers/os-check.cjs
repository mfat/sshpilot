'use strict';

// Copyright 2019-2023 Tauri Programme within The Commons Conservancy
// SPDX-License-Identifier: Apache-2.0
// SPDX-License-Identifier: MIT
/** @ignore */
function isWindows() {
    return navigator.appVersion.includes('Win');
}

exports.isWindows = isWindows;
