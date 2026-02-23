// LuxEdge Launcher - Electron'u doğrudan çalıştırır
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');

// Electron binary yolunu bul
const electronPathFile = path.join(__dirname, 'node_modules', 'electron', 'path.txt');
const electronExeName = fs.readFileSync(electronPathFile, 'utf-8').trim();
const electronBinary = path.join(__dirname, 'node_modules', 'electron', 'dist', electronExeName);

console.log('[LuxEdge] Electron başlatılıyor...');

// ÖNEMLİ: ELECTRON_RUN_AS_NODE değişkenini kaldır!
// Bu değişken set edilmişse Electron, Node.js olarak çalışır ve
// app, BrowserWindow gibi API'leri yüklemez.
const env = { ...process.env };
delete env.ELECTRON_RUN_AS_NODE;

const appPath = path.resolve(__dirname);

const child = spawn(electronBinary, [appPath], {
    stdio: 'inherit',
    cwd: __dirname,
    env: env
});

child.on('close', (code) => {
    process.exit(code || 0);
});

child.on('error', (err) => {
    console.error('[LuxEdge] Electron başlatılamadı:', err.message);
    process.exit(1);
});
