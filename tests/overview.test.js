const test = require('node:test');
const assert = require('node:assert/strict');
const { describe, hex } = require('../web_ui/overview');
const now = 1000;
function device(strips, extra = {}) {
    return { id: 'desk', enabled: true, live: { reachable: true, checked_at: now,
        status: { strips, sleep_mode: false } }, ...extra };
}
const strip = (mode, extra = {}) => ({ index: 1, mode, color: '#ff8000', led_count: 20,
    brightness: 128, ...extra });
test('local effects keep running when PC multi-device mode is disabled', () => {
    const model = describe(device([strip('breathe')]), false, now);
    assert.equal(model.state, 'active');
    assert.equal(model.rows[0].code, '#804000');
    assert.match(model.rows[0].note, /Temsili/);
});
test('sleep and black output are off, rather than paused or running', () => {
    const d = device([strip('rainbow')]); d.live.status.sleep_mode = true;
    assert.equal(describe(d, true, now).state, 'off');
    assert.equal(describe(d, true, now).rows[0].code, '#000000');
    assert.equal(describe(device([strip('solid', { brightness: 0 })]), true, now).state, 'off');
    assert.equal(describe(device([strip('off')]), true, now).state, 'off');
});
test('outgoing stream data expires and disabled streams pause', () => {
    const d = device([strip('stream')], { frame: { updated_at: now, colors: [[1, 2, 3]], mode: 'stream' } });
    assert.equal(describe(d, true, now).rows[0].code, '#010203');
    d.frame.streams = { '1': [[7, 8, 9]] };
    assert.equal(describe(d, true, now).rows[0].code, '#070809');
    assert.equal(describe(d, true, now).state, 'active');
    assert.equal(describe(d, false, now).state, 'paused');
    assert.equal(describe(d, true, now + 4).state, 'paused');
});
test('offline and stale statuses never claim an active effect', () => {
    const d = device([strip('chase')]); d.live.reachable = false;
    assert.equal(describe(d, true, now).online, false);
    assert.equal(describe(d, true, now).state, 'paused');
    assert.equal(describe(d, true, now + 16).online, null);
    assert.equal(describe(d, true, now + 16).rows[0].code, null);
});
test('firmware settings beat unsent config and every physical strip is shown', () => {
    const d = device([strip('twinkle'), strip('off', { index: 2 }), strip('solid', { led_count: 0 })]);
    d.strips = [strip('rainbow')];
    const model = describe(d, true, now);
    assert.deepEqual(model.rows.map(s => s.mode), ['twinkle', 'off']);
    assert.equal(model.state, 'active');
});
test('legacy firmware uses fresh main stream samples; idle color is named', () => {
    const d = device(undefined, { id: 'legacy-primary', frame: { updated_at: now,
        colors: [[12, 34, 56]], mode: 'solid' } });
    assert.equal(describe(d, false, now).state, 'active');
    assert.equal(describe(d, false, now).rows[0].name, 'Bekleme rengi');
    assert.equal(hex([12, 34, 56]), '#0C2238');
});
test('1.6.4 physical strip metadata without modes displays the outgoing stream', () => {
    const d = device([{ index: 1, enabled: true, led_count: 74, led_pin: 'D2' },
        { index: 2, enabled: false, led_count: 0 }], { id: 'legacy-primary',
        frame: { updated_at: now, colors: [[12, 34, 56]], mode: 'stream' } });
    const result = describe(d, false, now);
    assert.equal(result.state, 'active');
    assert.equal(result.rows.length, 1);
    assert.equal(result.rows[0].name, 'Ekran akışı');
    assert.equal(result.rows[0].code, '#0C2238');
});
