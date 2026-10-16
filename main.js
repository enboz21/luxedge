// ============================================================
// LuxEdge - Electron Ana Süreç
// Akıllı Ekran Kenar Aydınlatma Kontrol Sistemi
// ============================================================

const { app, BrowserWindow, ipcMain, Tray, Menu, nativeImage } = require('electron');
const { spawn } = require('child_process');
const http = require('http');
const net = require('net');
const path = require('path');
const fs = require('fs');

let mainWindow;
let pythonProcess;
// Backend başlatılamazsa son çıktı satırları ve çıkış kodu arayüzde gösterilir.
const backendOutput = [];
let backendExit = null;
function rememberBackendOutput(text) {
    for (const line of text.split(/\r?\n/)) {
        if (!line.trim()) continue;
        backendOutput.push(line.trim());
        if (backendOutput.length > 40) backendOutput.shift();
    }
}
let tray = null;
let shutdownPromise = null;
const ownsInstance = app.requestSingleInstanceLock();
let PYTHON_PORT = Number(process.env.LUXEDGE_BACKEND_PORT || 8888);
const PORT_PINNED = Boolean(process.env.LUXEDGE_BACKEND_PORT);
const APP_VERSION = JSON.parse(fs.readFileSync(path.join(__dirname, 'package.json'), 'utf8')).luxedgeVersion;

// Kurulu (tepsi) LuxEdge 8888'i tutuyorsa yeni backend başka porta kaymasın diye
// portu Electron seçer ve backend'e ortam değişkeniyle verir.
function findFreePort(start, attempts = 20) {
    return new Promise(resolve => {
        const tryPort = port => {
            if (port >= start + attempts) { resolve(start); return; }
            const probe = net.createServer();
            probe.unref();
            probe.once('error', () => tryPort(port + 1));
            probe.listen({ port, host: '127.0.0.1' }, () => probe.close(() => resolve(port)));
        };
        tryPort(start);
    });
}
const START_IN_TRAY = '--start-in-tray';
let showOnReady = !(process.argv || []).includes(START_IN_TRAY);

function loginOptions(args = [START_IN_TRAY]) {
    return { path: app.getPath('exe'), args };
}

function autostartEnabled() {
    const settings = app.getLoginItemSettings(loginOptions());
    return settings.openAtLogin && settings.executableWillLaunchAtLogin !== false;
}

function migrateAutostart() {
    if (!app.isPackaged) return;
    const legacy = app.getLoginItemSettings(loginOptions([]));
    if (legacy.openAtLogin && legacy.executableWillLaunchAtLogin !== false) {
        app.setLoginItemSettings({ ...loginOptions(), openAtLogin: true });
    }
}

function showControlPanel() {
    showOnReady = true;
    if (!mainWindow) return;
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.show();
    mainWindow.focus();
}

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

function pythonPut(endpoint, body = {}, timeout = 5000) {
    return pythonRequest(endpoint, 'PUT', body, timeout);
}

function pythonDelete(endpoint, timeout = 5000) {
    return pythonRequest(endpoint, 'DELETE', null, timeout);
}

function pythonRequest(endpoint, method, body, timeout) {
    return new Promise((resolve, reject) => {
        const jsonData = body === null ? null : JSON.stringify(body);
        const headers = { 'Content-Type': 'application/json' };
        if (jsonData !== null) headers['Content-Length'] = Buffer.byteLength(jsonData);
        const options = {
            hostname: '127.0.0.1', port: PYTHON_PORT,
            path: endpoint, method, timeout, headers
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
        if (jsonData !== null) req.write(jsonData);
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
            executable = path.join(process.resourcesPath, 'lush_backend.exe');
            args = ['--no-tray'];
            cwd = process.resourcesPath;
            if (!fs.existsSync(executable)) {
                console.error('[LuxEdge] Windows backend bulunamadı. Kurulumu onarın.');
                resolve(false);
                return;
            }
        } else {
            const venv = path.join(__dirname, '.venv', 'Scripts', 'python.exe');
            executable = fs.existsSync(venv) ? venv : 'python';
            args = ['ambilight_pc.py', '--no-tray'];
            cwd = __dirname;
        }

        console.log(`[LuxEdge] Çalıştırılıyor: ${executable} ${args.join(' ')}`);
        console.log(`[LuxEdge] Çalışma dizini: ${cwd}`);

        pythonProcess = spawn(executable, args, {
            cwd: cwd,
            env: { ...process.env, PYTHONUNBUFFERED: "1", LUXEDGE_BACKEND_PORT: String(PYTHON_PORT) },
            windowsHide: true,
            detached: false
        });

        backendOutput.length = 0;
        backendExit = null;
        rememberBackendOutput(`Komut: ${executable} ${args.join(' ')} (port ${PYTHON_PORT})`);

        pythonProcess.stdout.on('data', (data) => {
            const msg = data.toString().trim();
            if (msg) { console.log(`[Python] ${msg}`); rememberBackendOutput(msg); }
        });

        pythonProcess.stderr.on('data', (data) => {
            const msg = data.toString().trim();
            if (msg) { console.error(`[Python] ${msg}`); rememberBackendOutput(msg); }
        });

        const startedProcess = pythonProcess;
        pythonProcess.on('close', (code) => {
            if (pythonProcess === startedProcess) pythonProcess = null;
            backendExit = code;
            console.log(`[LuxEdge] Python kapandı (kod: ${code})`);
        });

        pythonProcess.on('error', (err) => {
            backendExit = -1;
            rememberBackendOutput(`Başlatma hatası: ${err.message}`);
            console.error(`[LuxEdge] Python başlatılamadı: ${err.message}`);
        });

        // Python sunucusunun hazır olmasını bekle
        let attempts = 0;
        const checkReady = () => {
            if (app.isQuitting || pythonProcess !== startedProcess) { resolve(false); return; }
            attempts++;
            pythonGet('/api/status', 2000)
                .then(status => {
                    console.log(`[LuxEdge] Python hazır! (${attempts} deneme, port ${PYTHON_PORT})`);
                    if (status && status.backend_version && status.backend_version !== APP_VERSION) {
                        console.warn(`[LuxEdge] Backend sürümü ${status.backend_version}, uygulama ${APP_VERSION}. Eski bir backend yanıt veriyor olabilir.`);
                    }
                    resolve(true);
                })
                .catch(() => {
                    if (attempts < 60) setTimeout(checkReady, 1000);
                    else { console.warn('[LuxEdge] Python zaman aşımı'); resolve(false); }
                });
        };
        setTimeout(checkReady, 1000);
    });
}

function killPython() {
    const child = pythonProcess;
    pythonProcess = null;
    if (!child || !child.pid || child.exitCode !== null) return Promise.resolve();
    return new Promise(resolve => {
        let finished = false;
        let killer;
        const finish = () => {
            if (finished) return;
            finished = true;
            clearTimeout(timer);
            resolve();
        };
        const timer = setTimeout(() => {
            if (killer) killer.kill();
            console.error('[LuxEdge] Backend kapatma zaman aşımı');
            finish();
        }, 5000);
        try {
            killer = spawn('taskkill', ['/pid', String(child.pid), '/f', '/t'], { windowsHide: true, stdio: 'ignore' });
            killer.once('close', finish);
            killer.once('error', finish);
        } catch (error) { finish(); }
    });
}

function requestQuit(relaunch = false) {
    if (shutdownPromise) return shutdownPromise;
    app.isQuitting = true;
    shutdownPromise = killPython().then(() => {
        if (tray) { tray.destroy(); tray = null; }
        if (relaunch) app.relaunch();
        app.exit(0);
    });
    return shutdownPromise;
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

    for (const event of ['show', 'hide', 'minimize', 'restore']) {
        mainWindow.on(event, () => {
            mainWindow.webContents.send('window-visibility', mainWindow.isVisible() && !mainWindow.isMinimized());
        });
    }
    mainWindow.loadFile(path.join(__dirname, 'web_ui', 'index.html'));

    mainWindow.once('ready-to-show', () => {
        if (showOnReady || !tray) showControlPanel();
    });

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

        const icon = nativeImage.createFromBitmap(buf, { width: size, height: size });
        tray = new Tray(icon);
    } catch (e) {
        console.warn('[LuxEdge] Tepsi ikonu oluşturulamadı:', e.message);
        return;
    }

    const contextMenu = Menu.buildFromTemplate([
        {
            label: 'LuxEdge Kontrol Paneli',
            click: () => {
                showControlPanel();
                if (!mainWindow) createWindow();
            }
        },
        { type: 'separator' },
        {
            label: 'Yeniden Başlat',
            click: () => { requestQuit(true); }
        },
        {
            label: 'Çıkış',
            click: () => { requestQuit(); }
        }
    ]);

    tray.setToolTip('LuxEdge - Çalışıyor');
    tray.setContextMenu(contextMenu);
    tray.on('double-click', showControlPanel);
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
    ipcMain.handle('refresh-monitors', async () => {
        try { return await pythonPost('/api/monitors/refresh', {}, 5000); }
        catch (error) { return { success: false, message: 'Monitör listesi güncellenemedi.' }; }
    });
    ipcMain.handle('get-status', async () => {
        // The backend also serves the bounded manual UDP/HTTP test on this server.
        try { return await pythonGet('/api/status', 7000); }
        catch (e) {
            return {
                connection: 'bağlantı yok', connectivity: { state: 'backend_unavailable' }, running: false,
                last_error: "Python sunucusuna erişilemiyor...",
                backend_port: PYTHON_PORT, backend_exit: backendExit, backend_output: backendOutput.slice(-12),
            };
        }
    });

    ipcMain.handle('scan-network', async () => {
        try { return await pythonGet('/api/scan', 20000); }
        catch (e) { return { found: false, message: "Ağ taraması başarısız" }; }
    });

    ipcMain.handle('save-config', async (event, config) => {
        try { return await pythonPost('/api/config', config); }
        catch (e) { return { success: false, message: "Kayıt başarısız" }; }
    });

    ipcMain.handle('get-devices', async () => {
        try { return await pythonGet('/api/devices', 2000); }
        catch (e) { return { success: false, devices: [], message: "Cihazlar yüklenemedi" }; }
    });

    ipcMain.handle('get-devices-overview', async () => {
        try { return await pythonGet('/api/devices/overview', 2000); }
        catch (e) { return { success: false, message: 'Cihaz durumları alınamadı' }; }
    });

    ipcMain.handle('scan-devices', async () => {
        try { return await pythonGet('/api/devices/scan', 6000); }
        catch (e) { return { success: false, devices: [], message: "Cihaz taraması başarısız" }; }
    });

    ipcMain.handle('validate-devices', async () => {
        try { return await pythonGet('/api/devices/validation', 8000); }
        catch (e) { return { success: false, devices: [], message: "Cihaz doğrulaması başarısız" }; }
    });

    ipcMain.handle('set-multi-device-mode', async (event, enabled) => {
        try { return await pythonPost('/api/devices/mode', { enabled }, 10000); }
        catch (e) { return { success: false, message: "Çoklu mod değiştirilemedi" }; }
    });

    ipcMain.handle('create-device', async (event, device) => {
        try { return await pythonPost('/api/devices', device); }
        catch (e) { return { success: false, message: "Cihaz kaydedilemedi" }; }
    });

    ipcMain.handle('update-device', async (event, id, device) => {
        try { return await pythonPut(`/api/devices/${encodeURIComponent(id)}`, device); }
        catch (e) { return { success: false, message: "Cihaz güncellenemedi" }; }
    });

    ipcMain.handle('delete-device', async (event, id) => {
        try { return await pythonDelete(`/api/devices/${encodeURIComponent(id)}`); }
        catch (e) { return { success: false, message: "Cihaz silinemedi" }; }
    });

    ipcMain.handle('sync-device', async (event, id, options) => {
        try { return await pythonPost(`/api/devices/${encodeURIComponent(id)}/sync`, options || {}, 15000); }
        catch (e) { return { success: false, message: "Wemos ile eşitleme başarısız" }; }
    });

    ipcMain.handle('get-logs', async () => {
        try { return await pythonGet('/api/logs', 2000); }
        catch (e) { return { logs: [] }; }
    });

    ipcMain.handle('test-wemos-connection', async (event, target) => {
        const query = target && target.ip ? `?ip=${encodeURIComponent(target.ip)}&port=${encodeURIComponent(target.port || 7777)}` : '';
        try { return await pythonGet('/api/test-wemos-connection' + query, 7000); }
        catch (e) { return { success: false, message: "Test başarısız", udp_ok: false, http_ok: false }; }
    });

    ipcMain.handle('get-wemos-status', async (event, target) => {
        const query = target && target.ip ? `?ip=${encodeURIComponent(target.ip)}` : '';
        try { return await pythonGet('/api/wemos/status' + query, 6000); }
        catch (e) { return { success: false, message: "Wemos durumu alınamadı" }; }
    });

    ipcMain.handle('restart-app', async () => {
        setTimeout(() => { requestQuit(true); }, 1000);
        return { success: true, message: "Yeniden başlatılıyor..." };
    });

    ipcMain.handle('wemos-restart', async (event, target) => {
        try { return await pythonPost('/api/wemos/restart', target || {}, 5000); }
        catch (e) { return { success: false, message: "Wemos yeniden başlatılamadı" }; }
    });

    ipcMain.handle('wemos-sleep', async (event, target) => {
        try { return await pythonPost('/api/wemos/sleep', target || {}, 5000); }
        catch (e) { return { success: false, message: "Wemos uyku modu değiştirilemedi" }; }
    });

    ipcMain.handle('wemos-reset-wifi', async (event, target) => {
        try { return await pythonPost('/api/wemos/reset_wifi', target || {}, 5000); }
        catch (e) { return { success: false, message: "Wemos sıfırlama başarısız" }; }
    });

    // Otomatik Başlatma (Sadece Electron — backend'i Electron başlatır)
    ipcMain.handle('get-autostart', () => ({ enabled: autostartEnabled() }));
    ipcMain.handle('set-autostart', (event, enabled) => {
        try {
            app.setLoginItemSettings({ ...loginOptions(), openAtLogin: enabled });
            return { success: true, enabled };
        } catch (error) { return { success: false, message: error.message }; }
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

if (!ownsInstance) {
    app.quit();
} else {
app.on('second-instance', (event, argv = []) => {
    if (!argv.includes(START_IN_TRAY)) showControlPanel();
});
app.whenReady().then(async () => {
    console.log('[LuxEdge] Başlatılıyor...');

    // Otomatik başlatma durumunu logla
    try { migrateAutostart(); }
    catch (error) { console.warn('[LuxEdge] Başlangıç kaydı güncellenemedi:', error.message); }

    registerIpcHandlers();
    if (!PORT_PINNED) {
        const chosen = await findFreePort(PYTHON_PORT);
        if (chosen !== PYTHON_PORT) console.warn(`[LuxEdge] Port ${PYTHON_PORT} dolu (başka bir LuxEdge çalışıyor olabilir); ${chosen} kullanılacak.`);
        PYTHON_PORT = chosen;
    }
    await startPython();
    if (app.isQuitting) return;
    createTray();
    createWindow();

    app.on('activate', () => {
        showControlPanel();
        if (BrowserWindow.getAllWindows().length === 0) createWindow();
    });
});

app.on('window-all-closed', () => { /* Tepside çalışmaya devam et */ });
app.on('before-quit', event => {
    event.preventDefault();
    requestQuit();
});
process.on('uncaughtException', err => { console.error('[LuxEdge] Hata:', err); requestQuit(); });
}
