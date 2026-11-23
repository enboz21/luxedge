'use strict';

const fs = require('fs');
const path = require('path');
const { randomBytes } = require('crypto');

const MIB = 1024 * 1024;
const SEGMENT_BYTES = 2 * MIB;
const DEFAULT_RETENTION_BYTES = 512 * MIB;
const MIN_RETENTION_BYTES = 8 * MIB;
const MAX_RETENTION_BYTES = 4096 * MIB;
const RETENTION_FILE = 'luxedge-log-retention.json';
const MANAGED_NAME = /^(luxedge(?:-electron)?\.jsonl)(?:\.[1-5]|\.seg-(\d{1,16})-(\d{1,16})-[a-f0-9]{16})?$/;
const PLAIN_LEGACY_NAME = /^luxedge\.log(?:\.[1-5])?$/;
const SOURCES = new Set(['pc', 'wemos', 'electron', 'windows']);
const LEVELS = new Set(['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']);
const MAX_LINE = 8192;
const EMPTY_LINE = Buffer.alloc(0);

class ArchiveChanged extends Error {}

function validLimit(value) {
    return Number.isSafeInteger(value) && value >= MIN_RETENTION_BYTES && value <= MAX_RETENTION_BYTES && value % MIB === 0;
}

function retentionLimit(directory) {
    let descriptor;
    try {
        const file = path.join(directory, RETENTION_FILE);
        if (!fs.lstatSync(file).isFile()) return DEFAULT_RETENTION_BYTES;
        descriptor = fs.openSync(file, 'r');
        const bytes = Buffer.alloc(256);
        const length = fs.readSync(descriptor, bytes, 0, bytes.length, 0);
        const value = JSON.parse(bytes.toString('utf8', 0, length)).limit_bytes;
        return validLimit(value) ? value : DEFAULT_RETENTION_BYTES;
    } catch {
        return DEFAULT_RETENTION_BYTES;
    } finally {
        if (descriptor !== undefined) fs.closeSync(descriptor);
    }
}

function managedFile(directory, name) {
    const match = MANAGED_NAME.exec(name);
    if (!match && !PLAIN_LEGACY_NAME.test(name)) return null;
    const low = Number(match?.[2]);
    const high = Number(match?.[3]);
    const bounded = Number.isSafeInteger(low) && Number.isSafeInteger(high) && low > 0 && high >= low;
    return { name, file: path.join(directory, name), legacy: !match, active: name === match?.[1],
        writer: match?.[1] === 'luxedge-electron.jsonl' ? 1 : 0, low: bounded ? low : 0,
        high: bounded ? high : Number.POSITIVE_INFINITY };
}

async function inventory(directory) {
    let names;
    try {
        names = await fs.promises.readdir(directory);
    } catch (error) {
        if (error.code === 'ENOENT') return [];
        throw error;
    }
    const entries = names.map(name => managedFile(directory, name)).filter(Boolean);
    const inspected = await Promise.all(entries.map(async entry => {
        try {
            const stat = await fs.promises.lstat(entry.file);
            return stat.isFile() ? { ...entry, stat } : null;
        } catch (error) {
            if (error.code === 'ENOENT') return null;
            throw error;
        }
    }));
    return inspected.filter(Boolean);
}

function archiveEnd(entry) {
    return Number.isFinite(entry.high) ? entry.high : Math.trunc(entry.stat.mtimeMs * 1000);
}

function sortedElectronFiles(directory) {
    try {
        return fs.readdirSync(directory).map(name => managedFile(directory, name)).filter(entry =>
            entry && !entry.legacy && entry.writer === 1).flatMap(entry => {
            try {
                const stat = fs.lstatSync(entry.file);
                return stat.isFile() ? [{ ...entry, stat }] : [];
            } catch (error) {
                if (error.code !== 'ENOENT') throw error;
                return [];
            }
        }).sort((a, b) => Number(b.active) - Number(a.active) || archiveEnd(b) - archiveEnd(a))
            .map(entry => entry.file);
    } catch (error) {
        if (error.code === 'ENOENT') return [];
        throw error;
    }
}

function rowOrder(row) {
    if (Number.isSafeInteger(row?.archive_order_us) && row.archive_order_us > 0) return row.archive_order_us;
    return Date.parse(row?.timestamp) * 1000;
}

function rotateElectronLog(file, size) {
    let min = 0;
    let max = 0;
    if (size <= SEGMENT_BYTES) {
        const keys = fs.readFileSync(file, 'utf8').split('\n').flatMap(line => {
            try {
                const value = rowOrder(JSON.parse(line));
                return Number.isSafeInteger(value) && value > 0 ? [value] : [];
            } catch {
                return [];
            }
        });
        if (keys.length) {
            min = keys.reduce((a, b) => Math.min(a, b));
            max = keys.reduce((a, b) => Math.max(a, b));
        }
    }
    const suffix = randomBytes(8).toString('hex');
    fs.renameSync(file, `${file}.seg-${min}-${max}-${suffix}`);
}

async function prune(directory) {
    const limit = retentionLimit(directory);
    const budget = limit - 2 * SEGMENT_BYTES;
    const archives = (await inventory(directory)).filter(entry => !entry.legacy && !entry.active)
        .sort((a, b) => archiveEnd(a) - archiveEnd(b) || (a.name < b.name ? -1 : 1));
    let total = archives.reduce((sum, entry) => sum + entry.stat.size, 0);
    for (const entry of archives) {
        if (total <= budget || retentionLimit(directory) !== limit) break;
        try {
            await fs.promises.unlink(entry.file);
        } catch (error) {
            if (error.code !== 'ENOENT') throw error;
        }
        total -= entry.stat.size;
    }
}

async function setRetention(directory, limit) {
    if (!validLimit(limit)) throw new RangeError(`Günlük sınırı ${MIN_RETENTION_BYTES / MIB}–${MAX_RETENTION_BYTES / MIB} MiB arasında tam sayı olmalı.`);
    await fs.promises.mkdir(directory, { recursive: true });
    const target = path.join(directory, RETENTION_FILE);
    const temporary = `${target}.${randomBytes(8).toString('hex')}.tmp`;
    let created = false;
    try {
        const handle = await fs.promises.open(temporary, 'wx');
        created = true;
        try { await handle.writeFile(JSON.stringify({ limit_bytes: limit }) + '\n'); }
        finally { await handle.close(); }
        await fs.promises.rename(temporary, target);
    } finally {
        if (created) await fs.promises.unlink(temporary).catch(error => { if (error.code !== 'ENOENT') throw error; });
    }
    await prune(directory);
}

function rowKey(row, writer) {
    const order = rowOrder(row);
    if (!Number.isSafeInteger(order) || order < 1 || !Number.isFinite(Date.parse(row?.timestamp)) ||
            typeof row?.msg !== 'string' || !SOURCES.has(row.source) || (row.source === 'electron') !== (writer === 1)) return null;
    const id = Number.isSafeInteger(row.id) && row.id > 0 ? String(row.id).padStart(16, '0') : row.id;
    if (typeof id !== 'string' || !id || id.length > 128) return null;
    return [order, writer, id];
}

function compare(a, b) {
    return a[0] - b[0] || a[1] - b[1] || (a[2] < b[2] ? -1 : Number(a[2] > b[2]));
}

function readCursor(before) {
    if (before == null) return null;
    if (typeof before !== 'string' || before.length > 512 || !/^[\w-]+$/.test(before)) throw new RangeError('Geçersiz günlük sayfası.');
    try {
        const [version, key, ceiling] = JSON.parse(Buffer.from(before, 'base64url').toString());
        if (version !== 1 || key.length !== 3 || !Number.isSafeInteger(key[0]) || key[0] < 1 ||
            ![0, 1].includes(key[1]) || typeof key[2] !== 'string' || key[2].length > 128 ||
            ceiling.length !== 2 || ceiling.some(value => !Number.isSafeInteger(value) || value < 0)) throw new Error();
        return { key, ceiling };
    } catch {
        throw new RangeError('Geçersiz günlük sayfası.');
    }
}

function identity(stat) {
    return `${stat.dev}/${stat.ino}/${stat.birthtimeMs}`;
}

async function readRows(entry, accept) {
    let handle;
    try {
        handle = await fs.promises.open(entry.file, 'r');
        const current = await handle.stat();
        if (identity(current) !== identity(entry.stat) || current.size < entry.stat.size) throw new ArchiveChanged();
        if (!entry.stat.size) return;
        const buffer = Buffer.allocUnsafe(64 * 1024);
        let pending = EMPTY_LINE;
        let skipping = false;
        const use = line => {
            try {
                const row = JSON.parse(line.toString('utf8'));
                const key = rowKey(row, entry.writer);
                if (key) accept(row, key);
            } catch (error) {
                if (!(error instanceof SyntaxError)) throw error;
            }
        };
        for (let position = 0; position < entry.stat.size;) {
            const { bytesRead } = await handle.read(buffer, 0, Math.min(buffer.length, entry.stat.size - position), position);
            if (!bytesRead) throw new ArchiveChanged();
            position += bytesRead;
            const chunk = buffer.subarray(0, bytesRead);
            let offset = 0;
            while (offset < chunk.length) {
                const newline = chunk.indexOf(10, offset);
                const end = newline === -1 ? chunk.length : newline;
                const part = chunk.subarray(offset, end);
                if (!skipping && pending.length + part.length <= MAX_LINE) {
                    const line = pending.length ? Buffer.concat([pending, part]) : part;
                    if (newline === -1) pending = Buffer.from(line);
                    else use(line);
                } else {
                    skipping = true;
                }
                if (newline !== -1) {
                    pending = EMPTY_LINE;
                    skipping = false;
                }
                offset = end + 1;
            }
        }
    } catch (error) {
        if (error.code === 'ENOENT' || error.code === 'EBUSY') throw new ArchiveChanged();
        throw error;
    } finally {
        if (handle) await handle.close();
    }
}

function manifestSignature(entries) {
    return entries.filter(entry => !entry.legacy).map(entry => `${entry.name}/${identity(entry.stat)}`).sort().join('\n');
}

function eventTime(row) {
    let measured;
    if (row.source === 'wemos') measured = row.details?.event_timestamp_estimated;
    if (row.source === 'windows') measured = row.details?.event_at;
    const received = row.source === 'wemos' ? row.details?.received_at : undefined;
    for (const timestamp of [measured, received, row.timestamp]) {
        const milliseconds = Date.parse(timestamp);
        if (Number.isFinite(milliseconds)) return milliseconds;
    }
}

async function pageFromManifest(directory, entries, query) {
    const { cursor, limit, source, level, search, from, to, writer, minOrder, maxOrder } = query;
    const logs = entries.filter(entry => !entry.legacy);
    const unknown = logs.filter(entry => !Number.isFinite(entry.high));
    const ordered = logs.filter(entry => Number.isFinite(entry.high)).sort((a, b) => b.high - a.high);
    const ceiling = cursor ? cursor.ceiling : [0, 0];
    if (!cursor) for (const entry of ordered) ceiling[entry.writer] = Math.max(ceiling[entry.writer], entry.high);
    let oldest = null;
    let selected = [];
    const updateOldest = (row, key) => { if (!oldest || compare(key, oldest.key) < 0) oldest = { row, key }; };
    const forPage = (row, key, candidates) => {
        if (cursor && (key[0] > ceiling[key[1]] || compare(key, cursor.key) >= 0)) return;
        if ((writer !== null && key[1] !== writer) || key[0] < minOrder || key[0] > maxOrder) return;
        if ((source && row.source !== source) || (level && row.level !== level)) return;
        if (from !== -Infinity || to !== Infinity) {
            const time = eventTime(row);
            if (time < from || time > to) return;
        }
        if (search && ![row.msg, row.code, row.device_name, row.explanation].some(value =>
            typeof value === 'string' && value.toLocaleLowerCase('tr-TR').includes(search))) return;
        candidates.push({ row, key });
        if (candidates.length >= (limit + 1) * 4) {
            candidates.sort((a, b) => compare(b.key, a.key)).length = limit + 1;
        }
    };
    const take = async entry => {
        const candidates = [];
        await readRows(entry, (row, key) => {
            if (!cursor) ceiling[key[1]] = Math.max(ceiling[key[1]], key[0]);
            updateOldest(row, key);
            forPage(row, key, candidates);
        });
        const unique = new Map();
        for (const candidate of selected.concat(candidates)) unique.set(JSON.stringify(candidate.key), candidate);
        selected = [...unique.values()].sort((a, b) => compare(b.key, a.key)).slice(0, limit + 1);
    };
    for (const entry of unknown) await take(entry);
    const oldestKnown = ordered.reduce((best, entry) => !best || entry.low < best.low ? entry : best, null);
    if (oldestKnown && (!oldest || oldestKnown.low <= oldest.key[0])) await readRows(oldestKnown, updateOldest);
    for (const entry of ordered) {
        if (cursor && (entry.low > ceiling[entry.writer] || entry.low > cursor.key[0])) continue;
        if ((writer !== null && entry.writer !== writer) || entry.high < minOrder || entry.low > maxOrder) continue;
        if (selected.length > limit && entry.high < selected[limit].key[0]) break;
        await take(entry);
    }
    const hasMore = selected.length > limit;
    const page = selected.slice(0, limit);
    const next = hasMore ? Buffer.from(JSON.stringify([1, page.at(-1).key, ceiling])).toString('base64url') : null;
    return {
        logs: page.map(item => item.row), next_cursor: next, has_more: hasMore, available: true,
        retention: {
            limit_bytes: retentionLimit(directory), used_bytes: logs.reduce((sum, entry) => sum + entry.stat.size, 0),
            oldest_timestamp: oldest?.row.timestamp || null,
            legacy_used_bytes: entries.filter(entry => entry.legacy).reduce((sum, entry) => sum + entry.stat.size, 0),
        },
    };
}

async function archivePage(directory, options = {}) {
    const query = {
        limit: Number.isInteger(options.limit) ? Math.max(1, Math.min(200, options.limit)) : 100,
        cursor: readCursor(options.before), source: SOURCES.has(options.source) ? options.source : null,
        level: LEVELS.has(options.level) ? options.level : null,
        search: typeof options.search === 'string' ? options.search.slice(0, 128).toLocaleLowerCase('tr-TR') : '',
        from: Number.isFinite(options.from) ? options.from : -Infinity,
        to: Number.isFinite(options.to) ? options.to : Infinity,
        writer: options.writer === 0 || options.writer === 1 ? options.writer : null,
        minOrder: Number.isSafeInteger(options.minOrder) ? options.minOrder : 0,
        maxOrder: Number.isSafeInteger(options.maxOrder) ? options.maxOrder : Number.MAX_SAFE_INTEGER,
    };
    if (query.from > query.to) throw new RangeError('Geçersiz günlük zaman aralığı.');
    for (let retry = 0; retry < 3; retry++) {
        const entries = await inventory(directory);
        try {
            const result = await pageFromManifest(directory, entries, query);
            if (manifestSignature(await inventory(directory)) === manifestSignature(entries)) return result;
        } catch (error) {
            if (!(error instanceof ArchiveChanged)) throw error;
        }
    }
    throw new ArchiveChanged('Günlük dosyaları yenileniyor. Tekrar deneyin.');
}

module.exports = { archivePage, prune, retentionLimit, rotateElectronLog, rowOrder, setRetention, sortedElectronFiles,
    DEFAULT_RETENTION_BYTES, MIN_RETENTION_BYTES, MAX_RETENTION_BYTES, SEGMENT_BYTES };
