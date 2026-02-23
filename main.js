// ============================================================
// LuxEdge - Electron Ana Süreç
// Akıllı Ekran Kenar Aydınlatma Kontrol Sistemi
// ============================================================

const { app, BrowserWindow, ipcMain, Tray, Menu, nativeImage } = require('electron');
const { spawn } = require('child_process');
const http = require('http');
const path = require('path');

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

        let executable, args;
        if (app.isPackaged) {
            // Paketlenmiş mod: extraResources içindeki .exe'yi kullan
            executable = path.join(process.resourcesPath, 'lush_backend.exe');
            args = ['--no-tray'];
        } else {
            // Geliştirme modu: python scripti kullan
            executable = 'python';
            args = ['ambilight_pc.py', '--no-tray'];
        }

        pythonProcess = spawn(executable, args, {
            cwd: app.isPackaged ? path.dirname(executable) : __dirname,
            env: { ...process.env, PYTHONUNBUFFERED: "1" },
            windowsHide: true
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
                spawn('taskkill', ['/pid', pythonProcess.pid.toString(), '/f', '/t'], { windowsHide: true });
            } else {
                pythonProcess.kill('SIGTERM');
            }
        } catch (e) { /* */ }
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

    // Otomatik başlatma: SADECE ilk kurulumda ayarla
    // Kullanıcı görev yöneticisinden devre dışı bırakırsa tekrar açma
    const fs = require('fs');
    const firstRunFlag = path.join(app.getPath('userData'), '.startup_configured');
    if (!fs.existsSync(firstRunFlag)) {
        // İlk çalıştırma: otomatik başlatmayı ayarla
        app.setLoginItemSettings({
            openAtLogin: true,
            path: app.getPath('exe'),
            args: []
        });
        // Flag dosyası oluştur ki bir daha dokunmasın
        try { fs.writeFileSync(firstRunFlag, 'configured'); } catch (e) { }
        console.log('[LuxEdge] Otomatik başlatma ilk kez ayarlandı.');
    } else {
        console.log('[LuxEdge] Otomatik başlatma daha önce ayarlanmış, dokunulmuyor.');
    }

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
