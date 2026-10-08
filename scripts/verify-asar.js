// Read the ASAR archive directly; never launch Electron.
const asar = require('@electron/asar');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const archive = process.argv[2] || path.join(root, 'dist/win-unpacked/resources/app.asar');
for (const file of ['main.js', 'preload.js', 'web_ui/index.html', 'web_ui/polling.js', 'web_ui/monitors.js']) {
    assert.deepEqual(asar.extractFile(archive, file), fs.readFileSync(path.join(root, file)), `Stale packaged file: ${file}`);
}
console.log('ASAR source files match:', archive);
