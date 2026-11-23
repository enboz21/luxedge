// LuxEdge kontrol paneli - renderer mantığı
// Sayfalar: Genel Bakış, Ambilight, Cihazlar, Wemos, Günlük, Sistem

const api = window.luxedge;
let electronVisible = true;
const poller = createPoller(() => !document.hidden && electronVisible);
const fetchStatus = poller.add('status', fetchStatusData, 1500);
const fetchLogs = poller.add('logs', fetchLogsData, 2000);
const fetchLiveLogFile = poller.add('live-log-file', fetchLiveLogFileData, 2000);
const fetchWindowsDiagnostics = poller.add('windows-diagnostics', fetchWindowsDiagnosticsData, 3000);
const fetchOverview = poller.add('overview', fetchOverviewData, 1500);

// Firmware sınırı: wemos_code.ino içindeki MAX_LED_COUNT ile aynı olmalı
const MAX_LED_COUNT = 170;
const UI_VERSION = '1.7.4';

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
    overview: ['Genel Bakış', 'Tüm cihazlar, canlı renkler ve animasyonlar'],
    ambilight: ['Ambilight', 'Ana Wemos için ekran kenarı LED ayarları'],
    devices: ['Cihazlar', 'Birden fazla Wemos ve şerit bazlı görevler'],
    wemos: ['Wemos', 'Bağlantı, cihaz işlemleri ve firmware'],
    logs: ['Günlük', 'PC, Wemos, Windows ve uygulama kayıtları'],
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
    if (name === 'logs') {
        fetchLogs();
        fetchLiveLogFile();
        fetchWindowsDiagnostics();
    }
    if (name === 'overview') fetchOverview();
    if (name === 'devices') fetchDevices();
    if (name === 'wemos') loadWemosInfo();
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

async function fetchOverviewData() {
    if (currentPage !== 'overview') return;
    const container = document.getElementById('overview-devices');
    const message = document.getElementById('overview-message');
    try {
        const result = await api.getDevicesOverview();
        if (!result.success) throw new Error(result.message || 'Durum alınamadı');
        DeviceDashboard.render(container, result.devices || [], result.multi_device_enabled, device => {
            showPage('devices');
            setDeviceForm(device);
        });
        document.getElementById('overview-device-count').textContent = `${(result.devices || []).length} cihaz`;
        message.textContent = 'Durumlar otomatik yenilenir · Hareketli efektler temsili önizlemedir.';
    } catch (error) {
        // Never leave stale cards looking online after the backend disappears.
        container.dataset.signature = '';
        DeviceDashboard.render(container, deviceRecords.map(device => ({ ...device, live: null, frame: null })),
            multiDeviceEnabled, device => { showPage('devices'); setDeviceForm(device); });
        message.textContent = 'Arka plan hizmetine ulaşılamıyor. Güncel cihaz durumu bilinmiyor.';
    }
}

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
    const validation = deviceValidations.get(device.id);
    const [state, text] = validationChip(validation, device);
    setChip(chip, state, text);
    if (validation && validation.message) chip.title = validation.message;
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
    renderWemosTargets();
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
    showEditorValidation(device);
    updateRoleFields();
    renderDevices();
    setStripStatus('');
    if (device && role === 'strips' && !editorStrips.length && !autoFetchedStrips.has(device.id)) {
        autoFetchedStrips.add(device.id);
        fetchStripsFromWemos(true);
    }
}

const autoFetchedStrips = new Set();

function setStripStatus(text, isError = false) {
    const element = document.getElementById('strip-status');
    if (!element) return;
    element.textContent = text;
    element.style.color = isError ? 'var(--bad)' : '';
}

function describeFirmware(validation) {
    if (!validation) return '';
    if (!validation.reachable) return 'Wemos\'a ulaşılamıyor; IP adresini ve cihazın açık olduğunu kontrol edin.';
    if (!validation.segments_supported) return `Wemos firmware ${validation.firmware_version || 'eski'}: şerit modlarını desteklemiyor. 1.7.0 firmware yükleyin (Wemos sayfası, Firmware güncelleme).`;
    return `Firmware ${validation.firmware_version}, şerit modları destekleniyor.`;
}

function showEditorValidation(device) {
    const validation = device ? deviceValidations.get(device.id) : null;
    const [state, text] = validationChip(validation, device);
    const chip = document.getElementById('device-validation');
    setChip(chip, device ? state : 'neutral', device ? text : 'Kaydedilmedi');
    if (validation && validation.message) chip.title = validation.message;
    const hint = document.getElementById('device-editor-hint');
    if (validation && !validation.compatible) {
        const detail = validation.reachable
            ? `Doğrulama: ${validation.message}. Uygulamanın göndereceği LED sayısı Wemos'un beklediği sayıyla aynı olmalı.`
            : `Doğrulama: ${validation.message}.`;
        hint.textContent = hint.textContent ? `${hint.textContent} ${detail}` : detail;
    }
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
        setStripStatus('Wemos\'a gönderiliyor...');
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
        if (!silent) {
            setDeviceForm(result.device);
            if (result.device.role === 'strips') await fetchStripsFromWemos(true);
        }
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
        const stamp = new Date().toLocaleTimeString('tr-TR', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
        if (!silent) {
            setDeviceForm(result.device);
            showToast(result.pushed ? 'Ayarlar Wemos\'a gönderildi' : (result.message || 'Ayar gönderilemedi'), result.pushed ? 'success' : 'warning');
        } else {
            showEditorValidation(result.device);
            if (!result.pushed) showToast(result.message || 'Ayar gönderilemedi', 'warning');
        }
        setStripStatus(result.pushed
            ? `${stamp} Ayarlar Wemos'a gönderildi. ${describeFirmware(result.validation)}`
            : `${stamp} Gönderilemedi: ${result.message || ''} ${describeFirmware(result.validation)}`, !result.pushed);
        return true;
    } catch (error) {
        setStripStatus(`Gönderilemedi: ${error.message}`, true);
        showToast(error.message || 'Wemos ile eşitlenemedi', 'error');
        return false;
    }
}

async function fetchStripsFromWemos(silent = false) {
    const id = deviceFormValue('device-id');
    if (!id) return;
    const button = document.getElementById('fetch-strips-button');
    button.disabled = true;
    setStripStatus('Wemos\'tan şerit bilgisi alınıyor...');
    try {
        const saved = await saveDevice(true);
        if (!saved) return;
        const result = await api.syncDevice(id, { push: false });
        if (!result.success) throw new Error(result.message || 'Şeritler alınamadı');
        if (result.validation) deviceValidations.set(id, result.validation);
        await fetchDevices();
        autoFetchedStrips.add(id);
        setDeviceForm(result.device);
        const count = (result.device.strips || []).length;
        setStripStatus(`${count ? count + ' şerit alındı. ' : 'Wemos etkin şerit bildirmedi. '}${describeFirmware(result.validation)}`, !count || !(result.validation && result.validation.segments_supported));
        if (!silent) showToast(count ? `${count} şerit alındı` : 'Wemos etkin şerit bildirmedi', count ? 'success' : 'warning');
    } catch (error) {
        setStripStatus(`Şeritler alınamadı: ${error.message}`, true);
        if (!silent) showToast(error.message || 'Şeritler alınamadı', 'error');
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
        if (selected) setDeviceForm(selected);
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

// --- Wemos sayfası: hedef cihaz ---
let mainTarget = { ip: '', port: 7777 };
let connectionTarget = '';
let connectionTestRequest = 0;

function wemosTargets() {
    const list = [{ id: 'main', name: 'Ana Wemos', ip: mainTarget.ip, port: mainTarget.port }];
    deviceRecords.forEach(device => {
        if (device.id === LEGACY_ID) return;
        list.push({ id: device.id, name: device.name, ip: device.ip, port: device.port });
    });
    return list;
}

function selectedWemosTarget() {
    const select = document.getElementById('wemos-target');
    const chosen = select ? select.value : 'main';
    return wemosTargets().find(target => target.id === chosen) || wemosTargets()[0];
}

function renderWemosTargets() {
    const select = document.getElementById('wemos-target');
    if (!select) return;
    const previous = select.value || 'main';
    const targets = wemosTargets();
    select.replaceChildren(...targets.map(target => {
        const option = document.createElement('option');
        option.value = target.id;
        option.textContent = target.ip ? `${target.name} (${target.ip})` : `${target.name} (IP yok)`;
        return option;
    }));
    select.value = targets.some(target => target.id === previous) ? previous : 'main';
    renderWemosTargetDetails();
}

function renderWemosTargetDetails() {
    const target = selectedWemosTarget();
    els.wemosIp.textContent = target.ip || '-';
    els.wemosPort.textContent = target.port || '-';
    const label = document.getElementById('wemos-action-target');
    if (label) label.textContent = target.name;
}

document.getElementById('wemos-target').addEventListener('change', () => {
    connectionTestRequest++;
    document.getElementById('connection-test-details').classList.remove('show');
    renderWemosTargetDetails();
    loadWemosInfo();
});

async function loadWemosInfo() {
    const container = document.getElementById('wemos-info');
    const target = selectedWemosTarget();
    if (!container) return;
    if (!target.ip) { container.textContent = 'Bu cihazın IP adresi yok.'; return; }
    container.textContent = 'Bilgi alınıyor...';
    try {
        const result = await api.getWemosStatus({ ip: target.ip });
        if (!result.success) throw new Error(result.message || 'Durum alınamadı');
        const st = result.status;
        container.replaceChildren();
        const rows = [
            ['Firmware', st.firmware_version || 'bilinmiyor'],
            ['Mod', st.mode === 'hotspot' ? 'Hotspot (kurulum)' : 'Wi-Fi'],
            ['Sinyal', st.rssi ? `${st.rssi} dBm` : '-'],
            ['Çalışma süresi', st.uptime_seconds !== undefined ? `${Math.floor(st.uptime_seconds / 60)} dk` : '-'],
            ['Toplam LED', st.led_count ?? '-'],
            ['Akış LED', st.stream_led_count ?? (st.segments_supported ? '-' : 'firmware 1.7.0 gerekli')],
            ['Uyku modu', st.sleep_mode ? 'Açık' : 'Kapalı'],
            ['Son reset', st.reset_reason || '-'],
        ];
        rows.forEach(([label, value]) => {
            const row = document.createElement('div');
            row.className = 'kv';
            const k = document.createElement('span'); k.className = 'kv-label'; k.textContent = label;
            const v = document.createElement('span'); v.className = 'kv-value'; v.textContent = String(value);
            row.append(k, v);
            container.appendChild(row);
        });
        (st.strips || []).filter(strip => strip.enabled).forEach(strip => {
            const row = document.createElement('div');
            row.className = 'kv';
            const k = document.createElement('span'); k.className = 'kv-label'; k.textContent = `Şerit ${strip.index}`;
            const v = document.createElement('span'); v.className = 'kv-value';
            v.textContent = `${strip.led_pin} · ${strip.led_count} LED${strip.mode ? ' · ' + (STRIP_MODE_LABELS[strip.mode] || strip.mode) : ''}`;
            row.append(k, v);
            container.appendChild(row);
        });
    } catch (error) {
        container.textContent = `Bilgi alınamadı: ${error.message}`;
    }
}

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

    if (mainTarget.ip !== (status.wemos_ip || '') || mainTarget.port !== (status.wemos_port || 7777)) {
        mainTarget = { ip: status.wemos_ip || '', port: status.wemos_port || 7777 };
        renderWemosTargets();
    }
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

    const backendVersion = document.getElementById('backend-version');
    if (backendVersion) backendVersion.textContent = status.backend_version ? `${status.backend_version} (port ${status.backend_port || '?'})` : 'eski sürüm (sürüm bildirmiyor)';
    const warning = document.getElementById('version-warning');
    if (warning) {
        const unreachable = status.connectivity && status.connectivity.state === 'backend_unavailable';
        let text = '';
        if (unreachable) {
            const exit = status.backend_exit;
            text = exit === null || exit === undefined
                ? `Arka plan hizmetine (port ${status.backend_port || '?'}) ulaşılamıyor. Başlatılıyorsa birkaç saniye bekleyin.`
                : `Arka plan hizmeti kapandı (çıkış kodu ${exit}). Son çıktı:`;
            if (Array.isArray(status.backend_output) && status.backend_output.length && exit !== null && exit !== undefined) {
                text += '\n' + status.backend_output.join('\n');
            }
        } else if (status.backend_version !== UI_VERSION) {
            text = `Arka plan hizmeti ${status.backend_version || 'sürüm bildirmeyen eski bir sürüm'}, arayüz ${UI_VERSION}. Büyük olasılıkla tepside kurulu eski LuxEdge çalışıyor; onu kapatın ya da lush_backend.exe dosyasını yeniden derleyin.`;
        }
        warning.classList.toggle('hidden', !text);
        warning.textContent = text;
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
        const target = typeof selectedWemosTarget === 'function' ? selectedWemosTarget() : undefined;
        const result = await api.testWemosConnection(target && target.id !== 'main' ? { ip: target.ip, port: target.port } : undefined);
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
            wemos_port: mainTarget.port || 7777,
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

function actionTarget() {
    const target = selectedWemosTarget();
    if (!target.ip) { showToast(`${target.name} için IP adresi yok`, 'error'); return null; }
    return target;
}

async function restartWemos() {
    const target = actionTarget();
    if (!target) return;
    showToast(`${target.name} yeniden başlatılıyor`, 'info');
    const result = await api.restartWemos({ ip: target.ip });
    if (result && !result.success) showToast(result.message || 'Wemos yeniden başlatılamadı', 'error');
}

async function toggleSleep() {
    const target = actionTarget();
    if (!target) return;
    const result = await api.toggleSleep({ ip: target.ip });
    if (result && result.success) showToast(`${target.name}: ${result.message}`, 'success');
    else showToast((result && result.message) || 'Uyku modu değiştirilemedi', 'error');
}

async function resetWemosWifi() {
    const target = actionTarget();
    if (!target) return;
    const ok = await confirmDialog('Wi-Fi ve LED ayarlarını sıfırla',
        `${target.name} (${target.ip}) tüm ayarlarını silip hotspot moduna dönecek. Kurulumu yeniden yapmanız gerekir.`,
        { confirmLabel: 'Sıfırla', danger: true });
    if (!ok) return;
    try {
        const result = await api.resetWemosWifi({ ip: target.ip });
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
        const wemosIp = selectedWemosTarget().ip;
        if (!wemosIp) throw new Error('Seçilen cihazın IP adresi yok');

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

// --- Windows ağ tanılaması ---
const WINDOWS_DIAGNOSTICS_STATES = {
    idle: 'Oturum kapalı.',
    starting: 'Oturum hazırlanıyor. Windows gerekirse yönetici onayı isteyecek.',
    active: 'Oturum açık.',
    stopping: 'Oturum kapanıyor. Geçici değişiklikler geri yükleniyor…',
    restored: 'Oturum kapandı. Geçici değişiklikler geri yüklendi.',
    cancelled: 'Oturum iptal edildi.',
    failed: 'Oturum başlatılamadı.',
    restore_conflict: 'Oturum kapandı. Denetim ayarının Windows tarafından değiştirildiği görüldü. Güncel değer korundu.',
    restore_failed: 'Oturum kapandı. Önceki denetim değerine dönüş doğrulanamadı.',
    unsupported: 'Windows tanılaması bu ortamda kullanılamıyor.',
    unavailable: 'Windows tanılamasının durumu alınamadı.',
};
const WINDOWS_DIAGNOSTICS_REASONS = {
    confirmation_cancelled: 'Başlatma onayından vazgeçildi.',
    confirmation_unavailable: 'Uygulama onay penceresini açamadı.',
    uac_cancelled: 'Windows yönetici onayı reddedildi veya iptal edildi.',
    startup_failed: 'Windows tanılama yardımcısı başlatılamadı.',
    audit_snapshot_failed: 'Yönetici önceki denetim değerlerini okuyamadı. Oturum başlatılamadı.',
    security_read_unavailable: 'Yönetici Security günlüğüne okuma erişimi sağlayamadı.',
    policy_busy: 'Başka bir LuxEdge tanılama oturumu denetim ayarlarını kullanıyor.',
    partial_apply_failed: 'Geçici ayarların tamamı uygulanamadı.',
    session_expired: 'Beş dakikalık oturum süresi doldu.',
    backend_exit: 'LuxEdge hizmeti kapandı.',
    backend_unavailable: 'LuxEdge arka plan hizmetine erişilemiyor.',
    busy: 'Bir oturum işlemi zaten sürüyor.',
};
const WINDOWS_CHANNEL_ACCESS = {
    readable: 'okunabilir',
    access_denied: 'okuma izni yok',
    unavailable: 'kullanılamıyor',
    disabled: 'kanal kapalı',
};
const WINDOWS_AUDIT_VALUES = {
    enabled: 'Açık',
    disabled: 'Kapalı',
    access_denied: 'Bilinmiyor (okuma izni yok)',
    unavailable: 'Bilinmiyor',
};
const WINDOWS_DIAGNOSTICS_CAN_START = ['idle', 'restored', 'cancelled', 'failed'];
let windowsDiagnosticsStatus;
let windowsDiagnosticsNotice = '';
let windowsDiagnosticsPending = false;
let windowsDiagnosticsRevision = 0;

async function fetchWindowsDiagnosticsData() {
    if (currentPage !== 'logs' || windowsDiagnosticsPending) return;
    const revision = windowsDiagnosticsRevision;
    let status;
    try {
        status = await api?.getWindowsDiagnostics?.();
    } catch { /* Keep periodically checking while this page is visible. */ }
    if (revision !== windowsDiagnosticsRevision) return;
    windowsDiagnosticsStatus = status || { state: 'unavailable' };
    renderWindowsDiagnostics();
}

function windowsAuditDisplay(status, eventId) {
    const current = status.system_audit_failure?.[eventId];
    const currentText = WINDOWS_AUDIT_VALUES[current] || 'Bilinmiyor';
    const canStart = WINDOWS_DIAGNOSTICS_CAN_START.includes(status.state);
    const proposal = current === 'enabled' ? 'açık kalacak'
        : current === 'disabled' ? 'oturum boyunca açılacak'
            : 'yönetici önceki değeri kapalı olarak doğrularsa açılacak';
    const currentAndProposal = canStart ? currentText + ' → ' + proposal : currentText;
    const previous = status.previous_audit_failure?.[eventId];
    if (!previous) return currentAndProposal;
    const previousText = WINDOWS_AUDIT_VALUES[previous] || 'Bilinmiyor';
    const changed = Array.isArray(status.changed_audit_failure) && status.changed_audit_failure.includes(eventId);
    return ['Önceki: ' + previousText, 'Güncel: ' + currentAndProposal,
        changed ? 'oturumda geçici olarak açıldı' : ''].filter(Boolean).join(' · ');
}

function renderWindowsDiagnostics() {
    const status = windowsDiagnosticsStatus || {};
    const sources = document.getElementById('windows-diagnostics-sources');
    const security = WINDOWS_CHANNEL_ACCESS[status.security_channel] || 'durumu bilinmiyor';
    const defender = WINDOWS_CHANNEL_ACCESS[status.defender_channel] || 'durumu bilinmiyor';
    sources.textContent = 'Security günlüğü · ' + security
        + (status.elevated_security_read ? ' · Oturumun yönetici okuması etkin' : '')
        + ' | Defender Ağ Koruması · ' + defender;
    for (const eventId of ['5152', '5157']) {
        document.getElementById('windows-audit-' + eventId).textContent = windowsAuditDisplay(status, eventId);
    }
    const state = document.getElementById('windows-diagnostics-state');
    const remaining = status.state === 'active' && Number.isFinite(status.remaining_seconds)
        ? 'Kalan süre ' + Math.max(0, Math.ceil(status.remaining_seconds)) + ' sn.' : '';
    const message = [WINDOWS_DIAGNOSTICS_STATES[status.state], remaining,
        WINDOWS_DIAGNOSTICS_REASONS[status.reason], windowsDiagnosticsNotice].filter(Boolean).join(' ');
    if (state.textContent !== message) state.textContent = message;
    document.getElementById('windows-diagnostics-start').disabled = windowsDiagnosticsPending
        || !WINDOWS_DIAGNOSTICS_CAN_START.includes(status.state);
    document.getElementById('windows-diagnostics-stop').disabled = windowsDiagnosticsPending
        || !['starting', 'active'].includes(status.state);
}

async function changeWindowsDiagnostics(action) {
    if (windowsDiagnosticsPending || !api || !['start', 'stop'].includes(action)) return;
    windowsDiagnosticsRevision++;
    windowsDiagnosticsPending = true;
    windowsDiagnosticsNotice = '';
    renderWindowsDiagnostics();
    try {
        const next = action === 'start' ? await api.startWindowsDiagnostics() : await api.stopWindowsDiagnostics();
        if (next.reason === 'confirmation_cancelled' || next.state === 'unavailable') {
            windowsDiagnosticsNotice = WINDOWS_DIAGNOSTICS_REASONS[next.reason] || WINDOWS_DIAGNOSTICS_STATES.unavailable;
        } else {
            windowsDiagnosticsStatus = next;
        }
    } catch {
        windowsDiagnosticsNotice = WINDOWS_DIAGNOSTICS_STATES.unavailable;
    } finally {
        windowsDiagnosticsPending = false;
        renderWindowsDiagnostics();
    }
}

// --- Günlük dosyaları ---
const LOG_FILE_MESSAGES = {
    idle: 'Canlı TXT kaydı kapalı.',
    writing: 'Yeni günlük kayıtları TXT dosyasına yazılıyor.',
    stopped: 'Canlı TXT kaydı durduruldu.',
    cancelled: 'Dosya seçimi iptal edildi.',
    error: 'Canlı TXT dosyasına yazılamadı.',
};
let liveLogFileStatus = { state: 'idle' };
let liveLogFileNotice = '';
let liveLogFilePending = false;
let liveLogFileStateUnavailable = false;
let liveLogFileRevision = 0;
let incidentLogExport = null;

async function fetchLiveLogFileData() {
    if (currentPage !== 'logs' || liveLogFilePending || !api.getLiveLogFileState) return;
    const revision = liveLogFileRevision;
    try {
        const result = await api.getLiveLogFileState();
        if (revision !== liveLogFileRevision || !result?.state) return;
        liveLogFileStatus = result;
        liveLogFileStateUnavailable = false;
        renderLiveLogFile();
    } catch {
        if (revision !== liveLogFileRevision) return;
        liveLogFileStateUnavailable = true;
        renderLiveLogFile();
    }
}

function renderLiveLogFile() {
    const status = document.getElementById('log-live-file-status');
    const path = document.getElementById('log-live-file-path');
    const writing = liveLogFileStatus.state === 'writing';
    const count = Number.isSafeInteger(liveLogFileStatus.written) ? liveLogFileStatus.written : 0;
    const summary = liveLogFilePending ? 'Canlı TXT kayıt işlemi sürüyor…' : [
        liveLogFileNotice || LOG_FILE_MESSAGES[liveLogFileStatus.state],
        liveLogFileStateUnavailable ? 'Canlı kayıt durumu alınamadı.' : '',
        count > 0 ? count.toLocaleString('tr-TR') + ' kayıt' : '',
        liveLogFileStatus.message,
    ].filter(Boolean).join(' · ');
    if (status.textContent !== summary) status.textContent = summary;
    const target = liveLogFileStatus.path || '';
    if (path.textContent !== target) path.textContent = target;
    path.hidden = !target;
    document.getElementById('log-live-file-start').disabled = liveLogFilePending || writing || !api.startLiveLogFile;
    document.getElementById('log-live-file-stop').disabled = liveLogFilePending || !writing;
}

async function changeLiveLogFile(action) {
    if (liveLogFilePending || !['start', 'stop'].includes(action)) return;
    liveLogFileRevision++;
    liveLogFilePending = true;
    liveLogFileNotice = '';
    renderLiveLogFile();
    try {
        const result = action === 'start' ? await api.startLiveLogFile() : await api.stopLiveLogFile();
        if (!result?.state) throw new Error('live-log-file');
        liveLogFileStateUnavailable = false;
        if (result.state === 'cancelled') liveLogFileNotice = LOG_FILE_MESSAGES.cancelled;
        else liveLogFileStatus = result;
    } catch {
        liveLogFileNotice = 'Canlı TXT kayıt işlemi tamamlanamadı.';
    } finally {
        liveLogFilePending = false;
        renderLiveLogFile();
    }
}

function renderIncidentLogExport() {
    const button = document.getElementById('log-incident-export');
    const status = document.getElementById('log-incident-export-status');
    const path = document.getElementById('log-incident-export-path');
    const sameIncident = selectedLogIncident && incidentLogExport?.key === String(logKey(selectedLogIncident));
    const result = sameIncident ? incidentLogExport.result : null;
    const saved = result?.state === 'saved' || result?.state === 'stopped';
    let message = '';
    if (sameIncident && incidentLogExport.pending) message = 'Olay aralığındaki arşiv kayıtları hazırlanıyor…';
    else if (saved) message = 'Olay bağlamı TXT dosyasına kaydedildi.'
        + (Number.isSafeInteger(result.written) ? ' ' + result.written.toLocaleString('tr-TR') + ' kayıt.' : '');
    else if (result?.state === 'cancelled') message = 'Olay dosyası kaydedilmedi. Dosya seçimi iptal edildi.';
    else if (result) message = result.message || 'Olay bağlamı kaydedilemedi.';
    if (status.textContent !== message) status.textContent = message;
    status.hidden = !message;
    const target = result?.path || '';
    if (path.textContent !== target) path.textContent = target;
    path.hidden = !target;
    button.disabled = Boolean(incidentLogExport?.pending) || !api.saveIncidentLog;
}

async function exportSelectedLogIncident() {
    if (!selectedLogIncident || incidentLogExport?.pending || !api.saveIncidentLog) return;
    const at = logEventTime(selectedLogIncident);
    if (!Number.isFinite(at)) return;
    const operation = { key: String(logKey(selectedLogIncident)), pending: true, result: null };
    incidentLogExport = operation;
    renderIncidentLogExport();
    try {
        const result = await api.saveIncidentLog({
            from: at - LOG_CONTEXT_MS, to: at + LOG_CONTEXT_MS, selectedKey: operation.key,
        });
        operation.result = result || { state: 'error' };
    } catch {
        operation.result = { state: 'error' };
    } finally {
        operation.pending = false;
        renderIncidentLogExport();
    }
}

// --- Günlük ---
const {
    sources: LOG_SOURCES, levels: LOG_LEVELS, stages: LOG_STAGES, eventTime: logEventTime,
    rowTime: logTime, stage: logStage, detailsText: logDetails, timingLines: logTimingLines,
    causes: logCauses, formatEntry: formatLogEntry, formatLocalTime,
} = LogFormat;
const LOG_CONTEXT_MS = 30_000;
const LOG_ARCHIVE_PAGE_SIZE = 100;
const LOG_ARCHIVE_SCAN_PAGES = 4;
const LOG_MIB = 1024 * 1024;
const logDurationFormat = new Intl.NumberFormat('tr-TR', { maximumFractionDigits: 3 });
let logCache = [];
let clearedLogId = null;
let clearedLogTime = 0;
let visibleLogs = [];
let selectedLogIncident = null;
let timelineScrollTop = 0;
const logRows = new Map();
let logHistory = null;
let logIncidentArchive = null;
let logHistoryFilterTimer;
let logRetention = null;
let logRetentionPending = false;
let logRetentionNotice = '';

function logKey(entry) {
    return entry.id ?? JSON.stringify([entry.time, entry.level, entry.msg]);
}

function logTimestamp(entry) {
    return Date.parse(entry.timestamp) || 0;
}

function showLogStatus(message) {
    const status = document.getElementById('log-status');
    if (status.textContent !== message) status.textContent = message;
    status.hidden = !message;
}

async function fetchLogsData() {
    if (currentPage !== 'logs' || logHistory) return;
    try {
        const data = await api.getLogs();
        if (currentPage !== 'logs' || logHistory) return;
        const offline = data.available === false;
        const warnings = [];
        if (offline) warnings.push('PC hizmetine erişilemiyor. Elde bulunan kayıtlar gösteriliyor.');
        if (data.persistent === false) warnings.push('PC hizmetinin günlük dosyasına erişilemiyor.');
        if (data.electronPersistent === false) warnings.push('Uygulamanın günlük dosyasına erişilemiyor.');
        showLogStatus(warnings.join(' '));
        const entries = new Map();
        if (offline) logCache.forEach(entry => entries.set(logKey(entry), entry));
        data.logs.forEach(entry => entries.set(logKey(entry), entry));
        const latest = [...entries.values()].sort((a, b) => logTimestamp(a) - logTimestamp(b)).slice(-1000);
        if (JSON.stringify(latest) !== JSON.stringify(logCache) || document.getElementById('log-entries').children.length === 0) {
            logCache = latest;
            renderLogs();
        }
    } catch {
        if (currentPage !== 'logs' || logHistory) return;
        showLogStatus('Günlük alınamadı. Elde bulunan kayıtlar gösteriliyor.');
        renderLogs();
    }
}

function logDevice(entry, devicesByIp) {
    if (entry.device_id != null) return String(entry.device_id);
    const ip = entry.details?.ip;
    return (ip && devicesByIp?.get(ip)) || (entry.device_name ? String(entry.device_name) : (ip ? 'ip:' + ip : ''));
}

function isAfterLogClear(entry) {
    if (!clearedLogTime) return true;
    if (Number.isSafeInteger(entry.id) && clearedLogId !== null) return entry.id > clearedLogId;
    return logTimestamp(entry) > clearedLogTime;
}

function logDeviceOptions(entries, select) {
    const devicesByIp = new Map();
    const devices = new Map();
    for (const entry of entries) {
        const ip = entry.details?.ip;
        if (!ip || entry.device_id == null) continue;
        const id = String(entry.device_id);
        devicesByIp.set(ip, !devicesByIp.has(ip) || devicesByIp.get(ip) === id ? id : null);
    }
    for (const entry of entries) {
        const key = logDevice(entry, devicesByIp);
        if (key && (entry.device_name || !devices.has(key))) {
            devices.set(key, entry.device_name ? String(entry.device_name) : (key.startsWith('ip:') ? key.slice(3) : key));
        }
    }
    const options = [['', 'Tüm cihazlar'], ...devices];
    if (select && (select.options.length !== options.length || options.some(([id, name], index) =>
        select.options[index].value !== id || select.options[index].text !== name))) {
        const selected = select.value;
        select.replaceChildren(...options.map(([id, name]) => new Option(name, id)));
        if (devices.has(selected)) select.value = selected;
    }
    return { devices, devicesByIp };
}

function readLogFilters() {
    const value = field => document.getElementById('log-' + field).value;
    return {
        from: new Date(value('from')).getTime(), to: new Date(value('to')).getTime() + 999,
        source: value('source'), device: value('device'), level: value('level'), stage: value('stage'),
        search: value('search').trim().toLocaleLowerCase('tr-TR'),
    };
}

function mergeLogEntries(...groups) {
    return [...new Map(groups.flat().map(entry => [logKey(entry), entry])).values()];
}

function rememberArchiveDevices(history, entries) {
    for (const entry of entries) {
        const ip = entry.details?.ip;
        if (entry.device_id == null && !entry.device_name && !ip) continue;
        const key = JSON.stringify([entry.device_id, entry.device_name, ip]);
        history.devices.set(key, { device_id: entry.device_id, device_name: entry.device_name, details: { ip } });
        if (history.devices.size > 512) history.devices.delete(history.devices.keys().next().value);
    }
}

function filterLogs(entries, filters, devicesByIp) {
    return entries.filter(entry => {
        const time = logEventTime(entry);
        if ((Number.isFinite(filters.from) && (time === null || time < filters.from))
                || (Number.isFinite(filters.to) && (time === null || time > filters.to))) return false;
        if (filters.source && filters.source !== (entry.source || 'pc')) return false;
        if (filters.device && filters.device !== logDevice(entry, devicesByIp)) return false;
        if (filters.level && filters.level !== entry.level) return false;
        if (filters.stage && filters.stage !== logStage(entry)) return false;
        return !filters.search || [entry.msg, entry.explanation, entry.code, entry.device_name, entry.device_id,
            LOG_STAGES[logStage(entry)], logDetails(entry), ...logTimingLines(entry), ...logCauses(entry)]
            .join(' ').toLocaleLowerCase('tr-TR').includes(filters.search);
    });
}

function toggleLogArchive() {
    clearTimeout(logHistoryFilterTimer);
    selectedLogIncident = null;
    logIncidentArchive = null;
    showLogStatus('');
    if (logHistory) {
        logHistory = null;
        renderLogs();
        scrollLogsToTail();
        void fetchLogs();
        return;
    }
    logHistory = { offsets: [null], next: null, page: 0, logs: [], devices: new Map(), request: 0, pending: false, message: '' };
    rememberArchiveDevices(logHistory, logCache.concat(deviceRecords.map(device => ({
        device_id: device.id, device_name: device.name, details: { ip: device.ip },
    }))));
    void loadLogArchivePage(0);
}

function changeLogFilters() {
    if (!logHistory) {
        renderLogs();
        return;
    }
    clearTimeout(logHistoryFilterTimer);
    logHistory.request++;
    logHistory.pending = true;
    logHistory.offsets = [null];
    logHistory.next = null;
    logHistory.page = 0;
    logHistory.logs = [];
    logHistory.message = 'Arşiv filtreleniyor…';
    renderLogs();
    logHistoryFilterTimer = setTimeout(() => void loadLogArchivePage(0), 250);
}

function showLogArchivePage(direction) {
    if (!logHistory || logHistory.pending || selectedLogIncident) return;
    const page = logHistory.page + direction;
    if (page < 0 || direction > 0 && !logHistory.next) return;
    if (direction > 0) logHistory.offsets[page] = logHistory.next;
    void loadLogArchivePage(page);
}

async function loadLogArchivePage(page) {
    const history = logHistory;
    if (!history) return;
    const filters = readLogFilters();
    let before = history.offsets[page];
    if (filters.from > filters.to) {
        history.message = '';
        history.pending = false;
        renderLogs();
        return;
    }
    const request = ++history.request;
    history.pending = true;
    history.message = 'Arşiv okunuyor…';
    renderLogs();
    try {
        for (let scan = 0; scan < LOG_ARCHIVE_SCAN_PAGES; scan++) {
            const response = await api.getLogArchive({
                limit: LOG_ARCHIVE_PAGE_SIZE, before: before ?? undefined,
                from: Number.isFinite(filters.from) ? filters.from : undefined,
                to: Number.isFinite(filters.to) ? filters.to : undefined,
                source: filters.source, level: filters.level,
            });
            if (logHistory !== history || history.request !== request) return;
            if (response?.available === false || !Array.isArray(response?.logs)) throw new Error('archive');
            updateLogRetention(response.retention);
            rememberArchiveDevices(history, response.logs);
            const { devicesByIp } = logDeviceOptions([...history.devices.values()]);
            const rows = filterLogs(response.logs, filters, devicesByIp);
            const next = response.has_more && response.next_cursor !== before ? response.next_cursor : null;
            history.page = page;
            history.logs = response.logs;
            history.next = next;
            history.message = rows.length ? 'Arşiv · Sayfa ' + (page + 1)
                : next ? 'Bu aralıkta filtreye uyan kayıt yok. Daha eski kayıtları inceleyebilirsiniz.'
                    : 'Filtreye uyan başka arşiv kaydı yok.';
            if (rows.length || !next) break;
            before = next;
        }
    } catch {
        if (logHistory === history && history.request === request) {
            history.message = 'Arşiv okunamadı. Yeniden denemek için arşivi tekrar açın.';
        }
    } finally {
        if (logHistory === history && history.request === request) {
            history.pending = false;
            renderLogs();
            const container = document.getElementById('log-entries');
            container.scrollTop = container.scrollHeight;
        }
    }
}

function updateLogRetention(retention) {
    if (!retention || !Number.isFinite(retention.limit_bytes)) return;
    const input = document.getElementById('log-retention-limit');
    if (!logRetention || (retention.limit_bytes !== logRetention.limit_bytes && document.activeElement !== input)) {
        input.value = String(retention.limit_bytes / LOG_MIB);
    }
    logRetention = retention;
    renderLogRetention();
}

function renderLogRetention() {
    const usage = document.getElementById('log-retention-usage');
    const oldest = Date.parse(logRetention?.oldest_timestamp);
    const mib = bytes => logDurationFormat.format(bytes / LOG_MIB) + ' MiB';
    const values = logRetention ? [
        mib(logRetention.used_bytes) + ' / ' + mib(logRetention.limit_bytes),
        'En eski kayıt ' + (Number.isFinite(oldest) ? formatLocalTime(oldest) : 'yok'),
        logRetention.legacy_used_bytes > 0 ? 'Eski TXT günlükleri ' + mib(logRetention.legacy_used_bytes) : '',
    ] : ['Arşiv bilgisi bekleniyor…'];
    usage.textContent = [...values, logRetentionNotice].filter(Boolean).join(' · ');
    document.getElementById('log-retention-save').disabled = !logRetention || logRetentionPending;
}

async function loadLogRetention(open) {
    if (!open || logRetentionPending || logHistory?.pending) return;
    logRetentionPending = true;
    logRetentionNotice = '';
    renderLogRetention();
    try {
        const result = await api.getLogArchive({ limit: 1 });
        if (!result.retention) throw new Error('retention');
        updateLogRetention(result.retention);
    } catch {
        logRetentionNotice = 'Disk kullanımı sorgulanamadı.';
    } finally {
        logRetentionPending = false;
        renderLogRetention();
    }
}

async function saveLogRetention(event) {
    event.preventDefault();
    if (!logRetention || logRetentionPending) return;
    const input = document.getElementById('log-retention-limit');
    if (!input.reportValidity()) return;
    const limit = input.valueAsNumber * LOG_MIB;
    if (!Number.isSafeInteger(limit) || limit === logRetention.limit_bytes) return;
    logRetentionPending = true;
    logRetentionNotice = '';
    renderLogRetention();
    try {
        if (limit < logRetention.limit_bytes && !await confirmDialog('Günlük saklama sınırını düşür',
            'Yeni sınıra ulaşmak için en eski arşiv kayıtları kalıcı olarak silinebilir.',
            { confirmLabel: 'Sınırı düşür', danger: true })) return;
        const result = await api.setLogRetention({ limit_bytes: limit });
        if (!result.success || !result.retention) throw new Error('retention');
        updateLogRetention(result.retention);
        logRetentionNotice = 'Saklama sınırı kaydedildi.';
        if (logHistory && !selectedLogIncident) changeLogFilters();
    } catch {
        logRetentionNotice = 'Saklama sınırı kaydedilemedi.';
    } finally {
        logRetentionPending = false;
        renderLogRetention();
    }
}

async function loadLogContextPage() {
    const context = logIncidentArchive;
    if (!context || context.pending) return;
    const at = logEventTime(context.anchor);
    context.pending = true;
    context.error = false;
    renderLogs();
    try {
        const response = await api.getLogArchive({
            limit: 200, before: context.next ?? undefined, from: at - LOG_CONTEXT_MS, to: at + LOG_CONTEXT_MS,
        });
        if (logIncidentArchive !== context) return;
        if (response?.available === false || !Array.isArray(response?.logs)) throw new Error('archive');
        updateLogRetention(response.retention);
        context.entries = mergeLogEntries(context.entries, response.logs);
        context.next = response.has_more && response.next_cursor !== context.next ? response.next_cursor : null;
    } catch {
        if (logIncidentArchive === context) context.error = true;
    } finally {
        if (logIncidentArchive === context) {
            context.pending = false;
            renderLogs();
        }
    }
}

function selectLogIncident(entry) {
    if (logEventTime(entry) === null) return;
    if (!selectedLogIncident) timelineScrollTop = document.getElementById('log-entries').scrollTop;
    selectedLogIncident = entry;
    logIncidentArchive = { anchor: entry, entries: [], next: null, pending: false, error: false };
    renderLogs();
    const row = logRows.get(logKey(entry))?.row;
    if (row) {
        row.open = true;
        row.scrollIntoView({ block: 'nearest' });
    }
    document.getElementById('log-context-close').focus();
    void loadLogContextPage();
}

function closeLogContext() {
    if (!selectedLogIncident) return;
    const key = logKey(selectedLogIncident);
    selectedLogIncident = null;
    logIncidentArchive = null;
    renderLogs();
    document.getElementById('log-entries').scrollTop = timelineScrollTop;
    const row = logRows.get(key)?.row;
    if (row?.isConnected) row.firstElementChild.focus({ preventScroll: true });
}

function scrollLogsToTail() {
    if (selectedLogIncident || logHistory || !document.getElementById('log-autoscroll').checked) return;
    const container = document.getElementById('log-entries');
    container.scrollTop = container.scrollHeight;
}

function createLogRow(entry, deviceName, previous) {
    const row = document.createElement('details');
    row.className = 'log-entry';
    row.open = previous?.open || false;
    const summary = document.createElement('summary');
    const safeLevel = Object.hasOwn(LOG_LEVELS, entry.level) ? entry.level : '';
    const sourceName = LOG_SOURCES[entry.source] || LOG_SOURCES.pc;
    const parts = {
        'log-time': logTime(entry),
        ['log-level ' + safeLevel]: LOG_LEVELS[entry.level] || entry.level,
        'log-source': deviceName ? sourceName + ' · ' + deviceName : sourceName,
        'log-stage': LOG_STAGES[logStage(entry)],
        'log-msg': entry.msg + (entry.count > 1 ? ' (×' + entry.count + ')' : '')
    };
    for (const [className, value] of Object.entries(parts)) {
        const span = document.createElement('span');
        span.className = className;
        span.textContent = value;
        summary.appendChild(span);
    }
    const detail = document.createElement('div');
    detail.className = 'log-detail';
    const causes = logCauses(entry);
    const lines = [entry.explanation || entry.msg,
        causes.length ? 'Olası nedenler · ' + causes.join(' · ') : '', ...logTimingLines(entry)];
    for (const text of lines.filter(Boolean)) {
        const line = document.createElement('p');
        line.textContent = text;
        detail.appendChild(line);
    }
    if (logEventTime(entry) !== null) {
        const context = document.createElement('button');
        context.type = 'button';
        context.className = 'btn btn-ghost btn-sm';
        context.textContent = 'Olayın 30 sn öncesi ve sonrası';
        context.addEventListener('click', () => selectLogIncident(entry));
        detail.appendChild(context);
    }
    const technical = [entry.code, logDetails(entry)].filter(Boolean).join('\n');
    if (technical) {
        const disclosure = document.createElement('details');
        disclosure.open = previous?.querySelector('.log-detail details')?.open || false;
        const label = document.createElement('summary');
        label.textContent = 'Teknik ayrıntılar';
        const raw = document.createElement('pre');
        raw.textContent = technical;
        disclosure.append(label, raw);
        detail.appendChild(disclosure);
    }
    row.append(summary, detail);
    return row;
}

function renderLogs() {
    const container = document.getElementById('log-entries');
    const page = logHistory?.logs || logCache.filter(isAfterLogClear);
    const diskContext = logIncidentArchive?.entries || [];
    const allLogs = mergeLogEntries(logHistory ? diskContext : diskContext.filter(isAfterLogClear), page);
    const knownDevices = logHistory ? [...logHistory.devices.values(), ...allLogs] : allLogs;
    const { devices, devicesByIp } = logDeviceOptions(knownDevices, document.getElementById('log-device'));
    const logFilters = readLogFilters();
    const incidentKey = selectedLogIncident ? logKey(selectedLogIncident) : null;
    const filters = document.getElementById('log-filters');
    const context = document.getElementById('log-context');
    const archivePages = document.getElementById('log-archive-pages');
    const archiveStatus = document.getElementById('log-archive-status');
    const historyMessage = logHistory && !selectedLogIncident ? logHistory.message : '';
    document.getElementById('log-mode').textContent = logHistory ? 'Canlı günlüğe dön' : 'Arşive göz at';
    document.getElementById('log-clear').disabled = Boolean(logHistory);
    document.getElementById('log-autoscroll').disabled = Boolean(logHistory || selectedLogIncident);
    archivePages.hidden = !logHistory || Boolean(selectedLogIncident);
    document.getElementById('log-archive-newer').disabled = !logHistory?.page || logHistory.pending;
    document.getElementById('log-archive-older').disabled = !logHistory?.next || logHistory.pending;
    if (archiveStatus.textContent !== historyMessage) archiveStatus.textContent = historyMessage;
    filters.disabled = Boolean(selectedLogIncident);
    context.hidden = !selectedLogIncident;
    if (selectedLogIncident) renderIncidentLogExport();
    if (selectedLogIncident) {
        const at = logEventTime(selectedLogIncident);
        const contextStatus = logIncidentArchive?.pending ? ' · Arşiv okunuyor…'
            : logIncidentArchive?.error ? ' · Arşiv eşleşmeleri alınamadı.' : '';
        const title = 'Olay bağlamı ±30 sn · ' + logTime(selectedLogIncident) + ' · ' + selectedLogIncident.msg + contextStatus;
        const contextTitle = document.getElementById('log-context-title');
        const more = document.getElementById('log-context-more');
        more.hidden = !logIncidentArchive?.next && !logIncidentArchive?.error;
        more.disabled = Boolean(logIncidentArchive?.pending);
        more.textContent = logIncidentArchive?.error ? 'Arşivi yeniden dene' : 'Daha eski bağlamı yükle';
        if (contextTitle.textContent !== title) contextTitle.textContent = title;
        visibleLogs = allLogs.filter(entry => {
            const time = logEventTime(entry);
            return time !== null && Math.abs(time - at) <= LOG_CONTEXT_MS;
        });
        if (!visibleLogs.some(entry => logKey(entry) === incidentKey)) visibleLogs.push(selectedLogIncident);
    } else {
        visibleLogs = filterLogs(page, logFilters, devicesByIp);
    }
    visibleLogs.sort((a, b) => (logEventTime(a) ?? 0) - (logEventTime(b) ?? 0) || logTimestamp(a) - logTimestamp(b));
    const followsTail = !logHistory && !selectedLogIncident && document.getElementById('log-autoscroll').checked
        && (container.children.length === 0 || container.scrollHeight - container.scrollTop - container.clientHeight <= 8)
        && !container.querySelector('.log-entry[open]');
    const retained = new Set(allLogs.map(logKey));
    if (selectedLogIncident) retained.add(incidentKey);
    const rows = visibleLogs.map(entry => {
        const key = logKey(entry);
        const deviceName = devices.get(logDevice(entry, devicesByIp)) || entry.device_name || entry.device_id;
        const revision = JSON.stringify([entry, deviceName]);
        let cached = logRows.get(key);
        if (cached?.revision !== revision) {
            cached = { revision, row: createLogRow(entry, deviceName, cached?.row) };
            logRows.set(key, cached);
        }
        const row = cached.row;
        row.classList.toggle('log-incident', Boolean(selectedLogIncident) && key === incidentKey);
        return row;
    });
    const desired = new Set(rows);
    for (const row of Array.from(container.childNodes)) {
        if (!desired.has(row)) row.remove();
    }
    let position = container.firstElementChild;
    for (const row of rows) {
        if (row === position) position = position.nextElementSibling;
        else container.insertBefore(row, position);
    }
    for (const key of logRows.keys()) {
        if (!retained.has(key)) logRows.delete(key);
    }
    if (visibleLogs.length === 0) {
        const empty = document.createElement('p');
        empty.className = 'list-empty';
        empty.textContent = logFilters.from > logFilters.to ? 'Başlangıç zamanı bitişten sonra olamaz.'
            : logHistory?.pending ? 'Arşiv okunuyor…'
                : page.length ? 'Filtrelere uyan kayıt yok.' : 'Gösterilecek kayıt yok.';
        container.appendChild(empty);
    }
    if (followsTail) container.scrollTop = container.scrollHeight;
}

function clearLogs() {
    if (logHistory) return;
    selectedLogIncident = null;
    logIncidentArchive = null;
    clearedLogTime = Date.now();
    for (const entry of logCache) {
        if (Number.isSafeInteger(entry.id)) clearedLogId = Math.max(clearedLogId ?? 0, entry.id);
    }
    renderLogs();
}

function downloadLogs() {
    const lines = visibleLogs.map(entry => formatLogEntry(entry)).join('\n\n');
    const url = URL.createObjectURL(new Blob([lines], { type: 'text/plain;charset=utf-8' }));
    const link = document.createElement('a');
    link.href = url;
    link.download = `luxedge_log_${new Date().toISOString().slice(0, 19).replace(/:/g, '-')}.txt`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 0);
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
