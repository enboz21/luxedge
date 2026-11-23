const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const { chooseTxt, LiveLogFile, writeIncident } = require('../live_log');
const { formatEntry, eventTime } = require('../web_ui/log_format');

function profile(t) {
    const sandbox = path.resolve(__dirname, '../build/tests');
    fs.mkdirSync(sandbox, { recursive: true });
    const directory = fs.mkdtempSync(path.join(sandbox, 'luxedge-live-test-'));
    t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
    return directory;
}

function row(id, source, order, details = {}) {
    return { id, source, archive_order_us: order, timestamp: new Date(Math.trunc(order / 1000)).toISOString(),
        msg: 'entry-' + id, level: 'INFO', code: 'fixture', details };
}

function saveRows(directory, filename, rows) {
    fs.writeFileSync(path.join(directory, filename), rows.map(item => JSON.stringify(item)).join('\n') + '\n', 'utf8');
}

function journal(entries, { overflow = false, generation = 'test-generation' } = {}) {
    return async (since, through, after) => {
        const rows = entries.filter(item => item.id > after && item.archive_order_us >= since && item.archive_order_us <= through);
        return { logs: rows.slice(0, 512), has_more: rows.length > 512, generation, overflow: overflow && after === 0 };
    };
}

test('native chooser defaults to Desktop, accepts cancellation and normalizes TXT extension', async t => {
    const desktop = profile(t);
    let options;
    const dialog = { async showSaveDialog(_window, settings) {
        options = settings;
        return { canceled: true };
    } };
    assert.equal(await chooseTxt(dialog, {}, desktop, 'luxedge-canli-gunluk'), null);
    assert.equal(path.dirname(options.defaultPath), desktop);
    assert.match(path.basename(options.defaultPath), /^luxedge-canli-gunluk-.*\.txt$/);
    assert.deepEqual(fs.readdirSync(desktop), []);
    dialog.showSaveDialog = async () => ({ canceled: false, filePath: path.join(desktop, 'günlük') });
    assert.equal(await chooseTxt(dialog, {}, desktop, 'ignored'), path.join(desktop, 'günlük.txt'));
});

test('live file exists immediately, writes four sources and paginates beyond 1,000 without duplicates', async t => {
    const directory = profile(t);
    const target = path.join(directory, 'canlı.txt');
    const rows = [];
    const writer = await LiveLogFile.start(target, directory, journal(rows));
    t.after(() => writer.stop());
    assert.match(fs.readFileSync(target, 'utf8'), /^LuxEdge canlı günlük/);
    const sources = ['pc', 'wemos', 'windows'];
    const proof = { event_at: new Date(Math.trunc(writer.since / 1000)).toISOString(),
        event_channel: 'Security', event_id: 5157, event_record_id: 184, filter_origin: 'test-filter-ı' };
    for (let id = 1; id <= 1500; id++) rows.push(row(id, sources[id % 3], writer.since + Math.floor(id / 2), id === 2 ? proof : {}));
    const app = row('electron:one', 'electron', writer.since + 3);
    writer.addElectron(app);
    const status = await writer.stop();
    assert.deepEqual({ ...status, message: '' }, { state: 'stopped', path: target, written: 1501, message: '' });
    assert.equal(status.message, '');
    const text = fs.readFileSync(target, 'utf8');
    assert.equal((text.match(/ \| entry-/g) || []).length, 1501);
    assert.ok(text.includes(formatEntry(rows[1])));
    assert.ok(text.includes(formatEntry(app)));
    assert.match(text, /WFP filtre kökeni · test-filter-ı/);
    assert.match(text, /GMT[+-]\d{2}:\d{2}/);
    assert.ok(text.indexOf(' | entry-1\n') < text.indexOf(' | entry-1500\n'));
    assert.deepEqual(await writer.stop(), status);
    await assert.rejects(LiveLogFile.start(target, directory, journal([])), { code: 'EEXIST' });
    assert.equal(fs.readFileSync(target, 'utf8'), text);
});

test('archive fallback recovers past the 8,192-row journal horizon and deduplicates overlapping rows', async t => {
    const directory = profile(t);
    const target = path.join(directory, 'overflow.txt');
    const recent = [];
    const writer = await LiveLogFile.start(target, directory, journal(recent, { overflow: true }));
    const all = Array.from({ length: 8210 }, (_, index) => row(index + 1, 'pc', writer.since));
    saveRows(directory, 'luxedge.jsonl.1', all.slice(0, 600));
    saveRows(directory, 'luxedge.jsonl', all.slice(600));
    recent.push(...all.slice(-8192));
    const result = await writer.stop();
    assert.equal(result.state, 'stopped');
    assert.equal(result.written, all.length);
    assert.match(result.message, /boşluk/);
    const saved = fs.readFileSync(target, 'utf8');
    assert.equal((saved.match(/ \| entry-/g) || []).length, all.length);
    assert.equal((saved.match(/ \| entry-19\n/g) || []).length, 1);
    assert.match(saved, /PC canlı tamponunda boşluk/);
});

test('backend and archive read errors leave Electron output writable with a gap notice', async t => {
    const directory = profile(t);
    const target = path.join(directory, 'offline.txt');
    const writer = await LiveLogFile.start(target, directory, async () => { throw new Error('offline'); });
    saveRows(directory, 'luxedge.jsonl', [row(1, 'pc', writer.since)]);
    const original = fs.promises.open;
    t.mock.method(fs.promises, 'open', (file, ...args) => {
        if (file === path.join(directory, 'luxedge.jsonl')) throw Object.assign(new Error('unreadable'), { code: 'EACCES' });
        return original(file, ...args);
    });
    writer.addElectron(row('electron:offline', 'electron', writer.since));
    const result = await writer.stop();
    assert.equal(result.written, 1);
    assert.equal(result.state, 'stopped');
    assert.match(result.message, /PC canlı akışı kullanılamıyor/);
    const saved = fs.readFileSync(target, 'utf8');
    assert.match(saved, /entry-electron:offline/);
    assert.match(saved, /PC canlı tamponunda boşluk/);
});

test('journal reconnect fills a record missing from disk without repeating archived rows', async t => {
    const directory = profile(t);
    const file = path.join(directory, 'resumed.txt');
    const entries = [];
    let online = false;
    const reply = journal(entries);
    const writer = await LiveLogFile.start(file, directory, async (...args) => online ? reply(...args) : null);
    clearTimeout(writer.timer);
    for (let id = 1; id <= 4; id++) entries.push(row(id, 'pc', writer.since + id));
    saveRows(directory, 'luxedge.jsonl', [entries[0], entries[2]]);
    await writer.collect(writer.since + 999);
    assert.equal(writer.status().written, 2);
    online = true;
    assert.equal((await writer.stop()).written, 4);
    const text = fs.readFileSync(file, 'utf8');
    for (const item of entries) assert.equal(text.split(` | ${item.msg}\n`).length - 1, 1);
});

test('write failure stops safely, keeps the path and does not miscount flushed rows', async t => {
    const directory = profile(t);
    const target = path.join(directory, 'out-of-space.txt');
    const writer = await LiveLogFile.start(target, directory, journal([]));
    t.mock.method(writer.handle, 'write', async () => { throw Object.assign(new Error('full'), { code: 'ENOSPC' }); });
    writer.addElectron(row('electron:failure', 'electron', writer.since));
    const failed = await writer.stop();
    assert.deepEqual(failed, { state: 'error', path: target, written: 0, message: 'Seçilen sürücüde boş alan kalmadı.' });
    assert.match(fs.readFileSync(target, 'utf8'), /^LuxEdge canlı günlük/);
    assert.equal(writer.handle.fd, -1);
});

test('incident export includes every archive page plus distinct recent rows and uses the event clock', async t => {
    const directory = profile(t);
    const target = path.join(directory, 'incident.txt');
    const center = Date.parse('2026-11-22T23:30:45.123+03:00');
    const entries = Array.from({ length: 410 }, (_, index) => row(index + 1, index % 2 ? 'wemos' : 'pc',
        (center + index) * 1000, index % 2 ? { event_timestamp_estimated: new Date(center - index).toISOString() } : {}));
    const unrelated = row(900, 'pc', (center - 30_001) * 1000);
    const duplicate = entries[5];
    const recent = row('electron:recent', 'electron', (center + 510) * 1000);
    saveRows(directory, 'luxedge.jsonl.2', [unrelated, ...entries.slice(0, 204)]);
    saveRows(directory, 'luxedge.jsonl', entries.slice(204));
    const result = await writeIncident(target, directory, center - 30_000, center + 30_000, '7', [duplicate, recent]);
    assert.deepEqual(result, { state: 'saved', path: target, written: 411 });
    const text = fs.readFileSync(target, 'utf8');
    assert.match(text, /Seçilen kayıt · 7/);
    assert.doesNotMatch(text, /entry-900/);
    assert.equal((text.match(/ \| entry-6\n/g) || []).length, 1);
    const expected = [...entries, recent].sort((a, b) => eventTime(a) - eventTime(b));
    const written = text.split('\n').flatMap(line => line.includes(' | entry-') ? [line.split(' | ').at(-1)] : []);
    assert.deepEqual(written, expected.map(item => item.msg));
    await assert.rejects(writeIncident(target, directory, center - 30_000, center + 30_000, '7'), { code: 'EEXIST' });
    assert.equal(fs.readFileSync(target, 'utf8'), text);
});
