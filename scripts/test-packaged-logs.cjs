'use strict';

// Runs the unpacked build against a simulated loopback Wemos and a fresh user profile.
// Set PLAYWRIGHT_MODULE_PATH when using Playwright from outside this workspace.
const assert = require('node:assert/strict');
const dgram = require('node:dgram');
const fs = require('node:fs');
const http = require('node:http');
const net = require('node:net');
const path = require('node:path');
const formats = require('../web_ui/log_format');
const { luxedgeVersion } = require('../package.json');

const usage = 'Usage: node scripts/test-packaged-logs.cjs <workspace path to unpacked LuxEdge.exe>';
if (process.argv.includes('--help')) {
    console.log(usage);
    process.exit(0);
}
if (process.platform !== 'win32' || process.argv.length !== 3) {
    console.error(usage);
    process.exit(1);
}

const root = fs.realpathSync(path.join(__dirname, '..'));
function isInside(parent, location) {
    const relative = path.relative(parent, location);
    return relative !== '' && !path.isAbsolute(relative) && relative !== '..' && !relative.startsWith('..' + path.sep);
}

const executable = fs.realpathSync(process.argv[2]);
const resources = path.join(path.dirname(executable), 'resources');
if (!isInside(root, executable) || path.basename(executable).toLowerCase() !== 'luxedge.exe'
        || !fs.existsSync(path.join(resources, 'app.asar')) || !fs.existsSync(path.join(resources, 'lush_backend.exe'))) {
    throw new Error('Choose an unpacked LuxEdge build in the workspace.');
}
const { _electron } = require(process.env.PLAYWRIGHT_MODULE_PATH || 'playwright');
const buildRoot = path.join(root, 'build');
fs.mkdirSync(buildRoot, { recursive: true });
const scratch = fs.realpathSync(buildRoot);
if (!isInside(root, scratch)) throw new Error('Build scratch directory must be inside the workspace.');
const profile = fs.mkdtempSync(path.join(scratch, 'packaged-logs-'));
console.log('Isolated smoke profile: ' + profile);
const logDirectory = path.join(profile, 'LuxEdge');
const desktop = path.join(profile, 'Desktop');
const localAppData = path.join(profile, 'LocalAppData');
const temporary = path.join(profile, 'Temp');
for (const directory of [logDirectory, desktop, localAppData, temporary]) fs.mkdirSync(directory);
const deviceIp = '127.0.0.2';
const selectedDevice = 'c4-retained-wemos';
const mib = 1024 * 1024;
const anchorTime = Math.floor((Date.now() - 3600_000) / 1000) * 1000;
const journalFixtures = [];
const simulatedEvents = [{ seq: 1, uptime_ms: 20, code: 1, arg: 0 }];
const boot = '1270000200000004';
const readRows = file => fs.readFileSync(file, 'utf8').trim().split('\n').filter(Boolean).map(JSON.parse);
let electron;

function seedArchive() {
    const baseline = anchorTime - 3600_000;
    for (let segment = 0; segment < 5; segment++) {
        const rows = [];
        for (let offset = 0; offset < 400; offset++) {
            const index = segment * 400 + offset;
            const when = baseline + index * 1000;
            rows.push({
                id: index + 1, timestamp: new Date(when).toISOString(), archive_order_us: when * 1000,
                source: 'pc', level: 'INFO', code: 'fixture_filler',
                msg: 'C4 archive-filler #' + String(index).padStart(4, '0'),
                details: { payload: 'x'.repeat(2240) },
            });
        }
        if (segment === 4) {
            const inContext = [
                ['lower-outside', -30_001, 'pc', 'INFO', 'frame_failed'],
                ['lower-inclusive', -30_000, 'pc', 'INFO', 'frame_failed'],
                ['monitor-capture', -4200, 'pc', 'ERROR', 'frame_failed'],
                ['ANCHOR-Wemos-stream', 0, 'wemos', 'WARNING', 'stream_idle_entered'],
                ['Windows-source', 1200, 'windows', 'INFO', 'windows_wfp_query'],
                ['other-device', 1500, 'wemos', 'WARNING', 'stream_idle_entered'],
                ['upper-inclusive', 30_000, 'pc', 'INFO', 'frame_failed'],
                ['upper-outside', 30_001, 'pc', 'INFO', 'frame_failed'],
            ];
            for (const [tag, delta, source, level, code] of inContext) {
                const occurrence = anchorTime + delta;
                const receipt = tag === 'ANCHOR-Wemos-stream' ? occurrence + 45_000 : occurrence;
                const details = source === 'wemos'
                    ? { event_timestamp_estimated: new Date(occurrence).toISOString(),
                        received_at: new Date(receipt).toISOString(), stream_gap_ms: 85 }
                    : source === 'windows' ? { event_at: new Date(occurrence).toISOString(),
                        event_id: 5157, event_record_id: 77, event_channel: 'Security' } : { stage: 'capture' };
                const row = {
                    id: 2001 + journalFixtures.length, timestamp: new Date(receipt).toISOString(), archive_order_us: receipt * 1000,
                    source, level, code, msg: 'C4 ' + tag, details,
                    device_id: tag === 'ANCHOR-Wemos-stream' ? selectedDevice : 'other-device',
                    device_name: tag === 'ANCHOR-Wemos-stream' ? 'C4 test Wemos' : 'C4 other device',
                };
                journalFixtures.push(row);
                rows.push(row);
            }
        }
        rows.sort((a, b) => a.archive_order_us - b.archive_order_us);
        const bounds = [rows[0].archive_order_us, rows.at(-1).archive_order_us];
        const name = 'luxedge.jsonl.seg-' + bounds.join('-') + '-' + (segment + 1).toString(16).padStart(16, '0');
        fs.writeFileSync(path.join(logDirectory, name), rows.map(row => JSON.stringify(row)).join('\n') + '\n');
    }
    fs.writeFileSync(path.join(logDirectory, 'c4-do-not-prune.txt'), 'retention must preserve unmanaged files');
}

function listen(server, port, hostname) {
    return new Promise((resolve, reject) => {
        server.once('error', reject);
        server.listen(port, hostname, () => {
            server.off('error', reject);
            resolve(server.address().port);
        });
    });
}

function reserveUdp() {
    const socket = dgram.createSocket('udp4');
    socket.on('message', (message, remote) => {
        if (message.equals(Buffer.from('PING'))) socket.send('PONG', remote.port, remote.address);
    });
    return new Promise((resolve, reject) => {
        socket.once('error', reject);
        socket.bind(0, deviceIp, () => resolve(socket));
    });
}

function respondToDevice(request, response) {
    const url = new URL(request.url, 'http://' + deviceIp);
    let body;
    if (url.pathname === '/status') {
        body = {
            ip: deviceIp, diagnostics_version: 1, wifi_connected: true, udp_running: true,
            stream_led_count: 74, firmware_version: 'C4 fixture', last_data_age_ms: 30, reset_reason: 'C4 fixture',
        };
    } else if (url.pathname === '/diagnostics') {
        const since = url.searchParams.get('boot') === boot ? Number(url.searchParams.get('after')) : 0;
        const selected = simulatedEvents.filter(event => event.seq > since).slice(0, 8);
        body = { schema: 1, boot_id: boot, reset_reason: 0, uptime_ms: 200,
            oldest_seq: 1, latest_seq: simulatedEvents.length, next_seq: selected.at(-1)?.seq || since, lost: 0, events: selected };
    } else {
        response.writeHead(404).end();
        return;
    }
    response.writeHead(200, { 'Content-Type': 'application/json' }).end(JSON.stringify(body));
}

async function launch(port, options = {}) {
    const env = { ...process.env, APPDATA: profile, LOCALAPPDATA: localAppData, USERPROFILE: profile,
        TEMP: temporary, TMP: temporary, LUXEDGE_BACKEND_PORT: String(port),
        NO_PROXY: '127.0.0.1,127.0.0.2,localhost' };
    delete env.ELECTRON_RUN_AS_NODE;
    electron = await _electron.launch({
        executablePath: executable, args: [
            '--user-data-dir=' + path.join(profile, 'electron'), '--disable-gpu', '--in-process-gpu', '--no-sandbox',
            '--disable-background-networking', '--disable-component-update', '--disable-features=MediaRouter',
        ], env, timeout: 30000,
    });
    electron.process().stdout.on('data', bytes => process.stdout.write(bytes));
    electron.process().stderr.on('data', bytes => process.stderr.write(bytes));
    await electron.context().route('https://**/*', route => route.abort());
    const page = await electron.firstWindow({ timeout: 30000 });
    page.on('pageerror', error => options.rendererErrors?.push(error.message));
    await page.waitForLoadState('domcontentloaded');
    await electron.evaluate(({ app, dialog, ipcMain }, paths) => {
        for (const action of ['status', 'start', 'stop']) {
            const channel = 'windows-diagnostics-' + action;
            ipcMain.removeHandler(channel);
            ipcMain.handle(channel, () => ({
                state: 'unsupported', security_channel: 'unavailable', defender_channel: 'unavailable',
                system_audit_failure: { '5152': 'unavailable', '5157': 'unavailable' },
            }));
        }
        app.setPath('desktop', paths.desktop);
        globalThis.c4Dialogs = [];
        dialog.showSaveDialog = async (_window, options) => {
            globalThis.c4Dialogs.push(options.defaultPath);
            const filePath = options.defaultPath.includes('canli-gunluk') ? paths.live : paths.incident;
            return { canceled: false, filePath };
        };
    }, { desktop, live: path.join(desktop, 'live-test'), incident: path.join(desktop, 'incident-test') });
    await page.locator('button[data-page="logs"]').click();
    await page.waitForFunction(async () => (await window.luxedge.getLogs()).available !== false);
    return page;
}

async function waitText(page, selector, pattern) {
    await page.waitForFunction(({ selector, pattern }) => new RegExp(pattern).test(document.querySelector(selector).textContent),
        { selector, pattern }, { timeout: 15000 });
}

async function messages(page) {
    return page.locator('#log-entries .log-msg').allTextContents();
}

async function pageToOlder(page, steps) {
    for (let number = 2; number <= steps + 1; number++) {
        await page.locator('#log-archive-older').click();
        await waitText(page, '#log-archive-status', 'Sayfa ' + number + '$');
    }
}

async function displayLocalTime(page, selector, at) {
    await page.locator(selector).evaluate((input, epoch) => {
        const local = new Date(epoch);
        const numeric = [local.getFullYear(), local.getMonth() + 1, local.getDate(), local.getHours(),
            local.getMinutes(), local.getSeconds()].map(x => String(x).padStart(2, '0'));
        input.value = numeric.slice(0, 3).join('-') + 'T' + numeric.slice(3).join(':');
        input.dispatchEvent(new Event('input', { bubbles: true }));
    }, at);
}

function txtBlocks(file) {
    return fs.readFileSync(file, 'utf8').trimEnd().split('\n\n').slice(1);
}

async function verifyIncident(page) {
    await page.locator('#log-stage').selectOption('wemos_stream');
    await page.locator('#log-source').selectOption('wemos');
    await page.locator('#log-level').selectOption('WARNING');
    await page.locator('#log-search').fill('ANCHOR-Wemos-stream');
    await displayLocalTime(page, '#log-from', anchorTime);
    await displayLocalTime(page, '#log-to', anchorTime);
    await waitText(page, '#log-archive-status', 'Sayfa 1$');
    await page.locator('#log-device').selectOption(selectedDevice);
    await waitText(page, '#log-archive-status', 'Sayfa 1$');
    const anchor = page.locator('#log-entries .log-entry').filter({ hasText: 'C4 ANCHOR-Wemos-stream' });
    assert.deepEqual(await messages(page), ['C4 ANCHOR-Wemos-stream']);
    await anchor.locator('summary').first().click();
    await anchor.getByRole('button', { name: 'Olayın 30 sn öncesi ve sonrası' }).click();
    await page.waitForFunction(() => document.querySelector('#log-context-title').textContent.includes('±30 sn') &&
        !document.querySelector('#log-context-title').textContent.includes('okunuyor'));
    const expected = journalFixtures.filter(row => Math.abs(formats.eventTime(row) - anchorTime) <= 30_000)
        .sort((a, b) => formats.eventTime(a) - formats.eventTime(b));
    assert.deepEqual(await messages(page), expected.map(row => row.msg));
    await page.locator('#log-incident-export').click();
    await waitText(page, '#log-incident-export-status', 'kaydedildi');
    const actual = txtBlocks(path.join(desktop, 'incident-test.txt'));
    assert.deepEqual(actual, expected.map(row => formats.formatEntry(row)));
    assert.match(await page.locator('#log-incident-export-path').textContent(), /incident-test\.txt$/);
    console.log('PASS: source, device, stage, severity, search and local time filters; ±30s includes exact edges; 6 UI/TXT rows match');
    await page.locator('#log-context-close').click();
    assert.deepEqual(await messages(page), ['C4 ANCHOR-Wemos-stream']);
    await page.locator('#log-mode').click();
    for (const name of ['source', 'level', 'stage']) await page.locator('#log-' + name).selectOption('');
    await page.locator('#log-device').selectOption('');
    await page.locator('#log-search').fill('');
    for (const name of ['from', 'to']) await page.locator('#log-' + name).fill('');
}

async function verifyLive(page) {
    await page.locator('#log-live-file-start').click();
    await waitText(page, '#log-live-file-status', 'yazılıyor');
    simulatedEvents.push({ seq: 2, uptime_ms: 180, code: 18, arg: 74 },
        { seq: 3, uptime_ms: 195, code: 19, arg: 85 });
    await page.waitForFunction(() => [...document.querySelectorAll('#log-entries .log-msg')]
        .some(el => el.textContent.includes('Wemos akış kesilince bekleme animasyonuna geçti')), undefined, { timeout: 20000 });
    await page.locator('#log-live-file-stop').click();
    await waitText(page, '#log-live-file-status', 'durduruldu');
    await page.evaluate(() => fetchLogsData());
    const visible = await page.evaluate(() => visibleLogs);
    const live = txtBlocks(path.join(desktop, 'live-test.txt'));
    const rendered = new Set(visible.map(row => formats.formatEntry(row)));
    assert.ok(live.length >= 2);
    assert.ok(live.every(row => rendered.has(row)), 'each live TXT record matches a renderer row');
    const simulated = readRows(path.join(logDirectory, 'luxedge.jsonl')).filter(row =>
        row.details?.boot_id === boot && [2, 3].includes(row.details?.seq));
    assert.equal(simulated.length, 2);
    for (const row of simulated) assert.ok(live.includes(formats.formatEntry(row)));
    assert.ok(live.every(row => !row.includes('C4 archive-filler')));
    console.log('PASS: live TXT starts and stops via packaged IPC, excludes past rows, and matches ' + live.length + ' displayed records');
    return live.length;
}

async function main() {
    seedArchive();
    const simulatedHttp = http.createServer(respondToDevice);
    const udp = await reserveUdp();
    try {
        await listen(simulatedHttp, 80, deviceIp);
        const reserve = net.createServer();
        const backendPort = await listen(reserve, 0, '127.0.0.1');
        await new Promise(resolve => reserve.close(resolve));
        fs.writeFileSync(path.join(logDirectory, 'ambilight_config.json'), JSON.stringify({
            wemos_ip: deviceIp, wemos_port: udp.address().port, multi_device_enabled: false,
            devices: [], fps: 10, led_monitor_index: 1, top_leds: 24, bottom_leds: 24, left_leds: 13, right_leds: 13,
        }));
        const rendererErrors = [];
        let page = await launch(backendPort, { rendererErrors });
        await page.waitForFunction(async expectedVersion => {
            const result = await window.luxedge.getStatus();
            return result.backend_version === expectedVersion && result.connectivity?.state === 'connected';
        }, luxedgeVersion);
        await page.locator('#log-autoscroll').uncheck();
        const backendVersion = (await page.evaluate(() => window.luxedge.getStatus())).backend_version;
        await page.waitForFunction(() => visibleLogs.length === 1000);
        const liveTail = await page.evaluate(() => visibleLogs.map(row => row.msg));
        assert.equal(liveTail.length, 1000);
        await page.locator('#log-mode').click();
        await waitText(page, '#log-archive-status', 'Sayfa 1$');
        await page.locator('#log-search').fill('archive-filler');
        await waitText(page, '#log-archive-status', 'Sayfa 1$');
        await pageToOlder(page, 11);
        const oldRows = await messages(page);
        assert.equal(oldRows.length, 100);
        assert.ok(oldRows.every(row => row.includes('archive-filler') && !liveTail.includes(row)));
        console.log('PASS: packaged archive page 12 displays 100 indexed records older than the 1,000-item live view');
        await verifyIncident(page);
        const liveCount = await verifyLive(page);
        const chosenPaths = await electron.evaluate(() => globalThis.c4Dialogs);
        assert.equal(chosenPaths.length, 2);
        assert.ok(chosenPaths.every(file => isInside(desktop, file)));
        const segments = () => fs.readdirSync(logDirectory).filter(file => /\.seg-/.test(file));
        const before = segments().length;
        assert.equal(before, 5);
        await page.locator('.log-retention > summary').click();
        await waitText(page, '#log-retention-usage', '/ 512 MiB');
        await page.locator('#log-retention-limit').fill('8');
        await page.locator('#log-retention-save').click();
        await page.locator('#dialog-confirm').click();
        await waitText(page, '#log-retention-usage', '/ 8 MiB(?: ·|$)');
        assert.equal(JSON.parse(fs.readFileSync(path.join(logDirectory, 'luxedge-log-retention.json'))).limit_bytes, 8 * mib);
        assert.ok(segments().length < before);
        assert.ok(segments().reduce((sum, file) => sum + fs.statSync(path.join(logDirectory, file)).size, 0) <= 4 * mib);
        assert.ok(fs.existsSync(path.join(logDirectory, 'c4-do-not-prune.txt')));
        await electron.close();
        electron = null;
        page = await launch(backendPort, { rendererErrors });
        await page.locator('.log-retention > summary').click();
        await waitText(page, '#log-retention-usage', '/ 8 MiB(?: ·|$)');
        assert.equal(await page.locator('#log-retention-limit').inputValue(), '8');
        await page.locator('#log-mode').click();
        await waitText(page, '#log-archive-status', 'Sayfa 1$');
        await page.locator('#log-search').fill('archive-filler');
        await waitText(page, '#log-archive-status', 'Sayfa 1$');
        await pageToOlder(page, 10);
        assert.equal((await messages(page)).length, 100);
        const dialogs = await electron.evaluate(() => globalThis.c4Dialogs);
        assert.deepEqual(dialogs, []);
        assert.deepEqual(rendererErrors, []);
        console.log('PASS: 8 MiB retention prunes old managed segments, preserves unmanaged files and over 1,000 rows, survives restart');
        fs.writeFileSync(path.join(profile, 'result.json'), JSON.stringify({
            verifiedAt: new Date().toISOString(), executable, profile, backendVersion,
            archiveDepth: 1200, incidentRows: 6, liveRows: liveCount, retentionMiB: 8,
            originalSegments: before, retainedSegments: segments().length, rendererErrors,
            nativeSaveDialogTestDouble: true, windowsPolicyStatusTestDouble: true,
        }, null, 2) + '\n');
        console.log('Evidence: ' + path.join(profile, 'result.json'));
    } finally {
        if (electron) await electron.close().catch(() => {});
        udp.close();
        simulatedHttp.closeAllConnections();
        simulatedHttp.close();
    }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
