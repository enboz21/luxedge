// ============================================================
// LuxEdge - Preload Script (Context Bridge)
// Renderer process ile Main process arasındaki güvenli köprü
// ============================================================

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('luxedge', {
    // Durum
    getStatus: () => ipcRenderer.invoke('get-status'),
    refreshMonitors: () => ipcRenderer.invoke('refresh-monitors'),

    // Ağ
    scanNetwork: () => ipcRenderer.invoke('scan-network'),

    // Ayarlar
    saveConfig: (config) => ipcRenderer.invoke('save-config', config),
    getLogs: () => ipcRenderer.invoke('get-logs'),
    getLogArchive: query => ipcRenderer.invoke('get-log-archive', query),
    setLogRetention: settings => ipcRenderer.invoke('set-log-retention', settings),
    getLiveLogFileState: () => ipcRenderer.invoke('get-live-log-file-state'),
    startLiveLogFile: () => ipcRenderer.invoke('start-live-log-file'),
    stopLiveLogFile: () => ipcRenderer.invoke('stop-live-log-file'),
    saveIncidentLog: range => ipcRenderer.invoke('save-incident-log', range),
    getWindowsDiagnostics: () => ipcRenderer.invoke('windows-diagnostics-status'),
    startWindowsDiagnostics: () => ipcRenderer.invoke('windows-diagnostics-start'),
    stopWindowsDiagnostics: () => ipcRenderer.invoke('windows-diagnostics-stop'),

    // Pasif çoklu-Wemos cihaz kayıtları
    getDevices: () => ipcRenderer.invoke('get-devices'),
    getDevicesOverview: () => ipcRenderer.invoke('get-devices-overview'),
    scanDevices: () => ipcRenderer.invoke('scan-devices'),
    validateDevices: () => ipcRenderer.invoke('validate-devices'),
    setMultiDeviceMode: (enabled) => ipcRenderer.invoke('set-multi-device-mode', enabled),
    createDevice: (device) => ipcRenderer.invoke('create-device', device),
    updateDevice: (id, device) => ipcRenderer.invoke('update-device', id, device),
    deleteDevice: (id) => ipcRenderer.invoke('delete-device', id),
    syncDevice: (id, options) => ipcRenderer.invoke('sync-device', id, options),

    // Uygulama
    restartApp: () => ipcRenderer.invoke('restart-app'),

    // Wemos Kontrol
    restartWemos: (target) => ipcRenderer.invoke('wemos-restart', target),
    toggleSleep: (target) => ipcRenderer.invoke('wemos-sleep', target),
    resetWemosWifi: (target) => ipcRenderer.invoke('wemos-reset-wifi', target),
    testWemosConnection: (target) => ipcRenderer.invoke('test-wemos-connection', target),
    getWemosStatus: (target) => ipcRenderer.invoke('get-wemos-status', target),

    // Otomatik Başlatma
    getAutoStart: () => ipcRenderer.invoke('get-autostart'),
    setAutoStart: (enabled) => ipcRenderer.invoke('set-autostart', enabled),

    onWindowVisibility: callback => {
        const listener = (_event, visible) => callback(visible);
        ipcRenderer.on('window-visibility', listener);
        return () => ipcRenderer.removeListener('window-visibility', listener);
    },

    // Pencere Kontrolleri
    minimizeWindow: () => ipcRenderer.invoke('window-minimize'),
    maximizeWindow: () => ipcRenderer.invoke('window-maximize'),
    closeWindow: () => ipcRenderer.invoke('window-close')
});
