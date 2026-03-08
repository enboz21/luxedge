// ============================================================
// LuxEdge - Electron Ana Süreç
// Akıllı Ekran Kenar Aydınlatma Kontrol Sistemi
// ============================================================

const { app, BrowserWindow, ipcMain, Tray, Menu, nativeImage } = require('electron');
const { spawn } = require('child_process');
const http = require('http');
const path = require('path');
const fs = require('fs');
const os = require('os');

let mainWindow;
let pythonProcess;
let tray = null;
const PYTHON_PORT = 8888;

// ============================================================
// PYTHON HTTP İSTEKLERİ
// ============================================================

function pythonGet(endpoint, timeout = 5000) {
    return new Promise((resolve, reject) => {
        const req = http.get(`http://127.0.0.1:${PYTHON_PORT}${endpoint}`, { timeout }, (res) => {
            let data = '';
            res.on('data', chunk => data += chunk);
            res.on('end', () => {
                try { resolve(JSON.parse(data)); }
                catch (e) { resolve(data); }
            });
        });
        req.on('error', reject);
        req.on('timeout', () => { req.destroy(); reject(new Error('Zaman aşımı')); });
    });
}

function pythonPost(endpoint, body = {}, timeout = 5000) {
    return new Promise((resolve, reject) => {
        const jsonData = JSON.stringify(body);
        const options = {
            hostname: '127.0.0.1', port: PYTHON_PORT,
            path: endpoint, method: 'POST', timeout,
            headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(jsonData) }
        };
        const req = http.request(options, (res) => {
            let data = '';
            res.on('data', chunk => data += chunk);
            res.on('end', () => {
                try { resolve(JSON.parse(data)); }
                catch (e) { resolve(data); }
            });
        });
        req.on('error', reject);
        req.on('timeout', () => { req.destroy(); reject(new Error('Zaman aşımı')); });
        req.write(jsonData);
        req.end();
    });
}

// ============================================================
// PYTHON BACKEND
// ============================================================

function startPython() {
    return new Promise((resolve) => {
        console.log("[LuxEdge] Python backend başlatılıyor...");

        let executable, args, cwd;

        if (app.isPackaged) {
            // Paketlenmiş mod
            const isWin = process.platform === 'win32';
            const exeName = isWin ? 'lush_backend.exe' : 'lush_backend';
            const exePath = path.join(process.resourcesPath, exeName);
            const pyPath = path.join(process.resourcesPath, 'ambilight_pc.py');

            console.log(`[LuxEdge] Resources path: ${process.resourcesPath}`);
            console.log(`[LuxEdge] Backend exe: ${exePath} (var: ${fs.existsSync(exePath)})`);
            console.log(`[LuxEdge] Backend py: ${pyPath} (var: ${fs.existsSync(pyPath)})`);

            if (fs.existsSync(exePath)) {
                // PyInstaller binary mevcut — onu kullan
                executable = exePath;
                args = ['--no-tray'];
                cwd = process.resourcesPath;
                console.log(`[LuxEdge] PyInstaller ${isWin ? 'exe' : 'binary'} kullanılıyor`);
            } else if (fs.existsSync(pyPath)) {
                // Binary yok ama py var — sistem Python'ı dene
                executable = isWin ? 'python' : 'python3';
                args = [pyPath, '--no-tray'];
                cwd = process.resourcesPath;
                console.log("[LuxEdge] Sistem Python kullanılıyor (fallback)");
            } else {
                console.error(`[LuxEdge] HATA: Ne ${exeName} ne ambilight_pc.py bulunamadı!`);
                resolve(false);
                return;
            }
        } else {
            // Geliştirme modu: python scripti kullan
            const isWin = process.platform === 'win32';

            // Eğer projede .venv varsa (geliştirici ortamı), onu kullan
            const venvPathWin = path.join(__dirname, '.venv', 'Scripts', 'python.exe');
            const venvPathLin = path.join(__dirname, '.venv', 'bin', 'python');

            if (isWin && fs.existsSync(venvPathWin)) {
                executable = venvPathWin;
            } else if (!isWin && fs.existsSync(venvPathLin)) {
                executable = venvPathLin;
            } else {
                executable = isWin ? 'python' : 'python3';
            }

            args = ['ambilight_pc.py', '--no-tray'];
            cwd = __dirname;
        }

        console.log(`[LuxEdge] Çalıştırılıyor: ${executable} ${args.join(' ')}`);
        console.log(`[LuxEdge] Çalışma dizini: ${cwd}`);

        pythonProcess = spawn(executable, args, {
            cwd: cwd,
            env: { ...process.env, PYTHONUNBUFFERED: "1" },
            windowsHide: true,
            detached: process.platform !== 'win32' // Linux'ta process group oluştur
        });

        pythonProcess.stdout.on('data', (data) => {
            const msg = data.toString().trim();
            if (msg) console.log(`[Python] ${msg}`);
        });

        pythonProcess.stderr.on('data', (data) => {
            const msg = data.toString().trim();
            if (msg) console.error(`[Python] ${msg}`);
        });

        pythonProcess.on('close', (code) => {
            console.log(`[LuxEdge] Python kapandı (kod: ${code})`);
        });

        pythonProcess.on('error', (err) => {
            console.error(`[LuxEdge] Python başlatılamadı: ${err.message}`);
        });

        // Python sunucusunun hazır olmasını bekle
        let attempts = 0;
        const checkReady = () => {
            attempts++;
            pythonGet('/api/status', 2000)
                .then(() => {
                    console.log(`[LuxEdge] Python hazır! (${attempts} deneme)`);
                    resolve(true);
                })
                .catch(() => {
                    if (attempts < 60) setTimeout(checkReady, 1000);
                    else { console.warn('[LuxEdge] Python zaman aşımı'); resolve(false); }
                });
        };
        setTimeout(checkReady, 3000);
    });
}

function killPython() {
    if (pythonProcess) {
        try {
            if (process.platform === 'win32') {
                const { spawnSync } = require('child_process');
                spawnSync('taskkill', ['/pid', pythonProcess.pid.toString(), '/f', '/t'], { windowsHide: true });
            } else {
                // Linux: PyInstaller'ın bootloader'ı ile asıl scripti process group üzerinden tamamen kapat
                try {
                    process.kill(-pythonProcess.pid, 'SIGKILL');
                } catch (e) {
                    pythonProcess.kill('SIGKILL');
                }
            }
        } catch (e) { console.error('[LuxEdge] Python kapatma hatası:', e); }
        pythonProcess = null;
    }
}

// ============================================================
// PENCERE
// ============================================================

function createWindow() {
    mainWindow = new BrowserWindow({
        width: 1100,
        height: 750,
        minWidth: 850,
        minHeight: 600,
        title: "LuxEdge",
        backgroundColor: '#0d1117',
        frame: false,
        titleBarStyle: 'hidden',
        autoHideMenuBar: true,
        show: false,
        webPreferences: {
            preload: path.join(__dirname, 'preload.js'),
            contextIsolation: true,
            nodeIntegration: false
        }
    });

    mainWindow.loadFile(path.join(__dirname, 'web_ui', 'index.html'));

    mainWindow.once('ready-to-show', () => mainWindow.show());

    // X butonuna basılınca tepsiye küçült
    mainWindow.on('close', (event) => {
        if (!app.isQuitting) {
            event.preventDefault();
            mainWindow.hide();
        }
    });

    mainWindow.on('closed', () => { mainWindow = null; });
}

// ============================================================
// TEPSİ İKONU
// ============================================================

function createTray() {
    try {
        // 16x16 basit ikon oluştur
        const size = 16;
        const buf = Buffer.alloc(size * size * 4);
        for (let i = 0; i < size * size; i++) {
            const x = i % size, y = Math.floor(i / size);
            const dist = Math.sqrt((x - 8) ** 2 + (y - 8) ** 2);
            if (dist < 7) {
                const hue = (x * 360 / size) % 360;
                const [r, g, b] = hslToRgb(hue / 360, 0.8, 0.6);
                buf[i * 4] = r; buf[i * 4 + 1] = g; buf[i * 4 + 2] = b; buf[i * 4 + 3] = 255;
            }
        }

        const icon = nativeImage.createFromBuffer(buf, { width: size, height: size });
        tray = new Tray(icon);
    } catch (e) {
        console.warn('[LuxEdge] Tepsi ikonu oluşturulamadı:', e.message);
        return;
    }

    const contextMenu = Menu.buildFromTemplate([
        {
            label: '🖥️ LuxEdge Kontrol Paneli',
            click: () => {
                if (mainWindow) { mainWindow.show(); mainWindow.focus(); }
                else createWindow();
            }
        },
        { type: 'separator' },
        {
            label: '🔄 Yeniden Başlat',
            click: () => { app.isQuitting = true; killPython(); app.relaunch(); app.exit(); }
        },
        {
            label: '❌ Çıkış',
            click: () => { app.isQuitting = true; killPython(); app.quit(); }
        }
    ]);

    tray.setToolTip('LuxEdge - Çalışıyor');
    tray.setContextMenu(contextMenu);
    tray.on('double-click', () => {
        if (mainWindow) { mainWindow.show(); mainWindow.focus(); }
    });
}

function hslToRgb(h, s, l) {
    let r, g, b;
    if (s === 0) { r = g = b = l; }
    else {
        const hue2rgb = (p, q, t) => {
            if (t < 0) t += 1; if (t > 1) t -= 1;
            if (t < 1 / 6) return p + (q - p) * 6 * t;
            if (t < 1 / 2) return q;
            if (t < 2 / 3) return p + (q - p) * (2 / 3 - t) * 6;
            return p;
        };
        const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
        const p = 2 * l - q;
        r = hue2rgb(p, q, h + 1 / 3);
        g = hue2rgb(p, q, h);
        b = hue2rgb(p, q, h - 1 / 3);
    }
    return [Math.round(r * 255), Math.round(g * 255), Math.round(b * 255)];
}

// ============================================================
// IPC İŞLEYİCİLERİ
// ============================================================

function registerIpcHandlers() {
    ipcMain.handle('get-status', async () => {
        try { return await pythonGet('/api/status', 2000); }
        catch (e) { return { connection: 'bağlantı yok', running: false, last_error: "Python sunucusuna erişilemiyor..." }; }
    });

    ipcMain.handle('scan-network', async () => {
        try { return await pythonGet('/api/scan', 20000); }
        catch (e) { return { found: false, message: "Ağ taraması başarısız" }; }
    });

    ipcMain.handle('save-config', async (event, config) => {
        try { return await pythonPost('/api/config', config); }
        catch (e) { return { success: false, message: "Kayıt başarısız" }; }
    });

    ipcMain.handle('restart-app', async () => {
        setTimeout(() => { app.isQuitting = true; killPython(); app.relaunch(); app.exit(); }, 1000);
        return { success: true, message: "Yeniden başlatılıyor..." };
    });

    ipcMain.handle('wemos-restart', async () => {
        try { return await pythonPost('/api/wemos/restart', {}, 5000); }
        catch (e) { return { success: false, message: "Wemos yeniden başlatılamadı" }; }
    });

    ipcMain.handle('wemos-sleep', async () => {
        try { return await pythonPost('/api/wemos/sleep', {}, 5000); }
        catch (e) { return { success: false, message: "Wemos uyku modu değiştirilemedi" }; }
    });

    ipcMain.handle('wemos-reset-wifi', async () => {
        try { return await pythonPost('/api/wemos/reset_wifi', {}, 5000); }
        catch (e) { return { success: false, message: "Wemos sıfırlama başarısız" }; }
    });

    // Otomatik Başlatma (Sadece Electron — backend'i Electron başlatır)
    function getLinuxAutostartPath() {
        const configDir = process.env.XDG_CONFIG_HOME || path.join(os.homedir(), '.config');
        return path.join(configDir, 'autostart', 'luxedge.desktop');
    }

    ipcMain.handle('get-autostart', () => {
        if (process.platform === 'win32') {
            return { enabled: app.getLoginItemSettings().openAtLogin };
        } else {
            // Linux: Manuel desktop file kontrolü
            const desktopPath = getLinuxAutostartPath();
            return { enabled: fs.existsSync(desktopPath) };
        }
    });

    ipcMain.handle('set-autostart', (event, enabled) => {
        try {
            if (process.platform === 'win32') {
                app.setLoginItemSettings({
                    openAtLogin: enabled,
                    path: app.getPath('exe'),
                    args: []
                });
            } else {
                // Linux: Manuel XDG Autostart File
                const desktopPath = getLinuxAutostartPath();
                if (enabled) {
                    const autostartDir = path.dirname(desktopPath);
                    if (!fs.existsSync(autostartDir)) fs.mkdirSync(autostartDir, { recursive: true });

                    // AppImage kullanılıyorsa onun yolunu al, yoksa exe
                    const exePath = process.env.APPIMAGE || app.getPath('exe');

                    const desktopContent = `[Desktop Entry]
Type=Application
Name=LuxEdge
Exec="${exePath}"
Hidden=false
NoDisplay=false
X-GNOME-Autostart-enabled=true
Comment=LuxEdge Ambilight System
Terminal=false
`;
                    fs.writeFileSync(desktopPath, desktopContent);
                } else {
                    if (fs.existsSync(desktopPath)) fs.unlinkSync(desktopPath);
                }
            }
            console.log(`[LuxEdge] Otomatik başlatma: ${enabled ? 'AÇIK' : 'KAPALI'}`);
            return { success: true, enabled: enabled };
        } catch (e) {
            console.error('[LuxEdge] Otomatik başlatma ayarlanamadı:', e);
            return { success: false, message: e.message };
        }
    });

    // Pencere Kontrolleri
    ipcMain.handle('window-minimize', () => mainWindow.minimize());
    ipcMain.handle('window-maximize', () => {
        if (mainWindow.isMaximized()) mainWindow.unmaximize();
        else mainWindow.maximize();
    });
    ipcMain.handle('window-close', () => mainWindow.close());
}

// ============================================================
// UYGULAMA YAŞAM DÖNGÜSÜ
// ============================================================

app.whenReady().then(async () => {
    console.log('[LuxEdge] Başlatılıyor...');

    // Otomatik başlatma durumunu logla
    const loginSettings = app.getLoginItemSettings();
    console.log(`[LuxEdge] Otomatik başlatma: ${loginSettings.openAtLogin ? 'AÇIK' : 'KAPALI'}`);

    registerIpcHandlers();
    await startPython();
    createWindow();
    createTray();

    app.on('activate', () => {
        if (BrowserWindow.getAllWindows().length === 0) createWindow();
        else if (mainWindow) mainWindow.show();
    });
});

app.on('window-all-closed', () => { /* Tepside çalışmaya devam et */ });
app.on('before-quit', () => { app.isQuitting = true; });
app.on('will-quit', () => { killPython(); if (tray) { tray.destroy(); tray = null; } });

process.on('exit', killPython);
process.on('uncaughtException', (err) => { console.error('[LuxEdge] Hata:', err); killPython(); });
