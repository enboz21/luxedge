// ============================================================
// LuxEdge - Preload Script (Context Bridge)
// Renderer process ile Main process arasındaki güvenli köprü
// ============================================================

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('luxedge', {
    // Durum
    getStatus: () => ipcRenderer.invoke('get-status'),

    // Ağ
    scanNetwork: () => ipcRenderer.invoke('scan-network'),

    // Ayarlar
    saveConfig: (config) => ipcRenderer.invoke('save-config', config),

    // Uygulama
    restartApp: () => ipcRenderer.invoke('restart-app'),

    // Wemos Kontrol
    restartWemos: () => ipcRenderer.invoke('wemos-restart'),
    toggleSleep: () => ipcRenderer.invoke('wemos-sleep'),
    resetWemosWifi: () => ipcRenderer.invoke('wemos-reset-wifi'),

    // Pencere Kontrolleri
    minimizeWindow: () => ipcRenderer.invoke('window-minimize'),
    maximizeWindow: () => ipcRenderer.invoke('window-maximize'),
    closeWindow: () => ipcRenderer.invoke('window-close')
});
