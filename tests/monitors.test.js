const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { renderMonitorOptions, createMonitorRefresh } = require('../web_ui/monitors');

function select(value) {
    return { value, options: [], replaceChildren(...options) { this.options = options; } };
}
const document = { createElement: () => ({}) };

test('refresh only runs on click, blocks double clicks and releases busy state', async () => {
    let calls = 0;
    let resolve;
    const busy = [], applied = [];
    const refresh = createMonitorRefresh({ refreshMonitors: () => {
        calls++; return new Promise(done => { resolve = done; });
    } }, result => applied.push(result), state => busy.push(state), () => {});
    assert.equal(calls, 0);
    const pending = refresh();
    await refresh();
    assert.equal(calls, 1);
    resolve({ success: true, monitors: [{ index: 1 }] });
    await pending;
    assert.equal(applied.length, 1);
    assert.deepEqual(busy, [true, false]);
});

test('refresh failure preserves list and reenables button', async () => {
    const busy = [], messages = [];
    const refresh = createMonitorRefresh({ refreshMonitors: async () => ({ success: false, message: 'failed' }) },
        () => assert.fail('must preserve old list'), state => busy.push(state), text => messages.push(text));
    await refresh();
    assert.deepEqual(busy, [true, false]);
    assert.deepEqual(messages, ['failed']);
});

test('monitor options preserve selection including a disconnected display', () => {
    const picker = select('2');
    const list = [{ index: 1, name: 'First', width: 1920, height: 1080 }];
    renderMonitorOptions(picker, list, picker.value, document);
    assert.equal(picker.value, '2');
    assert.match(picker.options[1].textContent, /listede yok/);
    list.push({ index: 2, name: 'Second', width: 2560, height: 1440 });
    renderMonitorOptions(picker, list, picker.value, document);
    assert.equal(picker.options.length, 2);
    assert.match(picker.options[1].textContent, /Second/);
    assert.equal(picker.value, '2');
});

test('status polls do not rebuild lists; explicit newer generation updates both selectors', () => {
    const html = fs.readFileSync(path.join(__dirname, '../web_ui/index.html'), 'utf8');
    const start = html.indexOf('        function applyMonitorSnapshot(');
    const end = html.indexOf('        const refreshMonitors', start);
    const led = select('1'), device = select('2');
    let renders = 0;
    const context = vm.createContext({
        monitorListVersion: -1, availableMonitors: [], els: { ledMonitorIndex: led },
        document: { ...document, getElementById: () => device },
        renderMonitorOptions(...args) { renders++; renderMonitorOptions(...args); },
    });
    vm.runInContext(html.slice(start, end), context);
    const snapshot = { monitor_list_version: 1, led_monitor_index: 2, monitors: [{ index: 2, name: 'Old' }] };
    context.applyMonitorSnapshot(snapshot);
    for (let i = 0; i < 60; i++) context.applyMonitorSnapshot(snapshot);
    assert.equal(renders, 2);
    assert.equal(led.value, '2');
    context.applyMonitorSnapshot({ ...snapshot, monitor_list_version: 2, monitors: [{ index: 2, name: 'New' }] });
    assert.equal(renders, 4);
    assert.match(device.options[0].textContent, /New/);
    context.applyMonitorSnapshot(snapshot); // late response must not replace the refreshed list
    assert.equal(renders, 4);
});
