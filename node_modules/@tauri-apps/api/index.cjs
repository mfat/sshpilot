'use strict';

var app = require('./app.cjs');
var cli = require('./cli.cjs');
var clipboard = require('./clipboard.cjs');
var dialog = require('./dialog.cjs');
var event = require('./event.cjs');
var fs = require('./fs.cjs');
var globalShortcut = require('./globalShortcut.cjs');
var http = require('./http.cjs');
var notification = require('./notification.cjs');
var path = require('./path.cjs');
var process = require('./process.cjs');
var shell = require('./shell.cjs');
var tauri = require('./tauri.cjs');
var updater = require('./updater.cjs');
var window = require('./window.cjs');
var os = require('./os.cjs');

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
const invoke = tauri.invoke;

exports.app = app;
exports.cli = cli;
exports.clipboard = clipboard;
exports.dialog = dialog;
exports.event = event;
exports.fs = fs;
exports.globalShortcut = globalShortcut;
exports.http = http;
exports.notification = notification;
exports.path = path;
exports.process = process;
exports.shell = shell;
exports.tauri = tauri;
exports.updater = updater;
exports.window = window;
exports.os = os;
exports.invoke = invoke;
