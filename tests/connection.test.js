const test = require('node:test');
const assert = require('node:assert/strict');
const { renderConnection } = require('../web_ui/connection');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function elements() {
    return { statusText: {}, statusDot: {}, connStatus: { style: {} } };
}

test('all states use identical labels and consistent colors on both indicators', () => {
    const els = elements();
    for (const [state, cls, color] of [
        ['connected', 'active', 'success-color'],
        ['http_only', 'warning', 'warning-color'],
        ['checking', 'checking', 'accent-color'],
        ['disconnected', '', 'danger-color'],
        ['stopped', 'neutral', 'text-muted'],
        ['unconfigured', 'neutral', 'text-muted'],
        ['backend_unavailable', '', 'danger-color'],
    ]) {
        renderConnection(els, { connectivity: { state }, actual_fps: 60, packets_sent: 12345 });
        assert.equal(els.statusDot.className, `status-dot ${cls}`.trim());
        assert.equal(els.connStatus.style.color, `var(--${color})`);
        assert.equal(els.statusText.textContent, els.connStatus.textContent);
    }
});

test('repeated polls and protocol changes immediately update the entire presentation', () => {
    const els = elements();
    for (const state of ['connected', 'http_only', 'connected', ...Array(10).fill('disconnected'), 'connected']) {
        renderConnection(els, { connectivity: { state } });
        assert.equal(els.statusDot.className.includes('active'), state === 'connected');
        assert.equal(els.statusDot.className.includes('warning'), state === 'http_only');
    }
});

test('FPS and legacy connection strings never substitute for evidence', () => {
    const els = elements();
    renderConnection(els, { connection: 'bağlı', actual_fps: 60, packets_sent: 1000 });
    assert.equal(els.connStatus.style.color, 'var(--danger-color)');
    assert.match(els.statusText.textContent, /hizmetine/);
});

test('a late manual test cannot change the current target or main indicator', async () => {
    const html = fs.readFileSync(path.join(__dirname, '../web_ui/index.html'), 'utf8');
    const start = html.indexOf('        async function testWemosConnection()');
    const end = html.indexOf('        // --- Actions ---', start);
    const nodes = new Map();
    let resolve, refreshes = 0;
    const context = vm.createContext({
        connectionTestRequest: 0,
        document: { getElementById(id) {
            if (!nodes.has(id)) nodes.set(id, { classList: { add() {} }, textContent: '' });
            return nodes.get(id);
        } },
        api: { testWemosConnection: () => new Promise(done => { resolve = done; }) },
        fetchStatus: async () => { refreshes++; },
    });
    vm.runInContext(html.slice(start, end), context);
    const pending = vm.runInContext('testWemosConnection()', context);
    context.connectionTestRequest++; // A poll changed the target while this test was in flight.
    resolve({ udp_ok: true, http_ok: true, applied: false });
    await pending;
    assert.equal(refreshes, 0);
    assert.equal(nodes.get('udp-test-result').textContent, 'Test ediliyor...');
    const current = vm.runInContext('testWemosConnection()', context);
    resolve({ udp_ok: false, http_ok: true, applied: true });
    await current;
    assert.equal(refreshes, 1); // A fresh status fetch, never a direct indicator overwrite.
    assert.equal(nodes.get('http-test-result').textContent, '✅ Başarılı');
});
