/*
  @license
	Rollup.js v4.50.0
	Sun, 31 Aug 2025 07:37:46 GMT - commit 592e7d78f726bb038881f3c9ab4450d26c3db8c7

	https://github.com/rollup/rollup

	Released under the MIT License.
*/
export { version as VERSION, defineConfig, rollup, watch } from './shared/node-entry.js';
import './shared/parseAst.js';
import '../native.js';
import 'node:path';
import 'path';
import 'node:process';
import 'node:perf_hooks';
import 'node:fs/promises';
