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

    // Pasif çoklu-Wemos cihaz kayıtları
    getDevices: () => ipcRenderer.invoke('get-devices'),
    scanDevices: () => ipcRenderer.invoke('scan-devices'),
    validateDevices: () => ipcRenderer.invoke('validate-devices'),
    setMultiDeviceMode: (enabled) => ipcRenderer.invoke('set-multi-device-mode', enabled),
    createDevice: (device) => ipcRenderer.invoke('create-device', device),
    updateDevice: (id, device) => ipcRenderer.invoke('update-device', id, device),
    deleteDevice: (id) => ipcRenderer.invoke('delete-device', id),

    // Uygulama
    restartApp: () => ipcRenderer.invoke('restart-app'),

    // Wemos Kontrol
    restartWemos: () => ipcRenderer.invoke('wemos-restart'),
    toggleSleep: () => ipcRenderer.invoke('wemos-sleep'),
    resetWemosWifi: () => ipcRenderer.invoke('wemos-reset-wifi'),
    testWemosConnection: () => ipcRenderer.invoke('test-wemos-connection'),

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
