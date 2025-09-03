/*
  @license
	Rollup.js v4.50.0
	Sun, 31 Aug 2025 07:37:46 GMT - commit 592e7d78f726bb038881f3c9ab4450d26c3db8c7

	https://github.com/rollup/rollup

	Released under the MIT License.
*/
'use strict';

let fsEvents;
let fsEventsImportError;
async function loadFsEvents() {
    try {
        ({ default: fsEvents } = await import('fsevents'));
    }
    catch (error) {
        fsEventsImportError = error;
    }
}
// A call to this function will be injected into the chokidar code
function getFsEvents() {
    if (fsEventsImportError)
        throw fsEventsImportError;
    return fsEvents;
}

const fseventsImporter = /*#__PURE__*/Object.defineProperty({
    __proto__: null,
    getFsEvents,
    loadFsEvents
}, Symbol.toStringTag, { value: 'Module' });

exports.fseventsImporter = fseventsImporter;
exports.loadFsEvents = loadFsEvents;
//# sourceMappingURL=fsevents-importer.js.map
