// LuxEdge kontrol paneli - renderer mantığı
// Sayfalar: Genel Bakış, Ambilight, Cihazlar, Wemos, Günlük, Sistem

const api = window.luxedge;
let electronVisible = true;
const poller = createPoller(() => !document.hidden && electronVisible);
const fetchStatus = poller.add('status', fetchStatusData, 1500);
const fetchLogs = poller.add('logs', fetchLogsData, 2000);

// Firmware sınırı: wemos_code.ino içindeki MAX_LED_COUNT ile aynı olmalı
const MAX_LED_COUNT = 170;

const STRIP_MODES = ['stream', 'off', 'solid', 'breathe', 'rainbow', 'chase', 'twinkle'];
const STRIP_MODE_LABELS = {
    stream: 'Ekran akışı',
    off: 'Kapalı',
    solid: 'Sabit renk',
    breathe: 'Nefes alma',
    rainbow: 'Gökkuşağı',
    chase: 'Kovalama',
    twinkle: 'Parıltı',
};
const EDGE_LABELS = { top: 'Üst kenar', bottom: 'Alt kenar', left: 'Sol kenar', right: 'Sağ kenar' };
const ROLE_LABELS = { ambilight: 'Ambilight', strips: 'Şerit bazlı' };
const LEGACY_ID = 'legacy-primary';

const PAGES = {
    overview: ['Genel Bakış', 'Bağlantı, performans ve ekran düzeni'],
    ambilight: ['Ambilight', 'Ana Wemos için ekran kenarı LED ayarları'],
    devices: ['Cihazlar', 'Birden fazla Wemos ve şerit bazlı görevler'],
    wemos: ['Wemos', 'Bağlantı, cihaz işlemleri ve firmware'],
    logs: ['Günlük', 'Arka plan hizmetinin son kayıtları'],
    system: ['Sistem', 'Başlangıç ve uygulama bilgileri'],
};

// --- DOM ---
const els = {
    statusDot: document.getElementById('status-dot'),
    statusText: document.getElementById('status-text'),
    connStatus: document.getElementById('conn-status'),
    wemosIp: document.getElementById('wemos-ip'),
    wemosPort: document.getElementById('wemos-port'),
    localIp: document.getElementById('local-ip'),
    fpsVal: document.getElementById('fps-val'),
    fpsRing: document.getElementById('fps-ring'),
    packetsSent: document.getElementById('packets-sent'),
    errorCount: document.getElementById('error-count'),
    totalLeds: document.getElementById('total-leds'),
    ambTotalLeds: document.getElementById('amb-total-leds'),

    topLeds: document.getElementById('top_leds'),
    bottomLeds: document.getElementById('bottom_leds'),
    leftLeds: document.getElementById('left_leds'),
    rightLeds: document.getElementById('right_leds'),
    edgeWidth: document.getElementById('edge_width'),
    edgeOffset: document.getElementById('edge_offset'),
    manualIpInput: document.getElementById('manual-ip-input'),
    ledMonitorIndex: document.getElementById('led_monitor_index'),

    idleMode: document.getElementById('idle_mode'),
    idleUseWindowsColor: document.getElementById('idle_use_windows_color'),
    idleColor: document.getElementById('idle_color'),
    idleBrightness: document.getElementById('idle_brightness'),
    idleBrightnessVal: document.getElementById('idle_brightness_val'),
    fullscreenMaxBrightness: document.getElementById('fullscreen_max_brightness'),
    fullscreenMaxBrightnessVal: document.getElementById('fullscreen_max_brightness_val'),
    targetFps: document.getElementById('target_fps'),
    targetFpsVal: document.getElementById('target_fps_val'),
    autostartToggle: document.getElementById('autostart_toggle'),
};

// --- Sayfa gezinme ---
let currentPage = 'overview';

function showPage(name) {
    if (!PAGES[name]) return;
    currentPage = name;
    document.querySelectorAll('.nav-item').forEach(item => item.classList.toggle('active', item.dataset.page === name));
    document.querySelectorAll('.page').forEach(page => page.classList.toggle('active', page.dataset.page === name));
    document.getElementById('page-title').textContent = PAGES[name][0];
    document.getElementById('page-subtitle').textContent = PAGES[name][1];
    if (name === 'logs') fetchLogs();
    if (name === 'devices') fetchDevices();
}

document.querySelectorAll('.nav-item').forEach(item => item.addEventListener('click', () => showPage(item.dataset.page)));
document.querySelectorAll('[data-goto]').forEach(button => button.addEventListener('click', () => showPage(button.dataset.goto)));

// --- Bildirim ve onay ---
function showToast(message, type = 'info') {
    const container = document.getElementById('toast-container');
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(() => {
        toast.style.opacity = '0';
        setTimeout(() => toast.remove(), 300);
    }, 3200);
}

function confirmDialog(title, text, { confirmLabel = 'Onayla', danger = false } = {}) {
    const dialog = document.getElementById('dialog');
    const confirmButton = document.getElementById('dialog-confirm');
    const cancelButton = document.getElementById('dialog-cancel');
    document.getElementById('dialog-title').textContent = title;
    document.getElementById('dialog-text').textContent = text;
    confirmButton.textContent = confirmLabel;
    confirmButton.className = danger ? 'btn btn-danger' : 'btn btn-primary';
    dialog.classList.remove('hidden');
    return new Promise(resolve => {
        const finish = value => {
            dialog.classList.add('hidden');
            confirmButton.onclick = null;
            cancelButton.onclick = null;
            resolve(value);
        };
        confirmButton.onclick = () => finish(true);
        cancelButton.onclick = () => finish(false);
    });
}

// --- Ambilight ayarları: otomatik kayıt ---
let autoSaveTimer = null;
let isEditingMode = false;
let editModeTimer = null;

function handleInputEdit(isIpField = false) {
    isEditingMode = true;
    clearTimeout(editModeTimer);
    // Durum sorgusu eski değeri geri yazmasın diye kısa süre kilitle
    editModeTimer = setTimeout(() => { isEditingMode = false; }, 4000);

    clearTimeout(autoSaveTimer);
    const delay = isIpField ? 1500 : 600;
    autoSaveTimer = setTimeout(() => {
        if (isIpField) saveManualIp(true);
        else saveConfig(true);
    }, delay);
}

function updateEstimatedAmper() {
    const totalLeds = parseInt(els.totalLeds.textContent) || 0;
    const brightness = els.fullscreenMaxBrightness ? parseInt(els.fullscreenMaxBrightness.value) : 255;
    // WS2812B beyazda tam parlaklıkta yaklaşık 60 mA çeker
    const amper = (totalLeds * 0.06 * (brightness / 255.0)).toFixed(1);
    const target = document.getElementById('estimated_amper_val');
    if (target) target.textContent = `${amper} A`;
}

function bindAmbilightInputs() {
    if (els.idleBrightness) {
        els.idleBrightness.addEventListener('input', event => {
            els.idleBrightnessVal.textContent = event.target.value + '%';
        });
    }
    if (els.targetFps) {
        els.targetFps.addEventListener('input', event => {
            els.targetFpsVal.textContent = event.target.value + ' FPS';
        });
    }
    if (els.fullscreenMaxBrightness) {
        els.fullscreenMaxBrightness.addEventListener('input', event => {
            els.fullscreenMaxBrightnessVal.textContent = event.target.value;
            updateEstimatedAmper();
        });
    }

    const configInputs = [
        els.topLeds, els.bottomLeds, els.leftLeds, els.rightLeds,
        els.edgeWidth, els.edgeOffset, els.idleMode, els.idleUseWindowsColor, els.idleColor,
        els.idleBrightness, els.fullscreenMaxBrightness, els.targetFps, els.ledMonitorIndex,
    ];
    configInputs.forEach(input => {
        if (!input) return;
        input.addEventListener('input', () => handleInputEdit(false));
        input.addEventListener('change', () => handleInputEdit(false));
    });
    [els.topLeds, els.bottomLeds, els.leftLeds, els.rightLeds].forEach(input => {
        if (input) input.addEventListener('input', renderEdgePreviewFromInputs);
    });

    if (els.manualIpInput) {
        els.manualIpInput.addEventListener('input', () => handleInputEdit(true));
    }

    if (els.idleUseWindowsColor && els.idleColor) {
        els.idleUseWindowsColor.addEventListener('change', event => {
            els.idleColor.disabled = event.target.checked;
        });
    }

    if (els.autostartToggle && api.getAutoStart) {
        api.getAutoStart().then(result => { els.autostartToggle.checked = result.enabled; }).catch(() => { });
        els.autostartToggle.addEventListener('change', async event => {
            const enabled = event.target.checked;
            try {
                const result = await api.setAutoStart(enabled);
                if (result.success) showToast(enabled ? 'Otomatik başlatma açıldı' : 'Otomatik başlatma kapatıldı', 'success');
                else { showToast('Ayar kaydedilemedi: ' + (result.message || ''), 'error'); event.target.checked = !enabled; }
            } catch (error) {
                showToast('Ayar kaydedilemedi', 'error');
                event.target.checked = !enabled;
            }
        });
    }
}

function renderEdgePreview(top, bottom, left, right) {
    const map = { top, bottom, left, right };
    Object.entries(map).forEach(([edge, count]) => {
        const element = document.getElementById('pv-' + edge);
        if (!element) return;
        element.classList.toggle('on', count > 0);
        element.querySelector('span').textContent = count;
    });
}

function renderEdgePreviewFromInputs() {
    renderEdgePreview(
        parseInt(els.topLeds.value) || 0, parseInt(els.bottomLeds.value) || 0,
        parseInt(els.leftLeds.value) || 0, parseInt(els.rightLeds.value) || 0);
    const total = ['topLeds', 'bottomLeds', 'leftLeds', 'rightLeds'].reduce((sum, key) => sum + (parseInt(els[key].value) || 0), 0);
    if (els.ambTotalLeds) els.ambTotalLeds.textContent = total;
}

// --- Pencere düğmeleri ---
document.getElementById('btn-minimize').onclick = () => api.minimizeWindow();
document.getElementById('btn-maximize').onclick = () => api.maximizeWindow();
document.getElementById('btn-close').onclick = () => api.closeWindow();

// --- Monitör listesi ---
let availableMonitors = [];
let monitorListVersion = -1;

function applyMonitorSnapshot(status) {
    if (!Array.isArray(status.monitors) || status.monitor_list_version <= monitorListVersion) return;
    monitorListVersion = status.monitor_list_version;
    availableMonitors = status.monitors;
    const selected = els.ledMonitorIndex._loaded
        ? els.ledMonitorIndex.value : (status.led_monitor_index || 1);
    renderMonitorOptions(els.ledMonitorIndex, availableMonitors, selected, document);
    els.ledMonitorIndex._loaded = true;
    const deviceSelect = document.getElementById('device-monitor-index');
    renderMonitorOptions(deviceSelect, availableMonitors, deviceSelect.value, document);
    if (typeof refreshStripMonitorSelects === 'function') refreshStripMonitorSelects();
}

const refreshMonitors = createMonitorRefresh(api, applyMonitorSnapshot, busy => {
    document.querySelectorAll('.monitor-refresh').forEach(button => {
        button.disabled = busy;
        button.textContent = busy ? 'Güncelleniyor' : 'Güncelle';
    });
}, showToast);

function monitorLabel(index) {
    const monitor = availableMonitors.find(item => String(item.index) === String(index));
    if (!monitor) return `Monitör ${index}`;
    return `Monitör ${monitor.index}: ${monitor.name || ''} ${monitor.width}x${monitor.height}`.replace(/\s+/g, ' ').trim();
}

// --- Cihazlar ---
let deviceRecords = [];
let deviceValidations = new Map();
let multiDeviceEnabled = false;
let editorStrips = [];
let stripApplyTimer = null;

function deviceFormValue(id, fallback = '') {
    const element = document.getElementById(id);
    return element ? element.value : fallback;
}

function validationChip(validation, device) {
    if (!validation) {
        return device && device.enabled ? ['neutral', 'Doğrulanmadı'] : ['neutral', 'Pasif'];
    }
    if (!validation.reachable) return ['bad', 'Erişilemiyor'];
    if (validation.compatible) return ['ok', 'Doğrulandı'];
    return ['warn', 'Uyumsuz'];
}

function setChip(element, state, text) {
    if (!element) return;
    element.className = `chip ${state}`;
    element.textContent = text;
}

function deviceSummary(device) {
    const role = device.role || 'ambilight';
    if (role === 'strips') {
        const strips = device.strips || [];
        const total = strips.reduce((sum, strip) => sum + (Number(strip.led_count) || 0), 0);
        return `${strips.length} şerit · ${total} LED`;
    }
    const leds = device.leds || {};
    const total = ['top', 'bottom', 'left', 'right'].reduce((sum, edge) => sum + (Number(leds[edge]) || 0), 0);
    return `Monitör ${device.monitor_index || 1} · ${total} LED`;
}

function buildDeviceItem(device, { selectable }) {
    const item = document.createElement(selectable ? 'button' : 'div');
    if (selectable) item.type = 'button';
    item.className = 'item';
    if (selectable && device.id === deviceFormValue('device-id')) item.classList.add('selected');

    const body = document.createElement('div');
    body.className = 'item-body';
    const title = document.createElement('div');
    title.className = 'item-title';
    title.textContent = device.name || device.ip;
    const sub = document.createElement('div');
    sub.className = 'item-sub';
    sub.append(`${device.ip}:${device.port}`);
    const sep1 = document.createElement('span'); sep1.className = 'sep'; sep1.textContent = '/';
    sub.append(sep1, ROLE_LABELS[device.role] || ROLE_LABELS.ambilight);
    const sep2 = document.createElement('span'); sep2.className = 'sep'; sep2.textContent = '/';
    sub.append(sep2, deviceSummary(device));
    body.append(title, sub);

    const chip = document.createElement('span');
    const [state, text] = validationChip(deviceValidations.get(device.id), device);
    setChip(chip, state, text);
    if (!device.enabled && device.id !== LEGACY_ID) setChip(chip, 'neutral', 'Pasif');
    item.append(body, chip);
    if (selectable) item.addEventListener('click', () => setDeviceForm(device));
    return item;
}

function renderDevices() {
    const container = document.getElementById('device-list');
    const overview = document.getElementById('ov-device-list');
    const mode = document.getElementById('multi-device-mode');
    const toggle = document.getElementById('multi-device-toggle');
    const hint = document.getElementById('ov-multi-hint');
    if (mode) mode.textContent = multiDeviceEnabled ? 'Açık: doğrulanmış etkin cihazlara veri gönderiliyor' : 'Kapalı: yalnız Ana Wemos kullanılıyor';
    if (toggle) toggle.checked = multiDeviceEnabled;
    if (hint) hint.textContent = multiDeviceEnabled ? 'Çoklu cihaz modu açık.' : 'Çoklu cihaz modu kapalı; ek cihazlar veri almıyor.';

    [container, overview].forEach(target => {
        if (!target) return;
        target.replaceChildren();
        if (!deviceRecords.length) {
            const empty = document.createElement('div');
            empty.className = 'list-empty';
            empty.textContent = 'Kayıtlı cihaz yok.';
            target.appendChild(empty);
            return;
        }
        deviceRecords.forEach(device => target.appendChild(buildDeviceItem(device, { selectable: target === container })));
    });
}

async function fetchDevices() {
    try {
        const result = await api.getDevices();
        if (!result.success) throw new Error(result.message || 'Cihazlar yüklenemedi');
        deviceRecords = result.devices || [];
        multiDeviceEnabled = Boolean(result.multi_device_enabled);
        renderDevices();
    } catch (error) {
        const container = document.getElementById('device-list');
        if (container) container.textContent = 'Cihaz kayıtları yüklenemedi.';
    }
}

function setDeviceForm(device = null) {
    const isLegacy = Boolean(device && device.id === LEGACY_ID);
    const leds = device?.leds || {};
    const role = device?.role || 'ambilight';
    document.getElementById('device-id').value = device?.id || '';
    document.getElementById('device-name').value = device?.name || '';
    document.getElementById('device-ip').value = device?.ip || '';
    document.getElementById('device-port').value = device?.port || 7777;
    document.getElementById('device-role').value = role;
    document.getElementById('device-enabled').checked = device ? Boolean(device.enabled) : true;
    document.getElementById('device-monitor-index').value = device?.monitor_index || 1;
    document.getElementById('device-top-leds').value = leds.top || 0;
    document.getElementById('device-bottom-leds').value = leds.bottom || 0;
    document.getElementById('device-left-leds').value = leds.left || 0;
    document.getElementById('device-right-leds').value = leds.right || 0;
    document.getElementById('device-form-title').textContent = device ? (device.name || 'Cihaz') : 'Yeni cihaz';
    document.getElementById('delete-device-button').classList.toggle('hidden', !device || isLegacy);
    document.getElementById('apply-device-button').disabled = !device;

    // Ana Wemos'un adres ve kenar ayarları Ambilight sayfasından yönetilir.
    ['device-ip', 'device-port', 'device-monitor-index', 'device-top-leds', 'device-bottom-leds',
        'device-left-leds', 'device-right-leds', 'device-enabled'].forEach(id => {
            document.getElementById(id).disabled = isLegacy;
        });
    document.getElementById('device-editor-hint').textContent = isLegacy
        ? 'Ana Wemos: IP, monitör ve kenar LED sayıları Ambilight sayfasından değiştirilir. Görev ve şerit ayarları burada düzenlenebilir.'
        : (device ? '' : 'Cihazı kaydettikten sonra "Wemos\'tan şeritleri al" ile şerit sayısı ve pinleri çekebilirsiniz.');

    editorStrips = (device?.strips || []).map(strip => ({ ...strip }));
    const [state, text] = validationChip(deviceValidations.get(device?.id), device);
    setChip(document.getElementById('device-validation'), device ? state : 'neutral', device ? text : 'Kaydedilmedi');
    updateRoleFields();
    renderDevices();
}

function updateRoleFields() {
    const role = deviceFormValue('device-role', 'ambilight');
    document.getElementById('role-ambilight-fields').classList.toggle('hidden', role !== 'ambilight');
    document.getElementById('role-strips-fields').classList.toggle('hidden', role !== 'strips');
    document.getElementById('fetch-strips-button').disabled = !deviceFormValue('device-id');
    if (role === 'strips') renderStrips();
}

document.getElementById('device-role').addEventListener('change', updateRoleFields);

function stripField(labelText, control) {
    const field = document.createElement('div');
    field.className = 'field';
    const label = document.createElement('label');
    label.textContent = labelText;
    field.append(label, control);
    return field;
}

function buildStripRow(strip, position) {
    const row = document.createElement('div');
    row.className = 'strip';

    const head = document.createElement('div');
    head.className = 'strip-head';
    const swatch = document.createElement('span');
    swatch.className = 'strip-swatch';
    swatch.style.background = strip.color || '#ff7800';
    const title = document.createElement('span');
    title.className = 'strip-title';
    title.textContent = `Şerit ${strip.index}`;
    const meta = document.createElement('span');
    meta.className = 'strip-meta';
    meta.textContent = `${strip.pin || 'pin ?'} · ${strip.led_count || 0} LED`;
    head.append(swatch, title, meta);

    const grid = document.createElement('div');
    grid.className = 'strip-grid';

    const mode = document.createElement('select');
    mode.className = 'input';
    STRIP_MODES.forEach(value => {
        const option = document.createElement('option');
        option.value = value;
        option.textContent = STRIP_MODE_LABELS[value];
        mode.appendChild(option);
    });
    mode.value = strip.mode || 'stream';
    mode.addEventListener('change', () => {
        editorStrips[position].mode = mode.value;
        renderStrips();
        scheduleStripApply();
    });

    const color = document.createElement('input');
    color.type = 'color';
    color.className = 'color';
    color.value = strip.color || '#ff7800';
    color.addEventListener('input', () => {
        editorStrips[position].color = color.value;
        swatch.style.background = color.value;
        scheduleStripApply();
    });

    const brightness = document.createElement('input');
    brightness.type = 'range';
    brightness.min = 0; brightness.max = 255;
    brightness.value = strip.brightness ?? 128;
    const brightnessLabel = `Parlaklık`;
    brightness.addEventListener('input', () => {
        editorStrips[position].brightness = Number(brightness.value);
        brightnessField.querySelector('label').textContent = `${brightnessLabel} ${Math.round(brightness.value / 2.55)}%`;
        scheduleStripApply();
    });

    const speed = document.createElement('input');
    speed.type = 'range';
    speed.min = 1; speed.max = 100;
    speed.value = strip.speed ?? 50;
    speed.addEventListener('input', () => {
        editorStrips[position].speed = Number(speed.value);
        speedField.querySelector('label').textContent = `Hız ${speed.value}`;
        scheduleStripApply();
    });

    const isLocal = mode.value !== 'stream' && mode.value !== 'off';
    const animated = ['breathe', 'rainbow', 'chase', 'twinkle'].includes(mode.value);
    const brightnessField = stripField(`${brightnessLabel} ${Math.round(brightness.value / 2.55)}%`, brightness);
    const speedField = stripField(`Hız ${speed.value}`, speed);
    color.disabled = !isLocal || mode.value === 'rainbow';
    brightness.disabled = !isLocal;
    speed.disabled = !animated;

    grid.append(stripField('Mod', mode), stripField('Renk', color), brightnessField, speedField);
    row.append(head, grid);

    if (mode.value === 'stream') {
        const stream = document.createElement('div');
        stream.className = 'strip-stream';
        const monitor = document.createElement('select');
        monitor.className = 'input strip-monitor';
        renderMonitorOptions(monitor, availableMonitors, strip.monitor_index || 1, document);
        monitor.addEventListener('change', () => { editorStrips[position].monitor_index = Number(monitor.value); scheduleStripApply(); });
        const edge = document.createElement('select');
        edge.className = 'input';
        Object.entries(EDGE_LABELS).forEach(([value, label]) => {
            const option = document.createElement('option');
            option.value = value; option.textContent = label;
            edge.appendChild(option);
        });
        edge.value = strip.edge || 'top';
        edge.addEventListener('change', () => { editorStrips[position].edge = edge.value; scheduleStripApply(); });
        const reverseLabel = document.createElement('label');
        reverseLabel.className = 'check';
        const reverse = document.createElement('input');
        reverse.type = 'checkbox';
        reverse.checked = Boolean(strip.reverse);
        reverse.addEventListener('change', () => { editorStrips[position].reverse = reverse.checked; scheduleStripApply(); });
        reverseLabel.append(reverse, document.createTextNode('Yönü ters çevir'));
        stream.append(stripField('Monitör', monitor), stripField('Kenar', edge), reverseLabel);
        row.appendChild(stream);
    }
    return row;
}

function renderStrips() {
    const container = document.getElementById('strip-list');
    if (!container) return;
    container.replaceChildren();
    if (!editorStrips.length) {
        const empty = document.createElement('div');
        empty.className = 'list-empty';
        empty.textContent = deviceFormValue('device-id')
            ? 'Şerit bilgisi yok. "Wemos\'tan şeritleri al" ile cihazdaki şerit sayısını ve pinleri çekin.'
            : 'Önce cihazı kaydedin; ardından şeritler Wemos\'tan alınır.';
        container.appendChild(empty);
        return;
    }
    editorStrips.forEach((strip, position) => container.appendChild(buildStripRow(strip, position)));
}

function refreshStripMonitorSelects() {
    document.querySelectorAll('.strip-monitor').forEach(select => {
        renderMonitorOptions(select, availableMonitors, select.value, document);
    });
}

// Şerit ayarı değiştiğinde kısa gecikmeyle kaydet ve Wemos'a gönder.
function scheduleStripApply() {
    clearTimeout(stripApplyTimer);
    if (!deviceFormValue('device-id')) return;
    stripApplyTimer = setTimeout(async () => {
        const saved = await saveDevice(true);
        if (saved) await applyDevice(true);
    }, 500);
}

function getDevicePayload() {
    return {
        name: deviceFormValue('device-name').trim(),
        ip: deviceFormValue('device-ip').trim(),
        port: Number(deviceFormValue('device-port', 7777)),
        role: deviceFormValue('device-role', 'ambilight'),
        enabled: document.getElementById('device-enabled').checked,
        monitor_index: Number(deviceFormValue('device-monitor-index', 1)),
        leds: {
            top: Number(deviceFormValue('device-top-leds', 0)),
            bottom: Number(deviceFormValue('device-bottom-leds', 0)),
            left: Number(deviceFormValue('device-left-leds', 0)),
            right: Number(deviceFormValue('device-right-leds', 0)),
        },
        strips: editorStrips.map(strip => ({
            index: strip.index,
            led_count: Number(strip.led_count) || 0,
            pin: strip.pin || '',
            mode: strip.mode || 'stream',
            color: strip.color || '#ff7800',
            brightness: Number(strip.brightness ?? 128),
            speed: Number(strip.speed ?? 50),
            monitor_index: Number(strip.monitor_index || 1),
            edge: strip.edge || 'top',
            reverse: Boolean(strip.reverse),
        })),
    };
}

async function saveDevice(silent = false) {
    const id = deviceFormValue('device-id');
    try {
        const result = id ? await api.updateDevice(id, getDevicePayload()) : await api.createDevice(getDevicePayload());
        if (!result.success) throw new Error(result.message || 'Cihaz kaydedilemedi');
        if (!silent) showToast(id ? 'Cihaz güncellendi' : 'Cihaz eklendi', 'success');
        await fetchDevices();
        if (!silent) setDeviceForm(result.device);
        return true;
    } catch (error) {
        showToast(error.message || 'Cihaz kaydedilemedi', 'error');
        return false;
    }
}

async function applyDevice(silent = false) {
    const id = deviceFormValue('device-id');
    if (!id) return false;
    try {
        const result = await api.syncDevice(id, { push: true });
        if (!result.success) throw new Error(result.message || 'Wemos ile eşitlenemedi');
        if (result.validation) deviceValidations.set(id, result.validation);
        await fetchDevices();
        if (!silent) {
            setDeviceForm(result.device);
            showToast(result.pushed ? 'Ayarlar Wemos\'a gönderildi' : (result.message || 'Ayar gönderilemedi'), result.pushed ? 'success' : 'warning');
        } else {
            const [state, text] = validationChip(result.validation, result.device);
            setChip(document.getElementById('device-validation'), state, text);
            if (!result.pushed) showToast(result.message || 'Ayar gönderilemedi', 'warning');
        }
        return true;
    } catch (error) {
        showToast(error.message || 'Wemos ile eşitlenemedi', 'error');
        return false;
    }
}

async function fetchStripsFromWemos() {
    const id = deviceFormValue('device-id');
    if (!id) return;
    const button = document.getElementById('fetch-strips-button');
    button.disabled = true;
    try {
        const saved = await saveDevice(true);
        if (!saved) return;
        const result = await api.syncDevice(id, { push: false });
        if (!result.success) throw new Error(result.message || 'Şeritler alınamadı');
        if (result.validation) deviceValidations.set(id, result.validation);
        await fetchDevices();
        setDeviceForm(result.device);
        const count = (result.device.strips || []).length;
        showToast(count ? `${count} şerit alındı` : 'Wemos etkin şerit bildirmedi', count ? 'success' : 'warning');
    } catch (error) {
        showToast(error.message || 'Şeritler alınamadı', 'error');
    } finally {
        button.disabled = !deviceFormValue('device-id');
    }
}

async function scanDevices() {
    const results = document.getElementById('device-discovery-results');
    try {
        showToast('Ağdaki Wemos cihazları aranıyor', 'info');
        const result = await api.scanDevices();
        if (!result.success) throw new Error(result.message || 'Tarama başarısız');
        results.replaceChildren();
        results.classList.remove('hidden');
        if (!result.devices.length) {
            results.textContent = 'Cihaz bulunamadı. Hiçbir ayar değiştirilmedi.';
            return;
        }
        const title = document.createElement('span');
        title.className = 'muted';
        title.textContent = 'Bulunan cihazlar (seçince forma aktarılır):';
        results.appendChild(title);
        result.devices.forEach(found => {
            const known = deviceRecords.find(device => device.ip === found.ip);
            const button = document.createElement('button');
            button.className = 'btn btn-sm';
            button.textContent = known ? `${found.ip} (${known.name})` : `${found.ip}:${found.port}`;
            button.addEventListener('click', () => {
                if (known) { setDeviceForm(known); return; }
                startNewDevice();
                document.getElementById('device-ip').value = found.ip;
                document.getElementById('device-port').value = found.port;
                document.getElementById('device-name').focus();
            });
            results.appendChild(button);
        });
        const close = document.createElement('button');
        close.className = 'btn btn-ghost btn-sm';
        close.textContent = 'Kapat';
        close.addEventListener('click', () => results.classList.add('hidden'));
        results.appendChild(close);
    } catch (error) {
        showToast(error.message || 'Tarama başarısız', 'error');
    }
}

async function validateDevices(silent = false) {
    try {
        const result = await api.validateDevices();
        if (!result.success) throw new Error(result.message || 'Doğrulama başarısız');
        deviceValidations = new Map((result.devices || []).map(device => [device.id, device]));
        renderDevices();
        const selected = deviceRecords.find(device => device.id === deviceFormValue('device-id'));
        if (selected) {
            const [state, text] = validationChip(deviceValidations.get(selected.id), selected);
            setChip(document.getElementById('device-validation'), state, text);
        }
        if (silent) return;
        const problems = (result.devices || []).filter(device => !device.compatible);
        if (!problems.length) showToast('Tüm cihazlar doğrulandı', 'success');
        else showToast(problems.map(device => `${deviceName(device.id)}: ${device.message}`).join(' | '), 'error');
    } catch (error) {
        if (!silent) showToast(error.message || 'Doğrulama başarısız', 'error');
    }
}

function deviceName(id) {
    const device = deviceRecords.find(item => item.id === id);
    return device ? device.name : id;
}

async function toggleMultiDeviceMode(target) {
    const toggle = document.getElementById('multi-device-toggle');
    if (target) {
        const ok = await confirmDialog('Çoklu cihaz modu',
            'Açıldığında yalnız doğrulanmış ve etkin cihazlara LED verisi gönderilir. Doğrulanamayan cihazlar atlanır. Devam edilsin mi?',
            { confirmLabel: 'Aç' });
        if (!ok) { toggle.checked = false; return; }
    }
    try {
        const result = await api.setMultiDeviceMode(target);
        if (!result.success) throw new Error(result.message || 'Çoklu mod değiştirilemedi');
        multiDeviceEnabled = result.multi_device_enabled;
        renderDevices();
        showToast(multiDeviceEnabled ? 'Çoklu cihaz modu açıldı' : 'Tek cihaz moduna dönüldü', 'success');
    } catch (error) {
        toggle.checked = multiDeviceEnabled;
        showToast(error.message || 'Çoklu mod değiştirilemedi', 'error');
    }
}

document.getElementById('multi-device-toggle').addEventListener('change', event => toggleMultiDeviceMode(event.target.checked));

function startNewDevice() {
    setDeviceForm();
    document.getElementById('device-name').focus();
}

async function deleteSelectedDevice() {
    const id = deviceFormValue('device-id');
    if (!id || id === LEGACY_ID) return;
    const ok = await confirmDialog('Cihazı sil', `"${deviceName(id)}" kaydı silinecek. Wemos'taki ayarlar değişmez.`, { confirmLabel: 'Sil', danger: true });
    if (!ok) return;
    try {
        const result = await api.deleteDevice(id);
        if (!result.success) throw new Error(result.message || 'Cihaz silinemedi');
        showToast('Cihaz silindi', 'success');
        deviceValidations.delete(id);
        setDeviceForm();
        await fetchDevices();
    } catch (error) {
        showToast(error.message || 'Cihaz silinemedi', 'error');
    }
}

// --- Durum ---
let connectionTarget = '';
let connectionTestRequest = 0;

async function fetchStatusData() {
    try {
        const status = await api.getStatus();
        updateUI(status);
    } catch (error) {
        renderConnection(els, { connectivity: { state: 'backend_unavailable' } });
    }
}

function updateFpsRing(fps, targetFps = 60) {
    const ring = els.fpsRing;
    if (!ring) return;
    ring.classList.remove('fps-good', 'fps-warn', 'fps-bad');
    const ratio = targetFps > 0 ? (fps / targetFps) : 0;
    ring.style.setProperty('--pct', Math.min(100, Math.round(ratio * 100)));
    if (ratio >= 0.85) ring.classList.add('fps-good');
    else if (ratio >= 0.60) ring.classList.add('fps-warn');
    else ring.classList.add('fps-bad');
}

function setIfIdle(element, value) {
    if (element && value !== undefined && document.activeElement !== element && !isEditingMode) element.value = value;
}

function updateUI(status) {
    applyMonitorSnapshot(status);
    renderConnection(els, status);

    const target = JSON.stringify([status.wemos_ip || '', status.wemos_port || 7777]);
    if (target !== connectionTarget) {
        connectionTarget = target;
        connectionTestRequest++;
        document.getElementById('connection-test-details').classList.remove('show');
    }

    els.wemosIp.textContent = status.wemos_ip || '-';
    els.wemosPort.textContent = status.wemos_port || '-';
    els.localIp.textContent = status.local_ip || '-';
    document.getElementById('ov-wemos-ip').textContent = status.wemos_ip ? `${status.wemos_ip}:${status.wemos_port || 7777}` : 'IP bekleniyor';

    const fps = status.actual_fps ? parseFloat(status.actual_fps) : 0;
    els.fpsVal.textContent = fps.toFixed(0);
    updateFpsRing(fps, status.fps || 60);
    els.packetsSent.textContent = status.packets_sent || '0';
    const errCount = status.errors || 0;
    els.errorCount.textContent = errCount;
    els.errorCount.style.color = errCount > 0 ? 'var(--bad)' : '';

    const lastErrorRow = document.getElementById('last-error-row');
    const lastErrorText = document.getElementById('last-error-text');
    if (errCount > 0 && status.last_error) {
        lastErrorRow.classList.remove('hidden');
        lastErrorText.textContent = status.last_error;
        lastErrorText.title = status.last_error;
    } else {
        lastErrorRow.classList.add('hidden');
    }

    if (!isEditingMode) {
        els.totalLeds.textContent = status.total_leds || '0';
        if (els.ambTotalLeds) els.ambTotalLeds.textContent = status.total_leds || '0';
        updateEstimatedAmper();
        setIfIdle(els.topLeds, status.top_leds);
        setIfIdle(els.bottomLeds, status.bottom_leds);
        setIfIdle(els.leftLeds, status.left_leds);
        setIfIdle(els.rightLeds, status.right_leds);
        setIfIdle(els.edgeWidth, status.edge_width);
        setIfIdle(els.edgeOffset, status.edge_offset);
        renderEdgePreview(status.top_leds || 0, status.bottom_leds || 0, status.left_leds || 0, status.right_leds || 0);

        if (status.led_monitor_index !== undefined && els.ledMonitorIndex && document.activeElement !== els.ledMonitorIndex
            && Array.from(els.ledMonitorIndex.options).some(option => option.value === String(status.led_monitor_index))) {
            els.ledMonitorIndex.value = status.led_monitor_index;
        }
        const monitorName = document.getElementById('ov-monitor-name');
        if (monitorName && status.led_monitor_index !== undefined) monitorName.textContent = monitorLabel(status.led_monitor_index);
        const previewMonitor = document.getElementById('pv-monitor');
        if (previewMonitor && status.led_monitor_index !== undefined) previewMonitor.textContent = `Monitör ${status.led_monitor_index}`;
    }

    if (status.idle_mode !== undefined && els.idleMode && document.activeElement !== els.idleMode && !isEditingMode) els.idleMode.checked = status.idle_mode;
    if (status.idle_use_windows_color !== undefined && els.idleUseWindowsColor && document.activeElement !== els.idleUseWindowsColor && !isEditingMode) {
        els.idleUseWindowsColor.checked = status.idle_use_windows_color;
        if (els.idleColor) els.idleColor.disabled = status.idle_use_windows_color;
    }
    if (status.idle_color !== undefined && els.idleColor && document.activeElement !== els.idleColor && !isEditingMode) els.idleColor.value = status.idle_color;
    if (status.idle_brightness !== undefined && els.idleBrightness && document.activeElement !== els.idleBrightness && !isEditingMode) {
        els.idleBrightness.value = status.idle_brightness;
        if (els.idleBrightnessVal) els.idleBrightnessVal.textContent = status.idle_brightness + '%';
    }
    if (status.fullscreen_max_brightness !== undefined && els.fullscreenMaxBrightness && document.activeElement !== els.fullscreenMaxBrightness && !isEditingMode) {
        els.fullscreenMaxBrightness.value = status.fullscreen_max_brightness;
        if (els.fullscreenMaxBrightnessVal) els.fullscreenMaxBrightnessVal.textContent = status.fullscreen_max_brightness;
    }
    if (status.fps !== undefined && els.targetFps && document.activeElement !== els.targetFps && !isEditingMode) {
        els.targetFps.value = status.fps;
        if (els.targetFpsVal) els.targetFpsVal.textContent = status.fps + ' FPS';
        const display = document.getElementById('target-fps-display');
        if (display) display.textContent = status.fps;
    }

    if (status.multi_device_enabled !== undefined && status.multi_device_enabled !== multiDeviceEnabled) {
        multiDeviceEnabled = Boolean(status.multi_device_enabled);
        renderDevices();
    }
}

// --- Bağlantı testi ---
async function testWemosConnection() {
    const details = document.getElementById('connection-test-details');
    const udpEl = document.getElementById('udp-test-result');
    const httpEl = document.getElementById('http-test-result');
    const msgEl = document.getElementById('connection-test-message');

    if (!details) return;
    details.classList.add('show');
    const request = ++connectionTestRequest;

    udpEl.className = 'kv-value checking';
    udpEl.textContent = 'Test ediliyor...';
    httpEl.className = 'kv-value checking';
    httpEl.textContent = 'Test ediliyor...';
    msgEl.textContent = 'Test ediliyor...';

    try {
        const result = await api.testWemosConnection();
        if (request !== connectionTestRequest) return;
        const udpOk = result.udp_ok;
        const httpOk = result.http_ok;

        udpEl.className = `kv-value ${udpOk ? 'ok' : 'fail'}`;
        udpEl.textContent = udpOk ? 'Başarılı' : 'Başarısız';
        httpEl.className = `kv-value ${httpOk ? 'ok' : 'fail'}`;
        httpEl.textContent = httpOk ? 'Başarılı' : 'Başarısız';
        msgEl.textContent = result.message || '';

        // Ana gösterge yalnız /api/status ile güncellenir; test yanıtı eskimiş olabilir.
        if (result.applied === false) msgEl.textContent = 'Test sonucu güncelliğini yitirdi; güncel durum kontrol ediliyor.';
        await fetchStatus();
    } catch (e) {
        if (request !== connectionTestRequest) return;
        udpEl.className = 'kv-value fail';
        udpEl.textContent = 'Başarısız';
        httpEl.className = 'kv-value fail';
        httpEl.textContent = 'Başarısız';
        msgEl.textContent = 'Test hatası: ' + e.message;
        await fetchStatus();
    }
}

// --- Actions ---
async function forceScan() {
    showToast('Ağ taranıyor, birkaç saniye sürebilir', 'info');
    try {
        const result = await api.scanNetwork();
        if (result.found) {
            if (els.manualIpInput) els.manualIpInput.value = result.ip || '';
            showToast(result.message || ('Cihaz bulundu: ' + result.ip), 'success');
            setTimeout(fetchStatus, 500);
            setTimeout(fetchStatus, 2000);
            setTimeout(fetchStatus, 5000);
        } else {
            showToast('Cihaz bulunamadı. Wemos açık ve aynı ağda olmalı.', 'error');
        }
    } catch (e) { showToast('Tarama hatası: zaman aşımı olabilir', 'error'); }
}

async function saveConfig(silent = false) {
    try {
        const config = {
            top_leds: parseInt(els.topLeds.value) || 0,
            bottom_leds: parseInt(els.bottomLeds.value) || 0,
            left_leds: parseInt(els.leftLeds.value) || 0,
            right_leds: parseInt(els.rightLeds.value) || 0,
            edge_width: parseInt(els.edgeWidth.value) || 20,
            edge_offset: parseInt(els.edgeOffset.value) || 0,
            idle_mode: Boolean(els.idleMode && els.idleMode.checked),
            idle_use_windows_color: Boolean(els.idleUseWindowsColor && els.idleUseWindowsColor.checked),
            idle_color: (els.idleColor && els.idleColor.value) || '#ff0000',
            idle_brightness: (els.idleBrightness && parseInt(els.idleBrightness.value)) || 50,
            fullscreen_max_brightness: (els.fullscreenMaxBrightness && parseInt(els.fullscreenMaxBrightness.value)) || 255,
            fps: (els.targetFps && parseInt(els.targetFps.value)) || 60,
            wemos_port: parseInt(els.wemosPort?.textContent) || 7777,
            led_monitor_index: parseInt(els.ledMonitorIndex?.value) || 1,
        };

        const manualIp = els.manualIpInput && els.manualIpInput.value.trim();
        if (manualIp) config.wemos_ip = manualIp;

        const totalLeds = config.top_leds + config.bottom_leds + config.left_leds + config.right_leds;
        if (totalLeds > MAX_LED_COUNT) {
            showToast(`Toplam LED sayısı (${totalLeds}) firmware sınırını (${MAX_LED_COUNT}) aşıyor`, 'error');
            return;
        }

        await api.saveConfig(config);
        if (!silent) showToast('Ayarlar kaydedildi', 'success');
        setTimeout(fetchStatus, 500);
    } catch (e) {
        console.error(e);
        if (!silent) showToast('Kayıt başarısız', 'error');
    }
}

async function restartApp() {
    const ok = await confirmDialog('Uygulamayı yeniden başlat', 'LuxEdge kapatılıp yeniden açılacak.', { confirmLabel: 'Yeniden başlat' });
    if (!ok) return;
    showToast('Uygulama yeniden başlatılıyor', 'warning');
    await api.restartApp();
}

async function restartWemos() {
    showToast('Wemos yeniden başlatılıyor', 'info');
    const result = await api.restartWemos();
    if (result && !result.success) showToast(result.message || 'Wemos yeniden başlatılamadı', 'error');
}

async function toggleSleep() {
    const result = await api.toggleSleep();
    if (result && result.success) showToast(result.message, 'success');
    else showToast((result && result.message) || 'Uyku modu değiştirilemedi', 'error');
}

async function resetWemosWifi() {
    const ok = await confirmDialog('Wi-Fi ve LED ayarlarını sıfırla',
        'Wemos tüm ayarlarını silip hotspot moduna dönecek. Kurulumu yeniden yapmanız gerekir.',
        { confirmLabel: 'Sıfırla', danger: true });
    if (!ok) return;
    try {
        const result = await api.resetWemosWifi();
        showToast(result.message, result.success ? 'success' : 'error');
    } catch (e) { showToast('Hata oluştu', 'error'); }
}

async function saveManualIp(silent = false) {
    const ip = els.manualIpInput.value.trim();
    if (!ip) {
        if (!silent) showToast('Bir IP adresi girin', 'error');
        return;
    }
    const ipRegex = /^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$/;
    if (!ipRegex.test(ip)) {
        if (!silent) showToast('Geçersiz IP biçimi. Örnek: 192.168.1.50', 'error');
        return;
    }
    try {
        const result = await api.saveConfig({ wemos_ip: ip });
        if (result.success) {
            if (!silent) showToast(`IP ${ip} olarak kaydedildi`, 'success');
            setTimeout(fetchStatus, 1000);
            setTimeout(fetchStatus, 4000);
        } else if (!silent) {
            showToast('Kayıt başarısız: ' + (result.message || ''), 'error');
        }
    } catch (e) {
        if (!silent) showToast('Kayıt hatası oluştu', 'error');
    }
}

// --- OTA ---
async function uploadFirmware() {
    const fileInput = document.getElementById('firmware-file');
    const file = fileInput.files[0];
    if (!file) { showToast('Bir .bin dosyası seçin', 'error'); return; }
    if (!file.name.endsWith('.bin')) { showToast('Yalnız .bin dosyaları yüklenebilir', 'error'); return; }

    const button = document.getElementById('btn-upload-firmware');
    const progress = document.getElementById('ota-progress');
    const bar = document.getElementById('ota-progress-bar');
    const statusText = document.getElementById('ota-status');

    button.disabled = true;
    button.textContent = 'Yükleniyor';
    progress.classList.remove('hidden');
    bar.style.width = '0%';
    statusText.textContent = 'Firmware dosyası okunuyor';

    try {
        const wemosIp = document.getElementById('wemos-ip').textContent;
        if (!wemosIp || wemosIp === '-') throw new Error('Wemos IP adresi bulunamadı');

        const formData = new FormData();
        formData.append('firmware', file);
        bar.style.width = '40%';
        statusText.textContent = 'Wemos\'a gönderiliyor, lütfen bekleyin';

        try {
            await fetch(`http://${wemosIp}/firmware`, { method: 'POST', body: formData });
        } catch (fetchError) {
            // Wemos güncelleme sonrası yeniden başlar; bağlantının kopması beklenen durumdur.
        }
        bar.style.width = '100%';
        statusText.textContent = 'Firmware gönderildi. Wemos yeniden başlatılıyor.';
        showToast('Firmware gönderildi, Wemos yeniden başlatılıyor', 'success');
        setTimeout(() => { progress.classList.add('hidden'); fetchStatus(); }, 10000);
    } catch (error) {
        bar.style.width = '0%';
        statusText.textContent = 'Hata: ' + error.message;
        showToast('Firmware yükleme hatası: ' + error.message, 'error');
    } finally {
        button.disabled = false;
        button.textContent = 'Yükle';
    }
}

// --- Günlük ---
let logCache = [];

async function fetchLogsData() {
    if (currentPage !== 'logs') return;
    try {
        const data = await api.getLogs();
        if (data.logs) renderLogs(data.logs);
    } catch (e) {
        const container = document.getElementById('log-entries');
        if (container) container.textContent = 'Günlük yüklenemedi: ' + e.message;
    }
}

function renderLogs(logs) {
    logCache = logs;
    const container = document.getElementById('log-entries');
    if (!container) return;
    container.replaceChildren();
    logs.forEach(entry => {
        const row = document.createElement('div');
        row.className = 'log-entry';
        const time = document.createElement('span'); time.className = 'log-time'; time.textContent = entry.time;
        const level = document.createElement('span'); level.className = `log-level ${entry.level}`; level.textContent = entry.level;
        const msg = document.createElement('span'); msg.className = 'log-msg'; msg.textContent = entry.msg;
        row.append(time, level, msg);
        container.appendChild(row);
    });
    const autoScroll = document.getElementById('log-autoscroll');
    if (autoScroll && autoScroll.checked) container.scrollTop = container.scrollHeight;
}

function clearLogs() {
    logCache = [];
    const container = document.getElementById('log-entries');
    if (container) {
        container.replaceChildren();
        const note = document.createElement('div');
        note.className = 'list-empty';
        note.textContent = 'Görünüm temizlendi. Yeni kayıtlar gelmeye devam eder.';
        container.appendChild(note);
    }
}

function downloadLogs() {
    const lines = logCache.map(entry => `[${entry.time}] [${entry.level}] ${entry.msg}`).join('\n');
    const blob = new Blob([lines], { type: 'text/plain' });
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = `luxedge_log_${new Date().toISOString().slice(0, 19).replace(/:/g, '-')}.txt`;
    link.click();
}

// --- Başlangıç ---
document.addEventListener('visibilitychange', () => poller.refresh());
if (window.luxedge && window.luxedge.onWindowVisibility) {
    window.luxedge.onWindowVisibility(visible => {
        electronVisible = visible;
        poller.refresh();
    });
}
bindAmbilightInputs();
poller.refresh();
startNewDevice();
fetchDevices().then(() => validateDevices(true));
fetchStatus();
