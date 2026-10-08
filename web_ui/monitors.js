(function (root) {
    function renderMonitorOptions(select, monitors, selected, document) {
        if (!select) return;
        const value = String(selected || select.value || 1);
        const options = monitors.map(monitor => {
            const option = document.createElement('option');
            option.value = String(monitor.index);
            option.textContent = `Monitör ${monitor.index}: ${monitor.name} (${monitor.width}x${monitor.height})${monitor.is_primary ? ' [Birincil]' : ''}`;
            return option;
        });
        if (!options.some(option => option.value === value)) {
            const missing = document.createElement('option');
            missing.value = value;
            missing.textContent = `Monitör ${value} (listede yok)`;
            options.push(missing);
        }
        select.replaceChildren(...options);
        select.value = value;
    }

    function createMonitorRefresh(api, apply, setBusy, notify) {
        let pending = false;
        return async () => {
            if (pending) return;
            pending = true;
            setBusy(true);
            try {
                const result = await api.refreshMonitors();
                if (!result.success) throw new Error(result.message || 'Monitör listesi güncellenemedi.');
                apply(result);
                notify('Monitör listesi güncellendi.', 'success');
            } catch (error) {
                notify(error.message, 'error');
            } finally {
                pending = false;
                setBusy(false);
            }
        };
    }
    const exported = { renderMonitorOptions, createMonitorRefresh };
    if (typeof module !== 'undefined') module.exports = exported;
    else Object.assign(root, exported);
})(globalThis);
