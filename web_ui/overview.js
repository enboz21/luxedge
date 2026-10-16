(function (root) {
    const names = { stream: 'Ekran akışı', solid: 'Sabit renk', off: 'Kapalı', breathe: 'Nefes alma',
        rainbow: 'Gökkuşağı', chase: 'Kovalama', twinkle: 'Parıltı' };
    const hex = rgb => '#' + rgb.map(v => Math.max(0, Math.min(255, Math.round(v))).toString(16).padStart(2, '0')).join('').toUpperCase();
    function describe(device, multi, now = Date.now() / 1000) {
        const fresh = device.live && now - device.live.checked_at < 15;
        const online = fresh ? device.live.reachable : null;
        const status = fresh && online ? device.live.status : null;
        const frame = device.frame && now - device.frame.updated_at < 3 ? device.frame : null;
        const sleep = status?.sleep_mode === true;
        // Firmware settings win over saved settings, which may not have been applied.
        const strips = status?.strips?.filter(s => s.enabled !== false && Number(s.led_count) > 0);
        const sources = strips?.length ? strips : [{ index: 1, mode: status ? 'stream' : 'unknown' }];
        const rows = sources.map(strip => {
            // Pre-segment firmware reports physical strips without mode fields;
            // every enabled strip consumes the legacy PC RGB stream.
            const mode = names[strip.mode] ? strip.mode
                : strip.mode == null && status && !status.segments_supported ? 'stream' : 'unknown';
            const stopped = sleep || mode === 'off' || (mode !== 'stream' && strip.brightness === 0);
            const pcEnabled = multi ? device.enabled : device.id === 'legacy-primary';
            const state = stopped ? 'off' : online === true && mode !== 'unknown'
                && (mode !== 'stream' || (pcEnabled && frame)) ? 'active' : 'paused';
            let colors = [];
            if (stopped) colors = [[0, 0, 0]];
            else if (mode === 'stream' && frame) colors = frame.streams?.[String(strip.index)] || frame.colors;
            else if (mode !== 'stream' && /^#?[\da-f]{6}$/i.test(strip.color || '')) {
                const value = strip.color.replace('#', '');
                colors = [[0, 2, 4].map(i => parseInt(value.slice(i, i + 2), 16) * (strip.brightness ?? 255) / 255)];
            }
            const code = colors.length ? hex(colors[0]) : null;
            return { index: strip.index, mode, state, colors, code, speed: strip.speed ?? 50,
                name: mode === 'stream' && frame?.mode === 'solid' ? 'Bekleme rengi' : names[mode] || 'Bilgi bekleniyor',
                note: stopped ? 'LED çıkışı kapalı' : mode === 'unknown' ? 'Güncel efekt verisi yok' : mode === 'stream'
                    ? frame ? 'Son gönderilen RGB örnekleri' : 'Güncel akış verisi yok'
                    : mode === 'solid' ? 'Cihazın bildirdiği renk ve parlaklık' : 'Temsili animasyon · temel renk' };
        });
        return { online, rows, state: rows.some(r => r.state === 'active') ? 'active'
            : rows.every(r => r.state === 'off') ? 'off' : 'paused',
            note: online === null ? 'Durum sorgulanıyor; güncel cihaz verisi bekleniyor.'
                : online === false ? 'Cihaza ulaşılamıyor; önizleme duraklatıldı.'
                : sleep ? 'Cihaz uyku modunda.'
                : !multi && device.id !== 'legacy-primary' ? 'Çoklu cihaz modu kapalı; yerel efektler cihaz üzerinde çalışabilir.'
                : !device.enabled ? 'PC akışı pasif; yerel efektler cihaz üzerinde çalışabilir.' : '' };
    }
    const stateNames = { active: 'Aktif', paused: 'Duraklatılmış', off: 'Kapalı' };
    function element(doc, tag, className, text) {
        const el = doc.createElement(tag); el.className = className;
        if (text !== undefined) el.textContent = text;
        return el;
    }
    function render(container, devices, multi, openDevice, now) {
        const doc = container.ownerDocument;
        const signature = JSON.stringify([devices, multi]);
        // Preserve animation phase and keyboard focus between identical polls.
        if (container.dataset.signature === signature && Date.now() - Number(container.dataset.renderedAt) < 3000) return;
        container.dataset.signature = signature; container.dataset.renderedAt = Date.now();
        const focusedId = container.contains(doc.activeElement)
            ? doc.activeElement.closest('.device-overview')?.dataset.deviceId : null;
        const cards = devices.map(device => {
            const model = describe(device, multi, now);
            const card = element(doc, 'article', 'card device-overview');
            card.dataset.deviceId = device.id;
            const head = element(doc, 'div', 'card-head');
            head.append(element(doc, 'h3', '', device.name || device.ip || 'İsimsiz cihaz'),
                element(doc, 'span', `chip ${model.online === true ? 'ok' : model.online === false ? 'bad' : 'neutral'}`,
                    model.online === true ? 'Çevrim içi' : model.online === false ? 'Çevrim dışı' : 'Kontrol ediliyor'));
            card.append(head, element(doc, 'p', 'hint device-address', `${device.ip || 'IP bekleniyor'}:${device.port || 7777}`));
            card.append(element(doc, 'div', `effect-state ${model.state}`, `Animasyon durumu: ${stateNames[model.state]}`));
            model.rows.forEach(row => {
                const section = element(doc, 'div', 'overview-strip');
                section.append(element(doc, 'div', 'label-row', `Şerit ${row.index} · Aktif animasyon: ${row.name} · ${stateNames[row.state]}`));
                const preview = element(doc, 'div', `effect-preview effect-${row.mode} ${row.state}`);
                preview.setAttribute('role', 'img');
                preview.setAttribute('aria-label', `${row.name}: ${stateNames[row.state]}. ${row.note}`);
                preview.style.setProperty('--effect-color', row.code || '#333333');
                preview.style.setProperty('--effect-duration', `${(110 - row.speed) / 22}s`);
                // Align the simulated phase to wall time across dashboard refreshes.
                preview.style.setProperty('--effect-phase', `-${Date.now() / 1000 % ((110 - row.speed) / 22)}s`);
                for (let i = 0; i < 16; i++) {
                    const led = element(doc, 'span', 'effect-led');
                    const rgb = row.colors[Math.floor(i * row.colors.length / 16)];
                    led.style.backgroundColor = rgb ? hex(rgb) : '#333333';
                    led.style.setProperty('--led-index', i);
                    preview.appendChild(led);
                }
                const color = element(doc, 'div', 'overview-color');
                const swatch = element(doc, 'span', 'strip-swatch');
                swatch.style.background = row.mode === 'rainbow' && row.state !== 'off'
                    ? 'linear-gradient(90deg, red, yellow, lime, cyan, blue, magenta)' : row.code || '#333333';
                const rgb = row.colors[0];
                color.append(swatch, element(doc, 'code', '', row.mode === 'rainbow' && row.state !== 'off' ? 'Aktif renk: Değişken · RGB spektrumu'
                    : row.code ? `Aktif renk: ${row.code} · RGB(${rgb.map(Math.round).join(', ')})` : 'Aktif renk: Veri bekleniyor'));
                section.append(preview, color, element(doc, 'p', 'hint', row.note));
                card.appendChild(section);
            });
            if (model.note) card.append(element(doc, 'p', 'hint', model.note));
            const button = element(doc, 'button', 'btn btn-ghost btn-sm', 'Cihazı yönet');
            button.type = 'button'; button.addEventListener('click', () => openDevice(device));
            card.appendChild(button);
            return card;
        });
        if (!cards.length) cards.push(element(doc, 'div', 'list-empty', 'Kayıtlı cihaz yok. Cihazlar sayfasından cihaz ekleyin.'));
        container.replaceChildren(...cards);
        if (focusedId) cards.find(card => card.dataset.deviceId === focusedId)?.querySelector('button')?.focus();
    }
    const api = { describe, render, hex };
    if (typeof module !== 'undefined') module.exports = api;
    else root.DeviceDashboard = api;
})(globalThis);
