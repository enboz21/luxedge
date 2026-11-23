const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { EventEmitter } = require('node:events');
const { pathToFileURL } = require('node:url');
const { createPoller } = require('../web_ui/polling');
const LogFormat = require('../web_ui/log_format');

function fixture(owns = true, options = {}) {
    const app = new EventEmitter();
    const calls = [];
    const windows = [], trays = [], loginWrites = [], loginReads = [], handlers = new Map();
    class Window extends EventEmitter {
        constructor(settings) {
            super();
            this.settings = settings;
            this.visible = false;
            this.webContents = { send() {}, mainFrame: { url: '' } };
            windows.push(this);
        }
        loadFile(filename) { this.webContents.mainFrame.url = pathToFileURL(filename).href; }
        isDestroyed() { return false; }
        isVisible() { return this.visible; }
        isMinimized() { return false; }
        show() { this.visible = true; }
        hide() { this.visible = false; }
        focus() { this.focused = true; }
        static getAllWindows() { return windows; }
    }
    class Tray extends EventEmitter {
        constructor() { super(); if (options.trayFails) throw new Error('No tray'); trays.push(this); }
        setToolTip() {}
        setContextMenu(menu) { this.menu = menu; }
    }
    let readyCalls = 0;
    Object.assign(app, {
        requestSingleInstanceLock: () => owns,
        whenReady: () => { readyCalls++; return new Promise(() => {}); },
        quit: () => calls.push('quit'),
        exit: () => calls.push('exit'),
        relaunch: () => calls.push('relaunch'),
        isPackaged: options.packaged ?? true,
        getPath: name => name === 'appData' ? options.appData : 'C:\\LuxEdge\\LuxEdge.exe',
        getLoginItemSettings: settings => {
            loginReads.push(settings);
            return { openAtLogin: settings.args.length ? !!options.current : !!options.legacy,
                executableWillLaunchAtLogin: options.approved !== false };
        },
        setLoginItemSettings: settings => loginWrites.push(settings),
    });
    const timers = new Map();
    const spawned = [];
    const processMock = new EventEmitter();
    processMock.env = { APPDATA: options.appData || 'MockAppData' };
    processMock.argv = options.argv || [];
    processMock.resourcesPath = path.resolve(__dirname, '..');
    const context = vm.createContext({
        require(name) {
            if (name === 'electron') return { app, BrowserWindow: Window, Tray, dialog: options.dialog,
                Menu: { buildFromTemplate: template => template },
                nativeImage: { createFromBitmap: () => ({}) },
                ipcMain: { handle: (name, handler) => handlers.set(name, handler) } };
            if (name === 'child_process') return { spawn: (...args) => {
                const proc = new EventEmitter();
                proc.stdout = new EventEmitter();
                proc.stderr = new EventEmitter();
                proc.pid = 4321;
                proc.exitCode = null;
                proc.kill = () => calls.push('kill-helper');
                spawned.push({ args, proc });
                return proc;
            } };
            if (name === 'http') throw new Error('Real network forbidden');
            if (name === 'fs' && !options.appData) {
                return { ...fs, mkdirSync: () => {}, appendFileSync: () => {} };
            }
            return require(name);
        },
        console: { log() {}, error() {}, warn() {} }, process: processMock,
        Buffer, __dirname: path.resolve(__dirname, '..'),
        setTimeout(fn, delay) { const id = timers.size + 1; timers.set(id, { fn, delay }); return id; },
        clearTimeout(id) { timers.delete(id); },
    });
    // Main imports HTTP, but no request can escape this inert replacement.
    const baseRequire = context.require;
    context.require = name => name === 'http'
        ? options.http || new Proxy({}, { get() { throw new Error('Network forbidden'); } })
        : baseRequire(name);
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../main.js'), 'utf8'), context);
    return { app, context, calls, spawned, timers, readyCalls, windows, trays, loginWrites, loginReads, handlers };
}

function diagnosticsBridgeFixture(replies, showMessage) {
    const requests = [];
    const dialogs = [];
    const http = {
        request(settings, respond) {
            const outgoing = new EventEmitter();
            let body;
            outgoing.write = chunk => { body = JSON.parse(chunk); };
            outgoing.end = () => {
                requests.push({ ...settings, body });
                if (!replies.length) {
                    outgoing.emit('error', new Error('Unexpected mocked request'));
                    return;
                }
                const response = new EventEmitter();
                respond(response);
                response.emit('data', JSON.stringify(replies.shift()));
                response.emit('end');
            };
            return outgoing;
        }
    };
    const dialog = {
        showMessageBox(window, settings) {
            dialogs.push({ window, settings });
            return showMessage?.() || Promise.resolve({ response: 1 });
        }
    };
    const f = fixture(true, { http, dialog });
    vm.runInContext('createWindow(); mainWindow.show(); registerIpcHandlers(); pythonProcess = { pid: 1234, exitCode: null };', f.context);
    const window = f.windows[0];
    const event = { sender: window.webContents, senderFrame: window.webContents.mainFrame };
    return { ...f, event, requests, dialogs };
}

test('Windows diagnostics rejects other senders, subframes and nonlocal main-frame URLs', async () => {
    const f = diagnosticsBridgeFixture([]);
    const wrongEvents = [
        { ...f.event, sender: {} },
        { ...f.event, senderFrame: { url: f.event.senderFrame.url } },
        { ...f.event, senderFrame: null },
    ];
    for (const event of wrongEvents) {
        for (const action of ['status', 'start', 'stop']) {
            assert.equal((await f.handlers.get('windows-diagnostics-' + action)(event)).reason, 'untrusted_frame');
        }
    }
    f.event.senderFrame.url += '?unexpected';
    for (const action of ['status', 'start', 'stop']) {
        assert.equal((await f.handlers.get('windows-diagnostics-' + action)(f.event)).reason, 'untrusted_frame');
    }
    assert.equal(f.requests.length, 0);
    assert.equal(f.dialogs.length, 0);
});

test('Windows diagnostics confirms exact scope before sending authenticated empty start and stop requests', async () => {
    const status = { state: 'idle', system_audit_failure: { 5152: 'disabled', 5157: 'access_denied' } };
    const f = diagnosticsBridgeFixture([status, { state: 'starting' }, { state: 'stopping' }]);
    const started = await f.handlers.get('windows-diagnostics-start')(f.event, { bogus: 'ignored' });
    assert.equal(started.state, 'starting');
    const [{ window, settings }] = f.dialogs;
    assert.equal(window, f.windows[0]);
    assert.equal(settings.defaultId, 0);
    assert.equal(settings.cancelId, 0);
    assert.match(settings.detail, /5152.*0cce9225-69ae-11d9-bed3-505054503030.*kapalı → oturum boyunca açık/);
    assert.match(settings.detail, /5157.*0cce9226-69ae-11d9-bed3-505054503030.*mevcut değer bilinmiyor/);
    assert.match(settings.detail, /her iki önceki değeri okuyabildiğinde/);
    assert.equal((await f.handlers.get('windows-diagnostics-stop')(f.event)).state, 'stopping');

    const token = vm.runInContext('WINDOWS_DIAGNOSTICS_TOKEN', f.context);
    assert.match(token, /^[0-9a-f]{64}$/);
    assert.deepEqual(f.requests.map(request => [request.hostname, request.method, request.path, request.body]), [
        ['127.0.0.1', 'GET', '/api/windows-diagnostics', undefined],
        ['127.0.0.1', 'POST', '/api/windows-diagnostics/start', {}],
        ['127.0.0.1', 'POST', '/api/windows-diagnostics/stop', {}],
    ]);
    assert.ok(f.requests.every(request => request.headers['X-LuxEdge-Windows-Diagnostics-Token'] === token));
});

test('Windows diagnostics performs no start after native cancellation or navigation during consent', async () => {
    const cancelled = diagnosticsBridgeFixture([{ state: 'idle' }], async () => ({ response: 0 }));
    assert.equal((await cancelled.handlers.get('windows-diagnostics-start')(cancelled.event)).reason, 'confirmation_cancelled');
    assert.deepEqual(cancelled.requests.map(request => request.method), ['GET']);

    const navigated = diagnosticsBridgeFixture([{ state: 'idle' }], async () => {
        navigated.event.senderFrame.url = 'file:///unrelated.html';
        return { response: 1 };
    });
    assert.equal((await navigated.handlers.get('windows-diagnostics-start')(navigated.event)).reason, 'confirmation_cancelled');
    assert.deepEqual(navigated.requests.map(request => request.method), ['GET']);
});

test('Windows diagnostics checks restoration before shutting down the backend', async () => {
    const f = diagnosticsBridgeFixture([{ state: 'idle' }, { state: 'starting' },
        { state: 'stopping' }, { state: 'stopping' }, { state: 'restored' }]);
    await f.handlers.get('windows-diagnostics-start')(f.event);
    const closing = vm.runInContext('requestQuit()', f.context);
    for (let poll = 0; poll < 2; poll++) {
        await new Promise(setImmediate);
        assert.equal(f.spawned.length, 0);
        const [timerId, timer] = [...f.timers][0];
        assert.equal(timer.delay, 1000);
        f.timers.delete(timerId);
        timer.fn();
    }
    await new Promise(setImmediate);
    assert.deepEqual(f.requests.slice(-3).map(request => request.path), [
        '/api/windows-diagnostics/stop', '/api/windows-diagnostics', '/api/windows-diagnostics'
    ]);
    assert.equal(f.spawned.length, 1);
    f.spawned[0].proc.emit('close', 0);
    await closing;
    assert.deepEqual(f.calls, ['exit']);
});

test('Windows diagnostics shutdown stops polling at the restore deadline and records the outcome', async () => {
    const f = diagnosticsBridgeFixture([{ state: 'idle' }, { state: 'starting' },
        ...Array.from({ length: 4 }, () => ({ state: 'stopping' }))]);
    await f.handlers.get('windows-diagnostics-start')(f.event);
    const closing = vm.runInContext('requestQuit()', f.context);
    for (let poll = 0; poll < 3; poll++) {
        await new Promise(setImmediate);
        const [timerId, timer] = [...f.timers][0];
        assert.equal(timer.delay, 1000);
        f.timers.delete(timerId);
        timer.fn();
    }
    await new Promise(setImmediate);
    assert.equal(f.requests.filter(request => request.method === 'GET').length, 4);
    assert.equal(vm.runInContext("readElectronLogs().at(-1).code", f.context), 'WINDOWS_DIAGNOSTICS_SHUTDOWN');
    assert.equal(f.spawned.length, 1);
    f.spawned[0].proc.emit('close');
    await closing;
});

function diagnosticsRendererFixture(api) {
    const source = fs.readFileSync(path.join(__dirname, '../web_ui/app.js'), 'utf8');
    const html = fs.readFileSync(path.join(__dirname, '../web_ui/index.html'), 'utf8');
    const elements = new Map();
    const context = vm.createContext({
        api, currentPage: 'logs',
        document: { getElementById(id) {
            assert.ok(html.includes('id="' + id + '"'), 'Missing element ' + id);
            if (!elements.has(id)) elements.set(id, {});
            return elements.get(id);
        } }
    });
    vm.runInContext(source.slice(source.indexOf('// --- Windows ağ tanılaması ---'), source.indexOf('// --- Günlük ---')), context);
    return { context, elements, run: code => vm.runInContext(code, context) };
}

test('Windows diagnostics shows channel permissions independently of audit preview and UAC denial', async () => {
    const status = { state: 'idle', security_channel: 'access_denied', defender_channel: 'readable',
        system_audit_failure: { 5152: 'enabled', 5157: 'access_denied' } };
    const f = diagnosticsRendererFixture({
        getWindowsDiagnostics: async () => status,
        startWindowsDiagnostics: async () => ({ ...status, state: 'cancelled', reason: 'uac_cancelled' })
    });
    await f.run('fetchWindowsDiagnosticsData()');
    assert.equal(f.elements.get('windows-diagnostics-sources').textContent,
        'Security günlüğü · okuma izni yok | Defender Ağ Koruması · okunabilir');
    assert.equal(f.elements.get('windows-audit-5152').textContent, 'Açık → açık kalacak');
    assert.match(f.elements.get('windows-audit-5157').textContent, /okuma izni yok.*yönetici önceki değeri kapalı olarak doğrularsa/);
    assert.equal(f.elements.get('windows-diagnostics-start').disabled, false);
    assert.equal(f.elements.get('windows-diagnostics-stop').disabled, true);
    await f.run("changeWindowsDiagnostics('start')");
    assert.match(f.elements.get('windows-diagnostics-state').textContent, /yönetici onayı reddedildi veya iptal edildi/);
});

test('Windows diagnostics renders active scope, stop and a restore conflict from subsequent polling', async () => {
    const active = { state: 'active', remaining_seconds: 84, elevated_security_read: true,
        security_channel: 'access_denied', defender_channel: 'disabled',
        system_audit_failure: { 5152: 'enabled', 5157: 'enabled' },
        previous_audit_failure: { 5152: 'disabled', 5157: 'enabled' }, changed_audit_failure: ['5152'] };
    const conflict = { ...active, state: 'restore_conflict', reason: 'policy_changed', elevated_security_read: false };
    const statuses = [active, conflict];
    const f = diagnosticsRendererFixture({
        getWindowsDiagnostics: async () => statuses.shift(),
        stopWindowsDiagnostics: async () => ({ ...active, state: 'stopping' })
    });
    await f.run('fetchWindowsDiagnosticsData()');
    assert.match(f.elements.get('windows-diagnostics-sources').textContent, /yönetici okuması etkin/);
    assert.match(f.elements.get('windows-diagnostics-state').textContent, /Kalan süre 84 sn/);
    assert.match(f.elements.get('windows-audit-5152').textContent, /Önceki: Kapalı.*Güncel: Açık.*geçici olarak açıldı/);
    assert.doesNotMatch(f.elements.get('windows-audit-5157').textContent, /geçici olarak açıldı/);
    assert.equal(f.elements.get('windows-diagnostics-stop').disabled, false);
    await f.run("changeWindowsDiagnostics('stop')");
    assert.match(f.elements.get('windows-diagnostics-state').textContent, /geri yükleniyor/);
    assert.equal(f.elements.get('windows-diagnostics-stop').disabled, true);
    await f.run('fetchWindowsDiagnosticsData()');
    assert.match(f.elements.get('windows-diagnostics-state').textContent, /Güncel değer korundu/);
    assert.doesNotMatch(f.elements.get('windows-diagnostics-state').textContent, /geri yüklendi/);
    assert.doesNotMatch(f.elements.get('windows-diagnostics-sources').textContent, /yönetici okuması etkin/);
});

test('Günlük recovers Electron records when the PC backend is offline', async t => {
    const appData = fs.mkdtempSync(path.join(require('node:os').tmpdir(), 'luxedge-logs-'));
    t.after(() => fs.rmSync(appData, { recursive: true, force: true }));
    const f = fixture(true, { appData });
    vm.runInContext('delete process.env.APPDATA', f.context);
    vm.runInContext("logElectron('ERROR', 'TEST_ERROR', 'Test kaydı', 'Deneme');", f.context);
    const logPath = path.join(appData, 'LuxEdge', 'luxedge-electron.jsonl');
    const original = fs.readFileSync(logPath, 'utf8');
    fs.writeFileSync(logPath + '.1', 'x'.repeat(2 * 1024 * 1024) + '\n' + original);
    fs.writeFileSync(logPath, '{"partial');

    const restarted = fixture(true, { appData });
    vm.runInContext('registerIpcHandlers()', restarted.context);
    const result = await restarted.handlers.get('get-logs')();
    assert.equal(result.available, false);
    assert.equal(result.electronPersistent, true);
    assert.equal(result.logs.length, 1);
    assert.equal(result.logs[0].code, 'TEST_ERROR');
    assert.match(result.logs[0].id, /^electron:/);
    vm.runInContext("logElectron('INFO', 'AFTER_PARTIAL', 'Yeni kayıt', 'Deneme');", restarted.context);
    const reopened = fixture(true, { appData });
    vm.runInContext('registerIpcHandlers()', reopened.context);
    const next = await reopened.handlers.get('get-logs')();
    assert.deepEqual(Array.from(next.logs, entry => entry.code), ['TEST_ERROR', 'AFTER_PARTIAL']);
});

test('Günlük keeps cleared records hidden when the backend goes offline', async () => {
    const appSource = fs.readFileSync(path.join(__dirname, '../web_ui/app.js'), 'utf8');
    const start = appSource.indexOf('// --- Günlük ---');
    const end = appSource.indexOf('// --- Başlangıç ---', start);
    const context = vm.createContext({
        api: {},
        LogFormat,
        currentPage: 'logs',
        document: { getElementById: () => ({ children: [] }) }
    });
    vm.runInContext(appSource.slice(start, end), context);
    vm.runInContext(`
        showLogStatus = () => {};
        renderLogs = () => { visibleLogs = logCache.filter(isAfterLogClear); };
        const cached = { id: 7, timestamp: '2026-01-01T00:00:00.000Z' };
        const electron = { id: 'electron:old', timestamp: '2026-01-01T00:00:00.000Z' };
        logCache = [cached, electron];
        clearLogs();
        api.getLogs = async () => ({ available: false, logs: [
            electron,
            { id: 'electron:new', timestamp: new Date(clearedLogTime + 1).toISOString() }
        ] });
    `, context);
    await vm.runInContext('fetchLogsData()', context);
    assert.equal(vm.runInContext('logCache.length', context), 3);
    assert.equal(vm.runInContext('visibleLogs.length', context), 1);
    assert.equal(vm.runInContext('visibleLogs[0].id', context), 'electron:new');
    vm.runInContext(`api.getLogs = async () => ({ available: true, logs: [
        cached, electron, { id: 8, timestamp: new Date(clearedLogTime + 1).toISOString() }
    ] });`, context);
    await vm.runInContext('fetchLogsData()', context);
    assert.equal(vm.runInContext('visibleLogs.length', context), 1);
    assert.equal(vm.runInContext('visibleLogs[0].id', context), 8);
});

test('Günlük labels estimated and receipt times in the display and exported text', async () => {
    const appSource = fs.readFileSync(path.join(__dirname, '../web_ui/app.js'), 'utf8');
    const start = appSource.indexOf('// --- Günlük ---');
    const end = appSource.indexOf('// --- Başlangıç ---', start);
    let exported;
    const link = { click() {} };
    const context = vm.createContext({
        Blob,
        LogFormat,
        URL: { createObjectURL(blob) { exported = blob; return 'blob:test'; }, revokeObjectURL() {} },
        document: { createElement: () => link },
        setTimeout: callback => callback(),
        event: {
            id: 3, source: 'wemos', level: 'INFO', code: 'stream_idle_exited', msg: 'Akış geri geldi',
            timestamp: '2026-01-01T00:00:05.654Z',
            details: {
                event_timestamp_estimated: '2026-01-01T00:00:02.321Z',
                received_at: '2026-01-01T00:00:05.654Z', stream_gap_ms: 750
            }
        }
    });
    vm.runInContext(appSource.slice(start, end), context);
    const format = new Intl.DateTimeFormat('tr-TR', {
        year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit', second: '2-digit', fractionalSecondDigits: 3
    });
    const lines = [
        'Tahmini olay zamanı · ' + format.format(Date.parse(context.event.details.event_timestamp_estimated)),
        'PC alım zamanı · ' + format.format(Date.parse(context.event.details.received_at)),
        'LED verisi aralığı · 750 ms'
    ];
    assert.deepEqual(Array.from(vm.runInContext('logTimingLines(event)', context)), lines);
    vm.runInContext('visibleLogs = [event]; downloadLogs()', context);
    assert.match(link.download, /^luxedge_log_.*\.txt$/);
    assert.equal(link.href, 'blob:test');
    const contents = await exported.text();
    for (const line of lines) assert.ok(contents.includes(line), line);
});

test('Günlük identifies Windows source and exports the event and audit evidence', async () => {
    const html = fs.readFileSync(path.join(__dirname, '../web_ui/index.html'), 'utf8');
    assert.match(html, /<option value="windows">Windows<\/option>/);
    const source = fs.readFileSync(path.join(__dirname, '../web_ui/app.js'), 'utf8');
    const start = source.indexOf('// --- Günlük ---');
    let exported;
    const context = vm.createContext({
        Blob,
        LogFormat,
        URL: { createObjectURL(blob) { exported = blob; return 'blob:test'; }, revokeObjectURL() {} },
        document: { createElement: () => ({ click() {} }) },
        setTimeout: callback => callback(),
        evidence: {
            id: 5157, source: 'windows', level: 'WARNING', code: 'windows_wfp_block',
            msg: 'Windows Filtreleme Platformu bağlantıyı engelledi',
            timestamp: '2026-01-01T00:00:05.654Z',
            details: {
                event_at: '2026-01-01T00:00:02.321Z', event_channel: 'Security',
                event_id: 5157, event_record_id: 158, filter_origin: 'Kaynak belirlenemedi',
                system_audit_failure: { 5152: 'disabled', 5157: 'enabled' }
            }
        }
    });
    vm.runInContext(source.slice(start, source.indexOf('// --- Başlangıç ---', start)), context);
    vm.runInContext('visibleLogs = [evidence]; downloadLogs()', context);
    const text = await exported.text();
    assert.match(text, /\| Windows \| Windows Filtreleme Platformu/);
    assert.match(text, /Windows olay zamanı · .*02,321/);
    assert.match(text, /Windows günlüğü · Güvenlik · Olay 5157 · Kayıt 158/);
    assert.match(text, /WFP filtre kökeni · Kaynak belirlenemedi/);
    assert.match(text, /Sistem hata denetimi · 5152 kapalı · 5157 açık/);
});

function logRendererFixture(api) {
    const html = fs.readFileSync(path.join(__dirname, '../web_ui/index.html'), 'utf8');
    const source = fs.readFileSync(path.join(__dirname, '../web_ui/app.js'), 'utf8');
    const ids = new Map();
    const downloads = [];
    const timers = new Map();
    let timerId = 0;
    class Element {
        constructor(tag = 'div') {
            this.tagName = tag;
            this.children = [];
            this.childNodes = this.children;
            this.dataset = {};
            this.value = '';
            this.scrollTop = 0;
            this.clientHeight = 24;
            this.classList = { toggle() {} };
        }
        get options() { return this.children; }
        get firstElementChild() { return this.children[0] || null; }
        get nextElementSibling() { return this.parent?.children[this.parent.children.indexOf(this) + 1] || null; }
        get isConnected() { return Boolean(this.parent); }
        get scrollHeight() { return this.children.length * 24; }
        append(...nodes) { for (const node of nodes) this.appendChild(node); }
        appendChild(node) { this.insertBefore(node, null); }
        insertBefore(node, next) {
            node.remove();
            node.parent = this;
            this.children.splice(next ? this.children.indexOf(next) : this.children.length, 0, node);
        }
        remove() {
            if (this.parent) this.parent.children.splice(this.parent.children.indexOf(this), 1);
            this.parent = null;
        }
        replaceChildren(...nodes) {
            for (const node of [...this.children]) node.remove();
            this.append(...nodes);
            this.value = nodes[0]?.value || '';
        }
        querySelector(selector) {
            if (selector === '.log-entry[open]') return this.children.find(child => child.className === 'log-entry' && child.open);
            if (selector === '.log-detail details') {
                return this.children.find(child => child.className === 'log-detail')?.children.find(child => child.tagName === 'details');
            }
            throw new Error('Unexpected selector ' + selector);
        }
        addEventListener() {}
        scrollIntoView() {}
        focus() {}
        click() {}
    }
    const element = id => {
        assert.ok(html.includes('id="' + id + '"'), 'Missing element ' + id);
        if (!ids.has(id)) ids.set(id, new Element());
        return ids.get(id);
    };
    const context = vm.createContext({
        api, LogFormat, Blob, currentPage: 'logs', deviceRecords: [],
        URL: { createObjectURL(blob) { downloads.push(blob); return 'blob:test'; }, revokeObjectURL() {} },
        document: { getElementById: element, createElement: tag => new Element(tag), activeElement: null },
        Option: function (text, value) {
            return Object.assign(new Element('option'), { text, value });
        },
        setTimeout(callback) { timers.set(++timerId, callback); return timerId; },
        clearTimeout(id) { timers.delete(id); },
        fetchLogs() {},
    });
    vm.runInContext(source.slice(source.indexOf('// --- Günlük dosyaları ---'), source.indexOf('// --- Başlangıç ---')), context);
    element('log-autoscroll').checked = true;
    return {
        context, element, downloads,
        run: code => vm.runInContext(code, context),
        visible: () => Array.from(vm.runInContext('visibleLogs.map(row => row.id)', context)),
        flushTimers() {
            for (const [id, callback] of [...timers]) {
                timers.delete(id);
                callback();
            }
        },
    };
}

const LOG_TEST_TIME = Date.parse('2026-11-22T19:02:01.500Z');

function logRow(id, source, delta, code, details = {}, override = {}) {
    return {
        id, source, code, msg: 'Olay ' + id, level: 'INFO',
        timestamp: new Date(LOG_TEST_TIME + delta).toISOString(), details, ...override,
    };
}

function localDateInput(milliseconds) {
    const date = new Date(milliseconds);
    return new Date(milliseconds - date.getTimezoneOffset() * 60_000).toISOString().slice(0, 19);
}

test('Günlük combines all four sources with duplicate clocks and exact intersecting filters', async () => {
    const pc = logRow(101, 'pc', 0, 'frame_failed', { stage: 'capture', ip: '192.0.2.11' },
        { device_id: 'desk', device_name: 'Masa', level: 'ERROR' });
    const windows = logRow(102, 'windows', 0, 'windows_wfp_block', {
        ip: '192.0.2.11', operation: 'UDP LED', event_id: 5157, event_at: pc.timestamp,
    }, { level: 'WARNING' });
    const wemos = logRow(103, 'wemos', 60_000, 'stream_idle_entered', {
        event_timestamp_estimated: new Date(LOG_TEST_TIME + 400).toISOString(), stream_gap_ms: 750,
    }, { device_id: 'desk', device_name: 'Masa' });
    const electron = logRow('electron:test', 'electron', 1200, 'BACKEND_READY');
    const f = logRendererFixture({ getLogs: async () => ({ available: true, logs: [wemos, windows, electron, pc] }) });
    await f.run('fetchLogsData()');
    assert.deepEqual(f.visible(), [102, 101, 103, 'electron:test']);
    for (const [key, value] of Object.entries({
        source: 'windows', device: 'desk', stage: 'udp_send', level: 'WARNING', search: '5157',
        from: localDateInput(LOG_TEST_TIME), to: localDateInput(LOG_TEST_TIME),
    })) f.element('log-' + key).value = value;
    f.run('changeLogFilters()');
    assert.deepEqual(f.visible(), [102]);
    f.element('log-to').value = localDateInput(LOG_TEST_TIME - 1000);
    f.run('changeLogFilters()');
    assert.deepEqual(f.visible(), []);
    f.element('log-to').value = localDateInput(LOG_TEST_TIME);
    f.element('log-search').value = '5152';
    f.run('changeLogFilters()');
    assert.deepEqual(f.visible(), []);
});

test('Günlük refresh preserves the open row, nested technical detail and scroll away from the tail', async () => {
    const rows = [logRow(1, 'pc', -1000, 'frame_failed', { stage: 'capture' }),
        logRow(2, 'wemos', 0, 'stream_idle_entered'), logRow(3, 'windows', 1000, 'windows_wfp_block')];
    const f = logRendererFixture({ getLogs: async () => ({ available: true, logs: rows.slice() }) });
    await f.run('fetchLogsData()');
    const row = f.run('logRows.get(1).row');
    const technical = row.querySelector('.log-detail details');
    row.open = true;
    technical.open = true;
    f.element('log-entries').scrollTop = 11;
    rows.push(logRow('electron:later', 'electron', 2000, 'BACKEND_READY'));
    await f.run('fetchLogsData()');
    assert.equal(f.run('logRows.get(1).row'), row);
    assert.equal(row.open, true);
    assert.equal(row.querySelector('.log-detail details'), technical);
    assert.equal(technical.open, true);
    assert.equal(f.element('log-entries').scrollTop, 11);
});

test('TXT canonical format preserves local offset, milliseconds, stage and error evidence', () => {
    const row = logRow(31, 'pc', 0, 'frame_failed', { stage: 'capture',
        error_type: 'ScreenShotError', winerror: 5, error_message: 'BitBlt başarısız' },
    { device_name: 'Masa', level: 'ERROR' });
    const minutes = -new Date(LOG_TEST_TIME).getTimezoneOffset();
    const offset = (minutes < 0 ? '-' : '+') + String(Math.floor(Math.abs(minutes) / 60)).padStart(2, '0')
        + ':' + String(Math.abs(minutes) % 60).padStart(2, '0');
    const contents = LogFormat.formatEntry(row);
    assert.ok(contents.split('\n')[0].includes(',500 GMT' + offset), contents);
    assert.match(contents, /\| Hata \| PC \| Masa \| Olay 31/);
    assert.match(contents, /Aşama · Ekran yakalama/);
    assert.match(contents, /Hata türü · ScreenShotError/);
    assert.match(contents, /Hata kodu · winerror 5/);
    assert.match(contents, /Hata iletisi · BitBlt başarısız/);
});

test('Günlük searches earlier archive pages while keeping live polling and filters separate', async () => {
    const live = [logRow(9001, 'wemos', 100, 'stream_idle_entered')];
    const wrongStage = logRow(8999, 'wemos', 0, 'wifi_disconnected');
    const older = logRow(410, 'wemos', -999_000, 'stream_idle_entered');
    const oldest = logRow(70, 'wemos', -1_399_000, 'stream_idle_exited');
    const archivedEvidence = logRow(411, 'windows', -998_000, 'windows_wfp_block');
    const requests = [];
    let liveReads = 0;
    const retention = { limit_bytes: 512 * 1024 * 1024, used_bytes: 3 * 1024 * 1024,
        oldest_timestamp: oldest.timestamp, legacy_used_bytes: 1024 * 1024 };
    const f = logRendererFixture({
        getLogs: async () => { liveReads++; return { available: true, logs: live }; },
        getLogArchive: async query => {
            requests.push(query);
            if (Number.isFinite(query.from)) return { retention, logs: [older, archivedEvidence], has_more: false };
            if (!query.before) return { retention, logs: [wrongStage], has_more: true, next_cursor: 'older1' };
            if (query.before === 'older1') return { retention, logs: [older], has_more: true, next_cursor: 'older2' };
            return { retention, logs: [oldest], has_more: false };
        },
    });
    await f.run('fetchLogsData()');
    f.element('log-source').value = 'wemos';
    f.element('log-stage').value = 'wemos_stream';
    f.run('toggleLogArchive()');
    await new Promise(setImmediate);
    assert.deepEqual(requests.map(request => request.before), [undefined, 'older1']);
    assert.ok(requests.every(request => request.source === 'wemos' && request.limit === 100));
    assert.deepEqual(f.visible(), [410]);
    assert.match(f.element('log-retention-usage').textContent, /3 MiB \/ 512 MiB · En eski kayıt .*Eski TXT günlükleri 1 MiB/);
    assert.equal(f.element('log-clear').disabled, true);
    f.run('selectLogIncident(logHistory.logs[0])');
    await new Promise(setImmediate);
    assert.deepEqual(f.visible(), [410, 411]);
    assert.equal(requests.at(-1).from, Date.parse(older.timestamp) - 30_000);
    assert.equal(requests.at(-1).source, undefined);
    f.run('closeLogContext()');
    assert.deepEqual(f.visible(), [410]);
    await f.run('fetchLogsData()');
    assert.equal(liveReads, 1);
    f.run('showLogArchivePage(1)');
    await new Promise(setImmediate);
    assert.deepEqual(f.visible(), [70]);
    assert.equal(f.element('log-archive-older').disabled, true);
    f.run('showLogArchivePage(-1)');
    await new Promise(setImmediate);
    assert.deepEqual(f.visible(), [410]);
    f.run('toggleLogArchive()');
    assert.equal(f.element('log-stage').value, 'wemos_stream');
    assert.deepEqual(f.visible(), [9001]);
    assert.equal(f.element('log-clear').disabled, false);
});

test('Günlük incident context merges archive pages and live rows; visible TXT and incident export use different scopes', async () => {
    const selected = logRow(51, 'wemos', 120_000, 'stream_idle_entered', {
        event_timestamp_estimated: new Date(LOG_TEST_TIME).toISOString(),
        received_at: new Date(LOG_TEST_TIME + 120_000).toISOString(),
    });
    const pc = logRow(50, 'pc', -20, 'frame_failed');
    const leftEdge = logRow('electron:left', 'electron', -30_000, 'BACKEND_READY');
    const rightEdge = logRow(52, 'windows', 75_000, 'windows_wfp_block', {
        event_at: new Date(LOG_TEST_TIME + 30_000).toISOString(),
    });
    const excluded = logRow(53, 'pc', 30_001, 'frame_failed');
    const archiveQueries = [];
    const incidentRequests = [];
    const f = logRendererFixture({
        getLogs: async () => ({ available: true, logs: [pc, selected] }),
        getLogArchive: async query => {
            archiveQueries.push(query);
            return !query.before ? { logs: [selected, pc, leftEdge], has_more: true, next_cursor: 'context1' }
                : { logs: [rightEdge, excluded], has_more: false };
        },
        saveIncidentLog: async request => {
            incidentRequests.push(request);
            return { state: 'saved', path: 'C:/Logs/incident.txt', written: 4 };
        },
    });
    await f.run('fetchLogsData()');
    f.element('log-source').value = 'wemos';
    f.run('changeLogFilters()');
    assert.deepEqual(f.visible(), [51]);
    f.run('selectLogIncident(logCache.find(row => row.id === 51))');
    await new Promise(setImmediate);
    assert.deepEqual(f.visible(), ['electron:left', 50, 51]);
    assert.equal(f.element('log-context-more').hidden, false);
    f.run('downloadLogs()');
    const visibleText = await f.downloads[0].text();
    for (const id of ['electron:left', 50, 51]) assert.ok(visibleText.includes('Olay ' + id));
    for (const id of [52, 53]) assert.ok(!visibleText.includes('Olay ' + id));
    await f.run('exportSelectedLogIncident()');
    const scope = { from: LOG_TEST_TIME - 30_000, to: LOG_TEST_TIME + 30_000 };
    assert.deepEqual({ ...incidentRequests[0] }, { ...scope, selectedKey: '51' });
    assert.match(f.element('log-incident-export-status').textContent, /4 kayıt/);
    assert.equal(f.element('log-incident-export-path').textContent, 'C:/Logs/incident.txt');
    await f.run('loadLogContextPage()');
    assert.deepEqual(archiveQueries.map(({ from, to, before }) => ({ from, to, before })),
        [{ ...scope, before: undefined }, { ...scope, before: 'context1' }]);
    assert.deepEqual(f.visible(), ['electron:left', 50, 51, 52]);
    assert.equal(f.element('log-context-more').hidden, true);
    f.run('closeLogContext()');
    assert.deepEqual(f.visible(), [51]);
    assert.equal(f.element('log-source').value, 'wemos');
});

test('Canlı TXT shows cancellation, destination and degraded-write messages, then stops', async () => {
    const path = 'C:/Logs/luxedge-live.txt';
    const starts = [{ state: 'cancelled' }, { state: 'writing', path, written: 0 }];
    const f = logRendererFixture({
        getLiveLogFileState: async () => ({ state: 'writing', path, written: 3, message: 'PC günlüğü geçici olarak erişilemiyor.' }),
        startLiveLogFile: async () => starts.shift(),
        stopLiveLogFile: async () => ({ state: 'stopped', path, written: 3, message: 'Bir aralık diskte yok.' }),
    });
    await f.run("changeLiveLogFile('start')");
    assert.match(f.element('log-live-file-status').textContent, /Dosya seçimi iptal edildi/);
    assert.equal(f.element('log-live-file-path').hidden, true);
    assert.equal(f.element('log-live-file-start').disabled, false);
    await f.run("changeLiveLogFile('start')");
    assert.equal(f.element('log-live-file-path').textContent, path);
    assert.equal(f.element('log-live-file-stop').disabled, false);
    await f.run('fetchLiveLogFileData()');
    assert.match(f.element('log-live-file-status').textContent, /3 kayıt.*geçici olarak erişilemiyor/);
    await f.run("changeLiveLogFile('stop')");
    assert.match(f.element('log-live-file-status').textContent, /durduruldu.*3 kayıt.*Bir aralık diskte yok/);
    assert.equal(f.element('log-live-file-path').textContent, path);
    assert.equal(f.element('log-live-file-stop').disabled, true);
});

test('second application never starts backend or readiness work', () => {
    const f = fixture(false);
    assert.deepEqual(f.calls, ['quit']);
    assert.equal(f.readyCalls, 0);
    assert.equal(f.spawned.length, 0);
});

test('second-instance restores and focuses the owned window', () => {
    const f = fixture();
    vm.runInContext(`mainWindow = {
        isMinimized: () => true,
        restore: () => process.emit('window-action', 'restore'),
        show: () => process.emit('window-action', 'show'),
        focus: () => process.emit('window-action', 'focus')
    }`, f.context);
    const actions = [];
    f.context.process.on('window-action', action => actions.push(action));
    f.app.emit('second-instance');
    assert.deepEqual(actions, ['restore', 'show', 'focus']);
});

test('shutdown targets only owned PID, waits asynchronously and is idempotent', async () => {
    const f = fixture();
    vm.runInContext('pythonProcess = { pid: 1234, exitCode: null }', f.context);
    const done = vm.runInContext('requestQuit(true)', f.context);
    const again = vm.runInContext('requestQuit(true)', f.context);
    assert.equal(done, again);
    assert.equal(f.spawned.length, 1);
    const { args, proc } = f.spawned[0];
    assert.equal(args[0], 'taskkill');
    assert.deepEqual(Array.from(args[1]), ['/pid', '1234', '/f', '/t']);
    assert.equal(args[2].windowsHide, true);
    assert.equal(f.calls.length, 0);
    proc.emit('close', 0);
    await done;
    assert.deepEqual(f.calls, ['relaunch', 'exit']);
    assert.equal(f.timers.size, 0);
});

test('shutdown with no backend does not kill other processes', async () => {
    const f = fixture();
    await vm.runInContext('requestQuit()', f.context);
    assert.equal(f.spawned.length, 0);
    assert.deepEqual(f.calls, ['exit']);
});

test('shutdown timeout is bounded', async () => {
    const f = fixture();
    vm.runInContext('pythonProcess = { pid: 1234, exitCode: null }', f.context);
    const done = vm.runInContext('requestQuit()', f.context);
    const timeout = [...f.timers.values()][0];
    assert.equal(timeout.delay, 5000);
    timeout.fn();
    await done;
    assert.deepEqual(f.calls, ['kill-helper', 'exit']);
});

test('backend startup does not kill processes by image name; launch failure settles', async () => {
    const f = fixture();
    const pending = vm.runInContext('startPython()', f.context);
    assert.equal(f.spawned.length, 1);
    const { args, proc } = f.spawned[0];
    assert.notEqual(args[0], 'taskkill');
    assert.equal(args[2].windowsHide, true);
    assert.equal(args[2].detached, false);
    assert.equal(args[2].env.LUXEDGE_WINDOWS_DIAGNOSTICS_TOKEN, vm.runInContext('WINDOWS_DIAGNOSTICS_TOKEN', f.context));
    proc.emit('error', new Error('simulated missing runtime'));
    proc.emit('close', -1);
    [...f.timers.values()][0].fn();
    assert.equal(await pending, false);
    await vm.runInContext('killPython()', f.context);
    assert.equal(f.spawned.length, 1);
});

test('hidden UI stops timers and requests, restoring refreshes immediately', async () => {
    let visible = true;
    let calls = 0;
    const timers = new Map();
    let id = 0;
    const poller = createPoller(() => visible, fn => { timers.set(++id, fn); return id; }, id => timers.delete(id));
    const run = poller.add('status', async () => { calls++; }, 1500);
    poller.refresh();
    await Promise.resolve();
    assert.equal(calls, 1);
    assert.equal(timers.size, 1);
    visible = false;
    poller.refresh();
    await run();
    assert.equal(calls, 1);
    assert.equal(timers.size, 0);
    visible = true;
    poller.refresh();
    await Promise.resolve();
    assert.equal(calls, 2);
    assert.equal(timers.size, 1);
});

test('slow status requests cannot overlap and recover after rejection', async () => {
    let calls = 0;
    let reject;
    const poller = createPoller(() => true);
    const run = poller.add('status', () => {
        calls++;
        return new Promise((_, no) => { reject = no; });
    }, 1500);
    const pending = run();
    await run();
    assert.equal(calls, 1);
    reject(new Error('simulated timeout'));
    await assert.rejects(pending, /simulated timeout/);
    const retry = run();
    assert.equal(calls, 2);
    reject(new Error('simulated timeout'));
    await assert.rejects(retry);
});

test('renderer and preload agree on visibility bridge and scripts parse', () => {
    const html = fs.readFileSync(path.join(__dirname, '../web_ui/index.html'), 'utf8');
    const app = fs.readFileSync(path.join(__dirname, '../web_ui/app.js'), 'utf8');
    const preload = fs.readFileSync(path.join(__dirname, '../preload.js'), 'utf8');
    assert.match(preload, /exposeInMainWorld\('luxedge'/);
    assert.match(preload, /onWindowVisibility/);
    assert.match(app, /window\.luxedge\.onWindowVisibility/);
    assert.match(html, /<script src="app.js"><\/script>/);
    new vm.Script(app);
    for (const match of html.matchAll(/<script>([\s\S]*?)<\/script>/g)) new vm.Script(match[1]);
});

for (const hidden of [false, true]) {
    test('window ready respects tray startup: ' + hidden, () => {
        const f = fixture(true, { argv: hidden ? ['--start-in-tray'] : [] });
        vm.runInContext('createTray(); createWindow()', f.context);
        assert.equal(f.windows[0].settings.show, false);
        f.windows[0].emit('ready-to-show');
        assert.equal(f.windows[0].visible, !hidden);
        f.trays[0].emit('double-click');
        assert.equal(f.windows[0].visible, true);
        f.windows[0].hide();
        f.trays[0].menu[0].click();
        assert.equal(f.windows[0].visible, true);
    });
}

test('tray failure exposes the hidden-start window', () => {
    const f = fixture(true, { argv: ['--start-in-tray'], trayFails: true });
    vm.runInContext('createTray(); createWindow()', f.context);
    f.windows[0].emit('ready-to-show');
    assert.equal(f.windows[0].visible, true);
});

test('automatic second launch stays hidden; manual launch before ready is remembered', () => {
    const f = fixture(true, { argv: ['--start-in-tray'] });
    f.app.emit('second-instance', {}, ['LuxEdge.exe', '--start-in-tray']);
    assert.equal(vm.runInContext('showOnReady', f.context), false);
    f.app.emit('second-instance', {}, ['LuxEdge.exe']);
    vm.runInContext('createTray(); createWindow()', f.context);
    f.windows[0].emit('ready-to-show');
    assert.equal(f.windows[0].visible, true);
    f.windows[0].hide();
    f.app.emit('second-instance', {}, ['LuxEdge.exe', '--start-in-tray']);
    assert.equal(f.windows[0].visible, false);
});

test('only enabled legacy startup entries migrate', () => {
    for (const opts of [{legacy:true}, {}, {current:true}, {legacy:true,approved:false}, {legacy:true,packaged:false}]) {
        const f = fixture(true, opts);
        vm.runInContext('migrateAutostart()', f.context);
        const expected = opts.legacy && opts.approved !== false && opts.packaged !== false ? 1 : 0;
        assert.equal(f.loginWrites.length, expected);
        if (expected) {
            assert.deepEqual(Array.from(f.loginWrites[0].args), ['--start-in-tray']);
            assert.equal(f.loginWrites[0].openAtLogin, true);
        }
    }
});

test('autostart read and write use the same executable and arguments', () => {
    const f = fixture(true, {current:true});
    vm.runInContext('registerIpcHandlers()', f.context);
    assert.equal(f.handlers.get('get-autostart')().enabled, true);
    for (const enabled of [true,false]) {
        assert.equal(f.handlers.get('set-autostart')({}, enabled).success, true);
        const written = f.loginWrites.at(-1);
        assert.equal(written.openAtLogin, enabled);
        assert.deepEqual(Array.from(written.args), Array.from(f.loginReads[0].args));
        assert.equal(written.path, f.loginReads[0].path);
    }
});

test('backend port avoids a port held by another LuxEdge and is handed to the backend', async () => {
    const net = require('node:net');
    const blocker = net.createServer();
    await new Promise(resolve => blocker.listen({ port: 0, host: '127.0.0.1' }, resolve));
    const busy = blocker.address().port;
    const f = fixture();
    try {
        const chosen = await vm.runInContext(`findFreePort(${busy})`, f.context);
        assert.equal(chosen, busy + 1);
        vm.runInContext(`PYTHON_PORT = ${chosen}; startPython()`, f.context);
        assert.equal(f.spawned[0].args[2].env.LUXEDGE_BACKEND_PORT, String(chosen));
    } finally {
        blocker.close();
    }
});
