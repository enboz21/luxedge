'use strict';

const fs = require('fs');
const path = require('path');
const { archivePage, rowOrder } = require('./log_archive');
const logText = require('./web_ui/log_format');

const BATCH_INTERVAL_MS = 400;
const ELECTRON_PENDING_LIMIT = 8192;
const MAX_JOURNAL_PAGES = 32;
const nowUs = () => Date.now() * 1000;

function timestampName() {
    return new Date().toISOString().slice(0, 23).replace(/[T:.]/g, '-');
}

async function chooseTxt(dialog, window, desktop, name) {
    const result = await dialog.showSaveDialog(window, {
        title: 'Günlüğü kaydet', defaultPath: path.join(desktop, `${name}-${timestampName()}.txt`),
        filters: [{ name: 'Metin dosyası', extensions: ['txt'] }],
        properties: ['showOverwriteConfirmation'],
    });
    if (result.canceled || !result.filePath) return null;
    return /\.txt$/i.test(result.filePath) ? result.filePath : result.filePath + '.txt';
}

async function writeUtf8(handle, text) {
    const contents = Buffer.from(text, 'utf8');
    for (let offset = 0; offset < contents.length;) {
        const { bytesWritten } = await handle.write(contents, offset, contents.length - offset);
        if (!bytesWritten) throw new Error('Günlük dosyasına yazılamadı.');
        offset += bytesWritten;
    }
}

function fileError(error) {
    if (error.code === 'EEXIST') return 'Bu adla bir dosya var. Farklı bir dosya adı seçin.';
    if (error.code === 'ENOSPC') return 'Seçilen sürücüde boş alan kalmadı.';
    return `Günlük dosyası hatası: ${error.message}`;
}

async function archivedBackendRows(directory, since, through, after) {
    const found = [];
    let before;
    do {
        const data = await archivePage(directory, { before, limit: 200, writer: 0, minOrder: since, maxOrder: through });
        found.push(...data.logs.filter(row => typeof row.id === 'number' && row.id > after));
        before = data.next_cursor;
    } while (before);
    return found;
}

function compareReceipt(a, b) {
    const delta = rowOrder(a) - rowOrder(b);
    if (delta) return delta;
    if (typeof a.id === 'number' && typeof b.id === 'number') return a.id - b.id;
    return String(a.id) < String(b.id) ? -1 : Number(String(a.id) > String(b.id));
}

class JournalCatchup {
    constructor() { this.ranges = []; }

    has(id) {
        let start = 0;
        let end = this.ranges.length;
        while (start < end) {
            const index = Math.floor((start + end) / 2);
            const [first, last] = this.ranges[index];
            if (id < first) end = index;
            else if (id > last) start = index + 1;
            else return true;
        }
        return false;
    }

    add(id) {
        const index = this.ranges.findIndex(([, last]) => last >= id - 1);
        if (index < 0) {
            this.ranges.push([id, id]);
            return;
        }
        const range = this.ranges[index];
        if (id < range[0] - 1) {
            this.ranges.splice(index, 0, [id, id]);
            return;
        }
        range[0] = Math.min(range[0], id);
        range[1] = Math.max(range[1], id);
        const next = this.ranges[index + 1];
        if (next && range[1] + 1 >= next[0]) {
            range[1] = next[1];
            this.ranges.splice(index + 1, 1);
        }
    }

    forgetThrough(id) {
        const index = this.ranges.findIndex(([, last]) => last > id);
        this.ranges = index < 0 ? [] : this.ranges.slice(index);
        if (this.ranges.length) this.ranges[0][0] = Math.max(this.ranges[0][0], id + 1);
    }
}

class LiveLogFile {
    static async start(file, directory, readJournal) {
        const handle = await fs.promises.open(file, 'wx');
        const session = new LiveLogFile(file, handle, directory, readJournal);
        try {
            await writeUtf8(handle, `LuxEdge canlı günlük · ${new Date().toISOString()}\n\n`);
            session.schedule(0);
            return session;
        } catch (error) {
            await handle.close().catch(() => {});
            throw error;
        }
    }

    constructor(file, handle, directory, readJournal) {
        this.file = file;
        this.handle = handle;
        this.directory = directory;
        this.readJournal = readJournal;
        this.since = nowUs();
        this.lastJournalId = 0;
        this.diskAhead = new JournalCatchup();
        this.lastBackendId = 0;
        this.lastBackendOrder = this.since - 1;
        this.generation = null;
        this.electron = [];
        this.droppedElectron = 0;
        this.written = 0;
        this.state = 'writing';
        this.message = '';
        this.hadGap = false;
        this.lostElectron = 0;
        this.timer = null;
        this.poll = null;
        this.stopping = null;
        this.nextFallback = 0;
    }

    status() {
        return { state: this.state, path: this.file, written: this.written, message: this.message };
    }

    addElectron(row) {
        if (this.state !== 'writing' || this.stopping) return;
        if (this.electron.length >= ELECTRON_PENDING_LIMIT) {
            this.droppedElectron++;
            return;
        }
        this.electron.push(row);
    }

    schedule(delay) {
        this.timer = setTimeout(() => {
            if (this.state !== 'writing' || this.stopping) return;
            this.poll = this.collect(nowUs() + 999).catch(error => this.fail(error)).finally(() => {
                this.poll = null;
                if (this.state === 'writing' && !this.stopping) this.schedule(BATCH_INTERVAL_MS);
            });
        }, delay);
        this.timer.unref?.();
    }

    async backendRows(through, final) {
        const records = new Map();
        let after = this.lastJournalId;
        let fallback = false;
        let gap = false;
        let available = false;
        for (let index = 0; index < MAX_JOURNAL_PAGES; index++) {
            let data;
            try { data = await this.readJournal(this.since, through, after); }
            catch { data = null; }
            if (!data || !Array.isArray(data.logs) || typeof data.generation !== 'string') {
                fallback = true;
                break;
            }
            available = true;
            fallback ||= data.overflow || (this.generation && this.generation !== data.generation);
            gap ||= Boolean(data.overflow);
            this.generation = data.generation;
            const previous = after;
            for (const row of data.logs) {
                if (Number.isSafeInteger(row.id) && row.id > previous) {
                    if (!this.diskAhead.has(row.id)) records.set(row.id, row);
                    after = Math.max(after, row.id);
                }
            }
            if (!data.has_more) break;
            if (after === previous || index + 1 === MAX_JOURNAL_PAGES) {
                fallback = gap = true;
                break;
            }
        }
        if (fallback && (available || final || Date.now() >= this.nextFallback)) {
            try {
                const from = gap ? this.since : this.lastBackendOrder + 1;
                const afterArchive = gap ? this.lastJournalId : this.lastBackendId;
                const archived = await archivedBackendRows(this.directory, from, through, afterArchive);
                for (const row of archived) {
                    if (!records.has(row.id) && !this.diskAhead.has(row.id)) records.set(row.id, row);
                }
            } catch {
                gap = true;
            }
            this.nextFallback = Date.now() + 1500;
        }
        return { rows: [...records.values()], available, gap, journalThrough: after };
    }

    async collect(through, final = false) {
        const { rows, available, gap, journalThrough } = await this.backendRows(through, final);
        const next = [];
        for (const row of this.electron) {
            if (rowOrder(row) <= through || final) rows.push(row);
            else next.push(row);
        }
        this.electron = next;
        rows.sort(compareReceipt);
        const bufferNotice = this.droppedElectron ? `Uygulama tamponu doldu: ${this.droppedElectron} kayıt alınamadı.` : '';
        const gapText = 'PC canlı tamponunda boşluk görüldü. Diskteki kayıtlar kontrol edildi.';
        const backendNotice = gap && !this.hadGap ? gapText : '';
        this.hadGap ||= gap;
        this.lostElectron += this.droppedElectron;
        this.message = [this.hadGap ? gapText : '',
            this.lostElectron ? `Uygulama tamponunda ${this.lostElectron} kayıt kaybedildi.` : '',
            available ? '' : 'PC canlı akışı kullanılamıyor. Diskteki kayıtlar kontrol ediliyor.'].filter(Boolean).join(' ');
        if (rows.length || bufferNotice || backendNotice) {
            const formatted = rows.map(row => logText.formatEntry(row));
            for (const notice of [backendNotice, bufferNotice]) {
                if (notice) formatted.push(`[${new Date().toISOString()}] ${notice}`);
            }
            await writeUtf8(this.handle, formatted.join('\n\n') + '\n\n');
            this.written += rows.length;
            this.droppedElectron = 0;
            const diskIds = [];
            for (const row of rows) {
                if (typeof row.id === 'number' && row.source !== 'electron') {
                    this.lastBackendId = Math.max(this.lastBackendId, row.id);
                    this.lastBackendOrder = Math.max(this.lastBackendOrder, rowOrder(row));
                    if (row.id > journalThrough) diskIds.push(row.id);
                }
            }
            for (const id of diskIds.sort((a, b) => a - b)) this.diskAhead.add(id);
        }
        this.lastJournalId = journalThrough;
        this.diskAhead.forgetThrough(journalThrough);
    }

    async fail(error) {
        this.state = 'error';
        this.message = fileError(error);
        await this.handle.close().catch(() => {});
    }

    stop() {
        if (this.stopping) return this.stopping;
        if (this.state !== 'writing') return Promise.resolve(this.poll).then(() => this.status());
        clearTimeout(this.timer);
        const through = nowUs() + 999;
        this.stopping = (async () => {
            await this.poll;
            if (this.state === 'writing') {
                try {
                    await this.collect(through, true);
                    await this.handle.sync();
                    await this.handle.close();
                    this.state = 'stopped';
                } catch (error) { await this.fail(error); }
            }
            return this.status();
        })();
        return this.stopping;
    }
}

async function writeIncident(file, directory, from, to, selectedKey, recent = []) {
    const records = new Map();
    let before;
    do {
        const data = await archivePage(directory, { before, from, to, limit: 200 });
        for (const row of data.logs) records.set(`${row.source}:${row.id}`, row);
        before = data.next_cursor;
    } while (before);
    for (const row of recent) {
        const time = logText.eventTime(row);
        const key = `${row.source}:${row.id}`;
        if (time !== null && time >= from && time <= to && !records.has(key)) records.set(key, row);
    }
    const rows = [...records.values()].sort((a, b) => logText.eventTime(a) - logText.eventTime(b) || compareReceipt(a, b));
    const handle = await fs.promises.open(file, 'wx');
    try {
        const header = `LuxEdge olay bağlamı · ${new Date(from).toISOString()} — ${new Date(to).toISOString()}\n`;
        await writeUtf8(handle, header + (selectedKey ? `Seçilen kayıt · ${selectedKey.replace(/[\r\n]/g, ' ')}\n` : '') + '\n');
        for (let index = 0; index < rows.length; index += 200) {
            const batch = rows.slice(index, index + 200).map(row => logText.formatEntry(row));
            await writeUtf8(handle, batch.join('\n\n') + '\n\n');
        }
        await handle.sync();
        return { state: 'saved', path: file, written: rows.length };
    } finally {
        await handle.close();
    }
}

module.exports = { chooseTxt, fileError, LiveLogFile, writeIncident };
