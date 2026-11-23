// ============================================================
// LuxEdge - Electron Ana Süreç
// Akıllı Ekran Kenar Aydınlatma Kontrol Sistemi
// ============================================================

const { app, BrowserWindow, dialog, ipcMain, Tray, Menu, nativeImage } = require('electron');
const { spawn } = require('child_process');
const http = require('http');
const net = require('net');
const path = require('path');
const fs = require('fs');
const { randomBytes, randomUUID } = require('crypto');
const { pathToFileURL, URLSearchParams } = require('url');
const logArchive = require(path.join(__dirname, 'log_archive.js'));
const liveText = require(path.join(__dirname, 'live_log.js'));

let mainWindow;
let pythonProcess;
// Backend başlatılamazsa son çıktı satırları ve çıkış kodu arayüzde gösterilir.
const backendOutput = [];
let backendExit = null;
const LOG_LIMIT = 1000;
let electronLogs;
let electronLogsReadable = true;
let electronLogsWritable = true;
let electronLogNeedsSeparator = false;
let electronLastOrder = 0;
let retentionWork = Promise.resolve();
let liveLogFile = null;
let liveLogAction = false;
let liveLogStartWork = null;
let incidentLogAction = false;
let incidentLogWork = null;

function logDirectory() {
    return path.join(process.env.APPDATA || app.getPath('appData'), 'LuxEdge');
}

function electronLogPath() {
    return path.join(logDirectory(), 'luxedge-electron.jsonl');
}

function readElectronLogTail(file) {
    const descriptor = fs.openSync(file, 'r');
    try {
        const size = fs.fstatSync(descriptor).size;
        const offset = Math.max(0, size - logArchive.SEGMENT_BYTES);
        const buffer = Buffer.alloc(size - offset);
        const length = fs.readSync(descriptor, buffer, 0, buffer.length, offset);
        const contents = buffer.toString('utf8', 0, length);
        if (!offset) return contents;
        const firstNewline = contents.indexOf('\n');
        return firstNewline < 0 ? '' : contents.slice(firstNewline + 1);
    } finally {
        fs.closeSync(descriptor);
    }
}

function readElectronLogs() {
    if (electronLogs) return electronLogs;
    electronLogs = [];
    const file = electronLogPath();
    let files;
    try {
        files = logArchive.sortedElectronFiles(logDirectory());
    } catch (error) {
        files = [];
        electronLogsReadable = false;
        console.warn('[LuxEdge] Electron günlük dizini okunamadı:', error.message);
    }
    for (const segment of files) {
        if (electronLogs.length >= LOG_LIMIT) break;
        try {
            const contents = readElectronLogTail(segment);
            if (segment === file && !contents.endsWith('\n')) electronLogNeedsSeparator = true;
            const lines = contents.split(/\r?\n/);
            for (let n = lines.length - 1; n >= 0 && electronLogs.length < LOG_LIMIT; n--) {
                if (!lines[n]) continue;
                try {
                    const entry = JSON.parse(lines[n]);
                    if (entry?.source === 'electron' && typeof entry.timestamp === 'string') {
                        electronLogs.push(entry);
                        const order = logArchive.rowOrder(entry);
                        if (Number.isSafeInteger(order)) electronLastOrder = Math.max(electronLastOrder, order);
                    }
                } catch {
                    // A partial line from an interrupted write does not invalidate the rest.
                }
            }
        } catch (error) {
            if (error.code !== 'ENOENT') {
                electronLogsReadable = false;
                console.warn('[LuxEdge] Electron günlük dosyası okunamadı:', error.message);
            }
        }
    }
    return electronLogs.reverse();
}

function logElectron(level, code, msg, explanation, details = '') {
    const recent = readElectronLogs();
    electronLastOrder = Math.max(Date.now() * 1000, electronLastOrder + 1);
    const entry = {
        id: 'electron:' + randomUUID(),
        archive_order_us: electronLastOrder,
        timestamp: new Date().toISOString(),
        level,
        msg,
        source: 'electron',
        code,
        explanation,
        details: String(details).slice(0, 2000)
    };
    recent.push(entry);
    if (recent.length > LOG_LIMIT) recent.shift();
    if (liveLogFile) liveLogFile.addElectron(entry);
    const file = electronLogPath();
    const line = JSON.stringify(entry) + '\n';
    try {
        fs.mkdirSync(path.dirname(file), { recursive: true });
        const info = fs.existsSync(file) ? fs.lstatSync(file) : null;
        if (info && !info.isFile()) throw new Error('Günlük dosyası normal bir dosya değil.');
        if (info?.size && info.size + Buffer.byteLength(line) + 1 > logArchive.SEGMENT_BYTES) {
            logArchive.rotateElectronLog(file, info.size);
            electronLogNeedsSeparator = false;
            scheduleLogPrune();
        }
        fs.appendFileSync(file, (electronLogNeedsSeparator ? '\n' : '') + line, 'utf8');
        if (!info) scheduleLogPrune();
        electronLogNeedsSeparator = false;
        electronLogsWritable = true;
    } catch (error) {
        electronLogsWritable = false;
        electronLogNeedsSeparator = true;
        console.warn('[LuxEdge] Electron günlüğüne yazılamadı:', error.message);
    }
}

function scheduleLogPrune() {
    retentionWork = retentionWork.then(() => logArchive.prune(logDirectory())).catch(error => {
        console.warn('[LuxEdge] Günlük saklama sınırı uygulanamadı:', error.message);
    });
}
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
const CONTROL_PANEL_FILE = path.join(__dirname, 'web_ui', 'index.html');
const CONTROL_PANEL_URL = pathToFileURL(CONTROL_PANEL_FILE).href;
const WINDOWS_DIAGNOSTICS_TOKEN = randomBytes(32).toString('hex');
const LIVE_LOG_TOKEN = randomBytes(32).toString('hex');
const WINDOWS_DIAGNOSTICS_ENDPOINT = '/api/windows-diagnostics';
const WINDOWS_DIAGNOSTICS_ROUTES = {
    status: { endpoint: WINDOWS_DIAGNOSTICS_ENDPOINT, method: 'GET', body: null },
    start: { endpoint: WINDOWS_DIAGNOSTICS_ENDPOINT + '/start', method: 'POST', body: {} },
    stop: { endpoint: WINDOWS_DIAGNOSTICS_ENDPOINT + '/stop', method: 'POST', body: {} },
};
const WINDOWS_AUDIT_SUBCATEGORIES = {
    '5152': '0cce9225-69ae-11d9-bed3-505054503030',
    '5157': '0cce9226-69ae-11d9-bed3-505054503030',
};
let windowsDiagnosticsAction = false;
let windowsDiagnosticsCleanupNeeded = false;

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

function pythonRequest(endpoint, method, body, timeout, extraHeaders = {}) {
    return new Promise((resolve, reject) => {
        const jsonData = body === null ? null : JSON.stringify(body);
        const headers = { 'Content-Type': 'application/json', ...extraHeaders };
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
        logElectron('INFO', 'BACKEND_STARTING', 'Arka plan hizmeti başlatılıyor.',
            'LuxEdge, PC hizmetini başlatmayı deniyor.');

        let executable, args, cwd;

        if (app.isPackaged) {
            // Paketlenmiş mod
            executable = path.join(process.resourcesPath, 'lush_backend.exe');
            args = ['--no-tray'];
            cwd = process.resourcesPath;
            if (!fs.existsSync(executable)) {
                console.error('[LuxEdge] Windows backend bulunamadı. Kurulumu onarın.');
                logElectron('ERROR', 'BACKEND_FILE_MISSING', 'Arka plan hizmetinin dosyası bulunamadı.',
                    'LuxEdge kurulum dizininde hizmeti başlatmak için gereken dosya yok.', 'lush_backend.exe');
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
            env: {
                ...process.env, PYTHONUNBUFFERED: "1", LUXEDGE_BACKEND_PORT: String(PYTHON_PORT),
                LUXEDGE_WINDOWS_DIAGNOSTICS_TOKEN: WINDOWS_DIAGNOSTICS_TOKEN,
                LUXEDGE_LIVE_LOG_TOKEN: LIVE_LOG_TOKEN,
            },
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
        let spawnFailed = false;
        pythonProcess.on('close', (code) => {
            const wasActive = pythonProcess === startedProcess;
            if (wasActive) pythonProcess = null;
            backendExit = code;
            console.log(`[LuxEdge] Python kapandı (kod: ${code})`);
            if (!app.isQuitting && !spawnFailed && wasActive) {
                logElectron('ERROR', 'BACKEND_EXITED', 'Arka plan hizmeti durdu.',
                    'LuxEdge çalışırken hizmet süreci kapandı.',
                    [`Çıkış kodu: ${code ?? 'bilinmiyor'}`, ...backendOutput.slice(-4)].join('\n'));
            }
        });

        pythonProcess.on('error', (err) => {
            spawnFailed = true;
            backendExit = -1;
            rememberBackendOutput(`Başlatma hatası: ${err.message}`);
            console.error(`[LuxEdge] Python başlatılamadı: ${err.message}`);
            logElectron('ERROR', 'BACKEND_SPAWN_FAILED', 'Arka plan hizmeti başlatılamadı.',
                'İşletim sistemi hizmet sürecini başlatamadı.', err.message);
        });

        // Python sunucusunun hazır olmasını bekle
        let attempts = 0;
        const checkReady = () => {
            if (app.isQuitting || pythonProcess !== startedProcess) { resolve(false); return; }
            attempts++;
            pythonGet('/api/status', 2000)
                .then(status => {
                    console.log(`[LuxEdge] Python hazır! (${attempts} deneme, port ${PYTHON_PORT})`);
                    logElectron('INFO', 'BACKEND_READY', 'Arka plan hizmeti hazır.',
                        'PC hizmeti çalışma durumu sorgusuna yanıt verdi.');
                    if (status && status.backend_version && status.backend_version !== APP_VERSION) {
                        console.warn(`[LuxEdge] Backend sürümü ${status.backend_version}, uygulama ${APP_VERSION}. Eski bir backend yanıt veriyor olabilir.`);
                        logElectron('WARNING', 'BACKEND_VERSION_MISMATCH', 'Uygulama ve hizmet sürümleri farklı.',
                            'Hizmetin bildirdiği sürüm, açık olan LuxEdge uygulamasının sürümünden farklı.',
                            `Hizmet: ${status.backend_version}, uygulama: ${APP_VERSION}`);
                    }
                    resolve(true);
                })
                .catch(() => {
                    if (attempts < 60) setTimeout(checkReady, 1000);
                    else {
                        console.warn('[LuxEdge] Python zaman aşımı');
                        logElectron('ERROR', 'BACKEND_START_TIMEOUT', 'Arka plan hizmeti zamanında yanıt vermedi.',
                            'LuxEdge, başlatma süresi içinde hizmetten çalışma durumu alamadı.',
                            backendOutput.slice(-4).join('\n'));
                        resolve(false);
                    }
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
    const stopBackend = () => {
        const stopLive = liveLogStartWork ? liveLogStartWork.then(session => session.stop()) : liveLogFile?.stop();
        return stopLive || incidentLogWork ? Promise.allSettled([stopLive, incidentLogWork]).then(killPython) : killPython();
    };
    const closeBackend = windowsDiagnosticsCleanupNeeded ? stopWindowsDiagnosticsOnExit().then(stopBackend) : stopBackend();
    shutdownPromise = closeBackend.then(() => {
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
    mainWindow.loadFile(CONTROL_PANEL_FILE);

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

function trustedControlPanelFrame(event) {
    if (!event || !mainWindow || mainWindow.isDestroyed()) return false;
    const contents = mainWindow.webContents;
    const frame = event.senderFrame;
    return event.sender === contents && frame === contents.mainFrame && frame?.url === CONTROL_PANEL_URL;
}

function readLiveLogJournal(since, through, after) {
    if (!pythonProcess || pythonProcess.exitCode !== null) return Promise.resolve(null);
    const query = new URLSearchParams({ since, through, after });
    return pythonRequest(`/api/live-log-rows?${query}`, 'GET', null, 1000,
        { 'X-LuxEdge-Live-Log-Token': LIVE_LOG_TOKEN });
}

async function startLiveLogFile(event) {
    if (!trustedControlPanelFrame(event) || app.isQuitting) return { state: 'error', message: 'Günlük isteği doğrulanamadı.' };
    if (liveLogAction || liveLogFile?.state === 'writing') return liveLogFile?.status() || { state: 'idle' };
    liveLogAction = true;
    let file;
    try {
        file = await liveText.chooseTxt(dialog, mainWindow, app.getPath('desktop'), 'luxedge-canli-gunluk');
        if (!file || !trustedControlPanelFrame(event) || app.isQuitting) return { state: 'cancelled' };
        liveLogStartWork = liveText.LiveLogFile.start(file, logDirectory(), readLiveLogJournal);
        const session = await liveLogStartWork;
        if (app.isQuitting) return await session.stop();
        liveLogFile = session;
        for (const row of readElectronLogs()) if (logArchive.rowOrder(row) >= session.since) session.addElectron(row);
        return session.status();
    } catch (error) {
        return { state: 'error', path: file, message: liveText.fileError(error) };
    } finally {
        liveLogAction = false;
        liveLogStartWork = null;
    }
}

async function stopLiveLogFile(event) {
    if (!trustedControlPanelFrame(event)) return { state: 'error', message: 'Günlük isteği doğrulanamadı.' };
    return liveLogFile ? liveLogFile.stop() : { state: 'idle' };
}

async function saveIncidentLog(event, options) {
    if (!trustedControlPanelFrame(event) || app.isQuitting) return { state: 'error', message: 'Günlük isteği doğrulanamadı.' };
    if (incidentLogAction) return { state: 'error', message: 'Olay dosyası hazırlanıyor.' };
    const { from, to, selectedKey } = options || {};
    if (!Number.isSafeInteger(from) || !Number.isSafeInteger(to) || from < 0 || to < from || to - from > 60_000
            || to > 8_640_000_000_000_000 || (selectedKey != null && (typeof selectedKey !== 'string' || selectedKey.length > 128))) {
        return { state: 'error', message: 'Geçersiz olay zaman aralığı.' };
    }
    incidentLogAction = true;
    let file;
    try {
        file = await liveText.chooseTxt(dialog, mainWindow, app.getPath('desktop'), 'luxedge-olay-baglami');
        if (!file || !trustedControlPanelFrame(event) || app.isQuitting) return { state: 'cancelled' };
        incidentLogWork = (async () => {
            const backend = await pythonGet('/api/logs?limit=' + LOG_LIMIT, 1000).catch(() => null);
            const recent = [...readElectronLogs(), ...(Array.isArray(backend?.logs) ? backend.logs : [])];
            return liveText.writeIncident(file, logDirectory(), from, to, selectedKey, recent);
        })();
        return await incidentLogWork;
    } catch (error) {
        return { state: 'error', path: file, message: liveText.fileError(error) };
    } finally {
        incidentLogAction = false;
        incidentLogWork = null;
    }
}

async function windowsDiagnosticsRequest(action, timeout = 5000) {
    if (!pythonProcess || pythonProcess.exitCode !== null) {
        return { state: 'unavailable', reason: 'backend_unavailable' };
    }
    const headers = { 'X-LuxEdge-Windows-Diagnostics-Token': WINDOWS_DIAGNOSTICS_TOKEN };
    try {
        const { endpoint, method, body } = WINDOWS_DIAGNOSTICS_ROUTES[action];
        const result = await pythonRequest(endpoint, method, body, timeout, headers);
        if (result && typeof result.state === 'string') return result;
    } catch { /* The service may be stopping or temporarily unreachable. */ }
    return { state: 'unavailable', reason: 'backend_unavailable' };
}

function windowsAuditConsentLine(eventId, current) {
    const prefix = eventId + ' başarısızlık denetimi (' + WINDOWS_AUDIT_SUBCATEGORIES[eventId] + ')';
    if (current === 'enabled') return prefix + ' açık → açık';
    if (current === 'disabled') return prefix + ' kapalı → oturum boyunca açık';
    return prefix + ' mevcut değer bilinmiyor → yönetici okumasında kapalı olduğu doğrulanırsa açılır';
}

async function startWindowsDiagnostics(event) {
    if (!trustedControlPanelFrame(event) || !mainWindow.isVisible() || app.isQuitting) {
        return { state: 'unavailable', reason: 'untrusted_frame' };
    }
    if (windowsDiagnosticsAction) return { state: 'unavailable', reason: 'busy' };
    windowsDiagnosticsAction = true;
    try {
        const status = await windowsDiagnosticsRequest('status');
        if (!['idle', 'restored', 'cancelled', 'failed'].includes(status.state)) return status;
        if (!trustedControlPanelFrame(event) || !mainWindow.isVisible() || app.isQuitting) {
            return { state: 'cancelled', reason: 'confirmation_cancelled' };
        }
        const audit = status.system_audit_failure;
        const confirmation = await dialog.showMessageBox(mainWindow, {
            type: 'question',
            title: 'Windows ağ tanılaması',
            message: 'Geçici Windows tanılaması başlatılsın mı?',
            detail: [
                'Oturum en fazla 5 dakika sürer. Gerektiğinde Windows yönetici onayı ister.',
                windowsAuditConsentLine('5152', audit?.['5152']),
                windowsAuditConsentLine('5157', audit?.['5157']),
                'Geçici ayarlar ancak yönetici her iki önceki değeri okuyabildiğinde uygulanır. Oturum bitince değiştirilen değerler geri yüklenir.',
                'Gerekirse yönetici yardımıyla yalnız LuxEdge bağlantısıyla eşleşen Security olayları okunur.',
            ].join('\n\n'),
            buttons: ['Vazgeç', 'Oturumu başlat'],
            defaultId: 0,
            cancelId: 0,
            noLink: true,
        });
        if (confirmation.response !== 1 || !trustedControlPanelFrame(event)
                || !mainWindow.isVisible() || app.isQuitting) {
            return { state: 'cancelled', reason: 'confirmation_cancelled' };
        }
        windowsDiagnosticsCleanupNeeded = true;
        return await windowsDiagnosticsRequest('start');
    } catch {
        return { state: 'unavailable', reason: 'confirmation_unavailable' };
    } finally {
        windowsDiagnosticsAction = false;
    }
}

async function stopWindowsDiagnostics(event) {
    if (!trustedControlPanelFrame(event) || app.isQuitting) {
        return { state: 'unavailable', reason: 'untrusted_frame' };
    }
    if (windowsDiagnosticsAction) return { state: 'unavailable', reason: 'busy' };
    windowsDiagnosticsAction = true;
    try {
        const result = await windowsDiagnosticsRequest('stop');
        if (result.state === 'restored' || result.state === 'idle') windowsDiagnosticsCleanupNeeded = false;
        return result;
    } finally {
        windowsDiagnosticsAction = false;
    }
}

async function stopWindowsDiagnosticsOnExit() {
    let status = await windowsDiagnosticsRequest('stop', 500);
    for (let check = 0; check < 3 && status.state === 'stopping'; check++) {
        await new Promise(resolve => setTimeout(resolve, 1000));
        status = await windowsDiagnosticsRequest('status', 500);
    }
    if (['idle', 'restored', 'cancelled', 'failed', 'restore_conflict'].includes(status.state)) return;
    logElectron('WARNING', 'WINDOWS_DIAGNOSTICS_SHUTDOWN',
        'Windows denetim ayarlarının geri yüklenmesi doğrulanamadı.',
        'Oturumun geri yükleme durumunu Günlük ekranından kontrol edin.', status.reason || status.state);
}

function registerIpcHandlers() {
    ipcMain.handle('get-live-log-file-state', event => trustedControlPanelFrame(event)
        ? (liveLogFile?.status() || { state: 'idle' }) : { state: 'error', message: 'Günlük isteği doğrulanamadı.' });
    ipcMain.handle('start-live-log-file', startLiveLogFile);
    ipcMain.handle('stop-live-log-file', stopLiveLogFile);
    ipcMain.handle('save-incident-log', saveIncidentLog);
    ipcMain.handle('windows-diagnostics-status', event => trustedControlPanelFrame(event)
        ? windowsDiagnosticsRequest('status') : { state: 'unavailable', reason: 'untrusted_frame' });
    ipcMain.handle('windows-diagnostics-start', startWindowsDiagnostics);
    ipcMain.handle('windows-diagnostics-stop', stopWindowsDiagnostics);
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
        let backend;
        try {
            backend = await pythonGet('/api/logs?limit=' + LOG_LIMIT, 2000);
        } catch {
            backend = { available: false };
        }
        const logs = [...(backend?.logs || []), ...readElectronLogs()];
        logs.sort((a, b) => (Date.parse(a.timestamp) || 0) - (Date.parse(b.timestamp) || 0));
        return {
            ...backend,
            available: Array.isArray(backend?.logs) && backend.available !== false,
            electronPersistent: electronLogsReadable && electronLogsWritable,
            logs: logs.slice(-LOG_LIMIT)
        };
    });

    ipcMain.handle('get-log-archive', async (event, options) => {
        if (!trustedControlPanelFrame(event)) return { available: false, logs: [], message: 'Günlük isteği doğrulanamadı.' };
        try { return await logArchive.archivePage(logDirectory(), options || {}); }
        catch (error) { return { available: false, logs: [], has_more: false, next_cursor: null,
            message: `Günlük geçmişi açılamadı: ${error.message}` }; }
    });

    ipcMain.handle('set-log-retention', async (event, options) => {
        if (!trustedControlPanelFrame(event)) return { success: false, message: 'Günlük ayarı isteği doğrulanamadı.' };
        const directory = logDirectory();
        const limit = options?.limit_bytes;
        const pending = retentionWork.then(() => logArchive.setRetention(directory, limit));
        retentionWork = pending.catch(() => {});
        try {
            await pending;
            const result = await logArchive.archivePage(directory, { limit: 1 });
            return { success: true, retention: result.retention };
        } catch (error) {
            return { success: false, message: error.message };
        }
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
