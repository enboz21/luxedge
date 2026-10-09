// Controlled local UDP peer; user configuration and physical LEDs are untouched.
// Set PLAYWRIGHT_MODULE_PATH when Playwright is supplied by an external runtime.
const { _electron } = require(process.env.PLAYWRIGHT_MODULE_PATH || 'playwright');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const dgram = require('node:dgram');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const executable = path.join(root, 'dist/win-unpacked/LuxEdge.exe');
const profile = fs.mkdtempSync(path.join(root, 'build/packaged-smoke-'));
const configDir = path.join(profile, 'LuxEdge');
fs.mkdirSync(configDir);
const config = JSON.parse(fs.readFileSync(path.join(root, 'ambilight_config.json'), 'utf8'));
Object.assign(config, { wemos_ip: '127.0.0.1', wemos_port: 17777, led_monitor_index: 1 });
fs.writeFileSync(path.join(configDir, 'ambilight_config.json'), JSON.stringify(config));
const env = { ...process.env, APPDATA: profile, LUXEDGE_BACKEND_PORT: '18888' };
const userDataArg = `--user-data-dir=${path.join(profile, 'electron')}`;
delete env.ELECTRON_RUN_AS_NODE;
let app, peer;
async function screenshot(name) {
    const png = await app.evaluate(async ({ BrowserWindow }) => {
        const window = BrowserWindow.getAllWindows()[0];
        window.showInactive();
        try {
            // A restored hidden window can initially expose its previous compositor frame.
            await new Promise(resolve => setTimeout(resolve, 500));
            const image = await Promise.race([
                window.capturePage(),
                new Promise((_, reject) => setTimeout(() => reject(new Error('Screenshot timed out')), 5000))
            ]);
            return image.toPNG().toString('base64');
        } finally { window.hide(); }
    });
    fs.writeFileSync(path.join(profile, name), Buffer.from(png, 'base64'));
}
async function secondLaunch(args) {
    await new Promise((resolve, reject) => {
        const child = spawn(executable, [userDataArg, ...args], { env, windowsHide: true });
        child.once('error', reject);
        child.once('exit', code => code === 0 ? resolve() : reject(new Error(`Second launch: ${code}`)));
    });
}
async function waitState(page, state) {
    const deadline = Date.now() + 25000;
    let status;
    while (Date.now() < deadline) {
        status = await page.evaluate(() => window.luxedge.getStatus());
        if (status.connectivity?.state === state) return status;
        await new Promise(resolve => setTimeout(resolve, 500));
    }
    throw new Error(`Expected ${state}: ${JSON.stringify(status)}`);
}
(async () => {
    app = await _electron.launch({ executablePath: executable,
        args: [userDataArg, '--start-in-tray', '--disable-gpu', '--in-process-gpu', '--no-sandbox'], env, timeout: 30000 });
    app.process().stdout.on('data', data => process.stdout.write(data));
    app.process().stderr.on('data', data => process.stderr.write(data));
    await app.context().route('https://fonts.googleapis.com/**', route => route.abort());
    await app.context().route('https://fonts.gstatic.com/**', route => route.abort());
    const page = await app.firstWindow({ timeout: 30000 });
    page.on('pageerror', error => console.error('Renderer:', error.message));
    console.log('Window created:', await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().map(w => ({
        url: w.webContents.getURL(), loading: w.webContents.isLoading(), crashed: w.webContents.isCrashed(), visible: w.isVisible()
    }))));
    await page.waitForLoadState('domcontentloaded');
    // Capture final state colors, not an intermediate CSS transition frame.
    await page.addStyleTag({ content: '.status-dot { transition: none !important; animation: none !important; }' });
    const visible = () => app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].isVisible());
    assert.equal(await visible(), false);
    console.log('PASS: packaged startup stays hidden');
    await secondLaunch(['--start-in-tray']);
    assert.equal(await visible(), false);
    await secondLaunch([]);
    assert.equal(await visible(), true);
    await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].hide());
    console.log('PASS: automatic second launch stays hidden; manual shortcut opens panel');
    const disconnected = await waitState(page, 'disconnected');
    console.log('Disconnected snapshot:', JSON.stringify(disconnected));
    assert.ok(disconnected.actual_fps > 0, 'Capture should run despite missing device');
    await page.evaluate(() => fetchStatusData());
    await page.waitForFunction(() => document.getElementById('conn-status').textContent === 'Bağlantı yok');
    assert.equal(await page.locator('#status-dot').evaluate(el => getComputedStyle(el).backgroundColor),
        await page.locator('#conn-status').evaluate(el => getComputedStyle(el).color));
    console.log('PASS: positive FPS does not mask missing Wemos; red label and dot agree');
    peer = dgram.createSocket('udp4');
    peer.on('message', (message, remote) => {
        if (message.equals(Buffer.from('PING'))) peer.send('PONG', remote.port, remote.address);
    });
    await new Promise(resolve => peer.bind(17777, '127.0.0.1', resolve));
    await waitState(page, 'connected');
    await page.evaluate(() => fetchStatusData());
    await page.waitForFunction(() => document.getElementById('status-dot').classList.contains('active'));
    assert.equal(await page.locator('#conn-status').innerText(), 'Bağlı');
    await screenshot('connected.png');
    console.log('PASS: real UDP PONG recovers green connection');
    await page.evaluate(() => testWemosConnection());
    await page.evaluate(() => fetchStatusData());
    assert.equal(await page.locator('#conn-status').innerText(), 'Bağlı');
    console.log('PASS: manual full probe and automatic poll keep the same green status');
    await new Promise(resolve => peer.close(resolve));
    peer = null;
    await waitState(page, 'disconnected');
    await page.evaluate(() => fetchStatusData());
    await page.waitForFunction(() => document.getElementById('conn-status').textContent === 'Bağlantı yok');
    await screenshot('disconnected.png');
    console.log('PASS: device removal becomes red after three failed probes');
    fs.writeFileSync(path.join(profile, 'result.json'), JSON.stringify({ passed: true, profile }, null, 2));
    console.log('Evidence:', profile);
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => {
    if (peer) peer.close();
    if (app) await app.close();
});
