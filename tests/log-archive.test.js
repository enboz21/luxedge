const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const archive = require('../log_archive');

const FIXED_INT = new Date('2026-11-22T20:20:20.123Z').getTime();

function profile(t) {
    const sandbox = path.resolve(__dirname, '../build/tests');
    fs.mkdirSync(sandbox, { recursive: true });
    const root = fs.mkdtempSync(path.join(sandbox, 'luxedge-archive-test-'));
    t.after(() => fs.rmSync(root, { recursive: true, force: true }));
    return root;
}

function row(id, source, options = {}) {
    const timestamp = options.timestamp || new Date(FIXED_INT).toISOString();
    return { source, id, timestamp, level: 'INFO', msg: String(id), ...options };
}

function writeRows(root, name, rows) {
    const file = path.join(root, name);
    fs.writeFileSync(file, rows.map(row => JSON.stringify(row)).join('\n') + '\n');
    return file;
}

test('archive cursor spans 1,400 rows from both writers with timestamp ties and mid-read rotation', async t => {
    const root = profile(t);
    const backend = Array.from({ length: 700 }, (_, i) => row(1 + i, i % 2 ? 'pc' : 'wemos'));
    const electron = Array.from({ length: 700 }, (_, i) => row(`electron:${String(1 + i).padStart(4, '0')}`, 'electron'));
    const pcPath = writeRows(root, 'luxedge.jsonl', backend);
    const elPath = writeRows(root, 'luxedge-electron.jsonl', electron);
    const first = await archive.archivePage(root, { limit: 177 });
    assert.equal(first.logs.length, 177);
    assert.equal(first.retention.oldest_timestamp, new Date(FIXED_INT).toISOString());

    archive.rotateElectronLog(pcPath, fs.statSync(pcPath).size);
    archive.rotateElectronLog(elPath, fs.statSync(elPath).size);
    writeRows(root, 'luxedge.jsonl', [row(701, 'windows', { archive_order_us: FIXED_INT * 1000 + 1 })]);
    writeRows(root, 'luxedge-electron.jsonl', [row('electron:0701', 'electron', { archive_order_us: FIXED_INT * 1000 + 1 })]);

    const loaded = first.logs.slice();
    let next = first.next_cursor;
    while (next) {
        const result = await archive.archivePage(root, { before: next, limit: 177 });
        assert.ok(result.logs.length > 0);
        loaded.push(...result.logs);
        next = result.next_cursor;
    }
    const ids = loaded.map(item => item.id);
    assert.deepEqual(ids, [...electron.reverse(), ...backend.reverse()].map(item => item.id));
    assert.equal(new Set(ids).size, 1400);
    const refreshed = await archive.archivePage(root, { limit: 2 });
    assert.deepEqual(refreshed.logs.map(item => item.id), ['electron:0701', 701]);
});

test('archive retries a file replaced between inventory and read, and surfaces access failure', async t => {
    const root = profile(t);
    const active = writeRows(root, 'luxedge.jsonl', [row(1, 'pc')]);
    const original = fs.promises.open;
    let replaced = false;
    const hook = t.mock.method(fs.promises, 'open', async (file, ...args) => {
        if (file === active && !replaced) {
            replaced = true;
            archive.rotateElectronLog(active, fs.statSync(active).size);
            writeRows(root, 'luxedge.jsonl', [row(2, 'pc', { archive_order_us: FIXED_INT * 1000 + 5 })]);
        }
        return original(file, ...args);
    });
    assert.deepEqual((await archive.archivePage(root)).logs.map(item => item.id), [2, 1]);
    assert.equal(replaced, true);
    hook.mock.restore();
    t.mock.method(fs.promises, 'open', async () => { throw Object.assign(new Error('access denied fixture'), { code: 'EACCES' }); });
    await assert.rejects(archive.archivePage(root), /access denied fixture/);
});

test('budget is shared by both writers, applied immediately, and keeps unrelated exports and legacy text', async t => {
    const root = profile(t);
    const MiB = 1024 * 1024;
    function sized(name, modified) {
        const file = path.join(root, name);
        const descriptor = fs.openSync(file, 'w');
        fs.ftruncateSync(descriptor, archive.SEGMENT_BYTES);
        fs.closeSync(descriptor);
        fs.utimesSync(file, modified, modified);
        return file;
    }
    const active = [sized('luxedge.jsonl', 100), sized('luxedge-electron.jsonl', 101)];
    const remove = [sized('luxedge.jsonl.2', 80), sized('luxedge-electron.jsonl.2', 81)];
    const keep = [sized('luxedge.jsonl.1', 90), sized('luxedge-electron.jsonl.1', 91)];
    const legacy = [sized('luxedge.log', 60), sized('luxedge.log.1', 61)];
    const unrelated = [sized('luxedge.jsonl.99', 60), sized('keep.txt', 60), sized('config.json', 60)];
    assert.equal(archive.retentionLimit(root), 512 * MiB);
    await assert.rejects(archive.setRetention(root, 8 * MiB - 1), RangeError);
    assert.equal(fs.existsSync(path.join(root, 'luxedge-log-retention.json')), false);
    await archive.setRetention(root, 10 * MiB);
    assert.equal(archive.retentionLimit(root), 10 * MiB);
    assert.equal(remove.map(file => fs.existsSync(file)).filter(Boolean).length, 1);
    await archive.setRetention(root, 8 * MiB);
    await Promise.all([archive.prune(root), archive.prune(root)]);
    assert.equal(remove.some(file => fs.existsSync(file)), false);
    assert.ok([...active, ...keep, ...legacy, ...unrelated].every(file => fs.existsSync(file)));
    const { retention } = await archive.archivePage(root);
    assert.deepEqual(retention, { limit_bytes: 8 * MiB, oldest_timestamp: null, used_bytes: 8 * MiB,
        legacy_used_bytes: 4 * MiB });
});

test('context bounds use measured Windows and estimated Wemos times and ignore malformed old rows', async t => {
    const root = profile(t);
    const lower = FIXED_INT - 30_000;
    const upper = FIXED_INT + 30_000;
    const at = value => new Date(value).toISOString();
    const file = writeRows(root, 'luxedge.jsonl.5', [
        row(1, 'pc', { timestamp: at(lower - 1) }),
        row(2, 'wemos', { timestamp: at(upper + 1000), details: { event_timestamp_estimated: at(lower) } }),
        row(3, 'windows', { timestamp: at(lower - 1000), details: { event_at: at(upper) } }),
        row(4, 'pc', { timestamp: at(upper + 1) }),
    ]);
    fs.appendFileSync(file, '{malformed tail');
    const data = await archive.archivePage(root, { from: lower, to: upper });
    assert.deepEqual(data.logs.map(item => item.id), [2, 3]);
    await assert.rejects(archive.archivePage(root, { before: 'forged' }), /sayfası/);
    await assert.rejects(archive.archivePage(root, { from: upper, to: lower }), /zaman aralığı/);
});
