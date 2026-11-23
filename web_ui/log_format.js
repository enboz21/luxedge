(function (root) {
    const sources = { pc: 'PC', wemos: 'Wemos', windows: 'Windows', electron: 'Uygulama' };
    const levels = { DEBUG: 'Hata ayıklama', INFO: 'Bilgi', WARNING: 'Uyarı', ERROR: 'Hata', CRITICAL: 'Kritik' };
    const stages = {
        capture: 'Ekran yakalama', color: 'Renk hesaplama', frame: 'Kare işleme',
        udp_send: 'UDP LED gönderimi', connectivity: 'Bağlantı yoklaması',
        wemos_stream: 'Wemos LED akışı', windows_audit: 'Windows denetimi',
        application: 'Uygulama', other: 'Diğer',
    };
    const dateTime = new Intl.DateTimeFormat('tr-TR', {
        year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit', second: '2-digit', fractionalSecondDigits: 3,
    });
    const offsetTime = new Intl.DateTimeFormat('tr-TR', { timeZoneName: 'longOffset' });
    const duration = new Intl.NumberFormat('tr-TR', { maximumFractionDigits: 3 });

    function formatLocalTime(timestamp, withZone = false) {
        if (!Number.isFinite(timestamp)) return '';
        const local = dateTime.format(timestamp);
        if (!withZone) return local;
        const offset = offsetTime.formatToParts(timestamp).find(part => part.type === 'timeZoneName')?.value;
        return local + ' ' + (!offset || offset === 'GMT' ? 'GMT+00:00' : offset);
    }

    function estimatedTime(entry) {
        return entry.source === 'wemos' ? Date.parse(entry.details?.event_timestamp_estimated) : NaN;
    }

    function eventTime(entry) {
        const windowsTime = entry.source === 'windows' ? Date.parse(entry.details?.event_at) : NaN;
        const received = entry.source === 'wemos' ? Date.parse(entry.details?.received_at) : NaN;
        return [estimatedTime(entry), windowsTime, received, Date.parse(entry.timestamp)].find(Number.isFinite) ?? null;
    }

    function rowTime(entry, withZone = false) {
        const timestamp = eventTime(entry);
        if (timestamp === null) return entry.time || '';
        return formatLocalTime(timestamp, withZone) + (Number.isFinite(estimatedTime(entry)) ? ' (tahmini)' : '');
    }

    function stage(entry) {
        const code = typeof entry.code === 'string' ? entry.code : '';
        if (Object.hasOwn(stages, entry.details?.stage)) return entry.details.stage;
        const operation = typeof entry.details?.operation === 'string' ? entry.details.operation : '';
        if (operation === 'UDP LED' || code.startsWith('udp_send_')) return 'udp_send';
        if (operation.startsWith('UDP PING') || operation.startsWith('HTTP ')
                || /^(connectivity_|diagnostics_|wemos_|wifi_)/.test(code)) return 'connectivity';
        if (entry.source === 'wemos' && code.startsWith('stream_')) return 'wemos_stream';
        if (code.startsWith('frame_') || code.startsWith('low_fps')) return 'frame';
        if (entry.source === 'windows') return 'windows_audit';
        if (entry.source === 'electron' || code === 'pc_started') return 'application';
        return 'other';
    }

    function detailsText(entry) {
        if (typeof entry.details === 'string') return entry.details;
        return entry.details && Object.keys(entry.details).length ? JSON.stringify(entry.details, null, 2) : '';
    }

    function timingLines(entry, withZone = false) {
        const details = entry.details;
        if (!details || typeof details !== 'object') return [];
        const lines = [];
        const time = (label, value) => {
            const display = formatLocalTime(Date.parse(value), withZone);
            if (display) lines.push(label + ' · ' + display);
        };
        time('Tahmini olay zamanı', details.event_timestamp_estimated);
        time('PC alım zamanı', details.received_at);
        time('Windows olay zamanı', entry.source === 'windows' ? details.event_at : null);
        if (withZone) {
            time('Günlüğe yazılma zamanı', entry.timestamp);
            time('Deneme başlangıcı', details.attempt_started_at);
            time('Deneme bitişi', details.attempt_finished_at);
            time('İlk hata zamanı', details.first_error_at);
            time('Son hata zamanı', details.last_error_at);
            time('Son yerel UDP gönderimi', details.last_successful_send_at);
        }
        if (Number.isFinite(details.outage_ms) && details.outage_ms >= 0) {
            lines.push('Bağlantı kesintisi · ' + duration.format(details.outage_ms / 1000) + ' sn');
        }
        if (Number.isFinite(details.stream_gap_ms) && details.stream_gap_ms >= 0) {
            lines.push('LED verisi aralığı · ' + duration.format(details.stream_gap_ms) + ' ms');
        }
        if (entry.source === 'windows') {
            if (details.event_channel) {
                const channel = {
                    Security: 'Güvenlik',
                    'Microsoft-Windows-Windows Defender/Operational': 'Microsoft Defender Ağ Koruması'
                }[details.event_channel] || details.event_channel;
                const event = details.event_id ? ' · Olay ' + details.event_id : '';
                const record = details.event_record_id ? ' · Kayıt ' + details.event_record_id : '';
                lines.push('Windows günlüğü · ' + channel + event + record);
            }
            if (details.filter_origin) lines.push('WFP filtre kökeni · ' + details.filter_origin);
            const policy = details.system_audit_failure;
            if (policy && typeof policy === 'object') {
                const states = { enabled: 'açık', disabled: 'kapalı', access_denied: 'okuma izni yok', unavailable: 'durum alınamadı' };
                lines.push('Sistem hata denetimi · ' + Object.entries(policy)
                    .map(([id, state]) => id + ' ' + (states[state] || state))
                    .join(' · '));
            }
        }
        return lines;
    }

    function causes(entry) {
        if (Array.isArray(entry.possible_causes) && entry.possible_causes.length) return entry.possible_causes;
        return ['WARNING', 'ERROR', 'CRITICAL'].includes(entry.level) ? ['Nedeni belirlenemedi.'] : [];
    }

    function formatEntry(entry, deviceName) {
        const details = entry.details;
        const source = sources[entry.source] || sources.pc;
        const device = deviceName || entry.device_name || entry.device_id || details?.ip;
        const error = details?.last_error || details;
        const errorCodes = ['winerror', 'errno'].flatMap(key =>
            Number.isSafeInteger(error?.[key]) ? [key + ' ' + error[key]] : []);
        return [
            [rowTime(entry, true), levels[entry.level] || entry.level, source, device, entry.msg].filter(Boolean).join(' | '),
            'Aşama · ' + stages[stage(entry)],
            entry.count > 1 ? 'Tekrar sayısı · ' + entry.count : '',
            entry.explanation,
            error?.error_type ? 'Hata türü · ' + error.error_type : '',
            error?.error_message ? 'Hata iletisi · ' + error.error_message : '',
            errorCodes.length ? 'Hata kodu · ' + errorCodes.join(' · ') : '',
            ...causes(entry).map(cause => 'Olası neden · ' + cause),
            ...timingLines(entry, true),
            entry.code,
            detailsText(entry),
        ].filter(Boolean).join('\n');
    }

    const logFormat = {
        sources, levels, stages, formatLocalTime, eventTime, rowTime, stage,
        detailsText, timingLines, causes, formatEntry,
    };
    if (typeof module !== 'undefined') module.exports = logFormat;
    else root.LogFormat = logFormat;
})(globalThis);
