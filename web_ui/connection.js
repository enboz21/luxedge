(function (root) {
    const states = {
        connected: ['Bağlı', 'active', '--success-color'],
        http_only: ['HTTP ile erişiliyor (UDP doğrulanamadı)', 'warning', '--warning-color'],
        checking: ['Kontrol ediliyor', 'checking', '--accent-color'],
        disconnected: ['Bağlantı yok', '', '--danger-color'],
        backend_unavailable: ['Arka plan hizmetine erişilemiyor', '', '--danger-color'],
        stopped: ['Sistem durduruldu', 'neutral', '--text-muted'],
        unconfigured: ['IP ayarlanmamış', 'neutral', '--text-muted'],
    };

    function renderConnection(els, status) {
        const state = status.connectivity?.state;
        // An old or malformed backend must never turn FPS into connection evidence.
        const [text, className, color] = states[state] || states.backend_unavailable;
        els.statusText.textContent = text;
        els.statusDot.className = `status-dot ${className}`.trim();
        els.connStatus.textContent = text;
        els.connStatus.style.color = `var(${color})`;
    }

    if (typeof module !== 'undefined') module.exports = { renderConnection };
    else root.renderConnection = renderConnection;
})(globalThis);
