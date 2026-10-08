const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { EventEmitter } = require('node:events');
const { createPoller } = require('../web_ui/polling');

function fixture(owns = true) {
    const app = new EventEmitter();
    const calls = [];
    let readyCalls = 0;
    Object.assign(app, {
        requestSingleInstanceLock: () => owns,
        whenReady: () => { readyCalls++; return new Promise(() => {}); },
        quit: () => calls.push('quit'),
        exit: () => calls.push('exit'),
        relaunch: () => calls.push('relaunch'),
    });
    const timers = new Map();
    const spawned = [];
    const processMock = new EventEmitter();
    processMock.env = {};
    const context = vm.createContext({
        require(name) {
            if (name === 'electron') return { app };
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
            return require(name);
        },
        console: { log() {}, error() {}, warn() {} }, process: processMock,
        Buffer, __dirname: path.resolve(__dirname, '..'),
        setTimeout(fn, delay) { const id = timers.size + 1; timers.set(id, { fn, delay }); return id; },
        clearTimeout(id) { timers.delete(id); },
    });
    // Main imports HTTP, but no request can escape this inert replacement.
    const baseRequire = context.require;
    context.require = name => name === 'http' ? new Proxy({}, { get() { throw new Error('Network forbidden'); } }) : baseRequire(name);
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../main.js'), 'utf8'), context);
    return { app, context, calls, spawned, timers, readyCalls };
}

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
    const preload = fs.readFileSync(path.join(__dirname, '../preload.js'), 'utf8');
    assert.match(preload, /exposeInMainWorld\('luxedge'/);
    assert.match(preload, /onWindowVisibility/);
    assert.match(html, /window\.luxedge\.onWindowVisibility/);
    for (const match of html.matchAll(/<script>([\s\S]*?)<\/script>/g)) new vm.Script(match[1]);
});
