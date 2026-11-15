"""Fetch bounded ESP8266 diagnostic batches independently of LED streaming."""
import ipaddress
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.client import HTTPException

UNKNOWN = 'Nedeni belirlenemedi'
POLL_SECONDS = 5
BACKOFF_SECONDS = 30
BOOT_ID = re.compile(r'[0-9a-f]{16}')
UINT32_MAX = 2**32 - 1
WIFI_REASONS = {
    2: 'Wi-Fi kimlik doğrulamasının süresi doldu.',
    4: 'Erişim noktasıyla ilişkilendirmenin süresi doldu.',
    15: 'Wi-Fi anahtar anlaşması zaman aşımına uğradı.',
    200: 'Erişim noktasının Wi-Fi sinyal paketleri zamanında alınamadı.',
    201: 'Yapılandırılmış erişim noktası taramada bulunamadı.',
    202: 'Erişim noktasıyla Wi-Fi kimlik doğrulaması başarısız oldu.',
    203: 'Erişim noktasıyla ilişkilendirme başarısız oldu.',
    204: 'Wi-Fi anahtar anlaşması zaman aşımına uğradı.',
}
RESET_REASONS = {
    0: 'ESP8266 normal açılış bildirdi.',
    1: 'ESP8266 donanım watchdog reseti bildirdi.',
    2: 'ESP8266 yazılım istisnası sonrası reset bildirdi.',
    3: 'ESP8266 yazılım watchdog reseti bildirdi.',
    4: 'ESP8266 yazılımla yeniden başlatma bildirdi.',
    5: 'ESP8266 derin uykudan uyanma bildirdi.',
    6: 'ESP8266 harici reset bildirdi.',
}

# code, level, message, explanation
EVENTS = {
    1: ('boot', logging.INFO, 'Wemos açıldı', 'ESP8266 açılışı kaydedildi.'),
    2: ('wifi_disconnected', logging.WARNING, 'Wemos Wi-Fi bağlantısı kesildi', 'ESP8266 Wi-Fi bağlantısının kesildiğini bildirdi.'),
    3: ('wifi_ready', logging.INFO, 'Wemos Wi-Fi üzerinden IP aldı', 'Wi-Fi bağlantısı sonrasında IP adresi hazır.'),
    4: ('wifi_retry', logging.INFO, 'Wemos Wi-Fi bağlantısını yeniden deniyor', 'Firmware bağlantıyı yeniden kurmayı denedi.'),
    5: ('hotspot', logging.INFO, 'Wemos kurulum hotspot moduna geçti', 'Cihaz kurulum ağına geçti.'),
    6: ('udp_bound', logging.INFO, 'Wemos UDP dinleyicisini başlattı', 'UDP dinleyicisi yerel porta bağlandı.'),
    7: ('udp_bind_failed', logging.ERROR, 'Wemos UDP dinleyicisini başlatamadı', 'UDP soketi yerel porta bağlanamadı.'),
    8: ('udp_reply_failed', logging.WARNING, 'Wemos UDP yanıtını gönderemedi', 'PING veya keşif yanıtı gönderilirken UDP işlemi başarısız oldu.'),
    9: ('udp_invalid', logging.WARNING, 'Wemos geçersiz UDP paketi aldı', 'Paket beklendiği biçimde okunamadı veya tanınmadı.'),
    10: ('stream_udp_retry', logging.INFO, 'Wemos akış sessizliği sonrası UDP dinleyicisini yeniliyor', 'Son LED paketinden sonra 5 saniye boyunca yeni LED verisi gelmedi.'),
    11: ('stream_wifi_retry', logging.INFO, 'Wemos akış sessizliği sonrası Wi-Fi bağlantısını yeniliyor', 'Son LED paketinden sonra 20 saniye boyunca yeni LED verisi gelmedi.'),
    12: ('stream_restart', logging.INFO, 'Wemos akış sessizliği sonrası yeniden başlatılıyor', 'Son LED paketinden sonra 60 saniye boyunca yeni LED verisi gelmedi.'),
    13: ('restart_requested', logging.INFO, 'Wemos yeniden başlatması istendi', 'Cihazın HTTP işlemi yeniden başlatma istedi.'),
    14: ('ota_failed', logging.ERROR, 'Wemos OTA güncellemesi başarısız oldu', 'ArduinoOTA hata kodu bildirdi.'),
    15: ('wifi_connect_failed', logging.WARNING, 'Wemos Wi-Fi bağlantısı kuramadı', 'Cihaz bağlantı denemeleri sonrasında kurulum ağına geçti.'),
    16: ('stream_resumed', logging.INFO, 'Wemos LED akışı geri geldi', 'Akış kurtarma adımından sonra geçerli LED verisi alındı.'),
    17: ('hotspot_failed', logging.ERROR, 'Wemos kurulum hotspot ağını başlatamadı', 'ESP8266 kurulum ağı yapılandırma veya başlatma hatası bildirdi.'),
}


class InvalidDiagnostics(ValueError):
    pass


def read_json(ip, path, limit):
    url = f'http://{ip}{path}'
    with urllib.request.urlopen(url, timeout=1.5) as response:
        if response.headers.get_content_type() != 'application/json':
            raise InvalidDiagnostics('Beklenmeyen içerik türü')
        body = response.read(limit + 1)
        if len(body) > limit:
            raise InvalidDiagnostics('Yanıt boyut sınırını aştı')
    data = json.loads(body)
    if not isinstance(data, dict):
        raise InvalidDiagnostics('Beklenmeyen tanılama biçimi')
    return data


def validated_batch(batch, cursor_boot, cursor_seq):
    boot = batch.get('boot_id')
    if type(batch.get('schema')) is not int or batch['schema'] != 1 or not isinstance(boot, str) or not BOOT_ID.fullmatch(boot):
        raise InvalidDiagnostics('Desteklenmeyen tanılama sürümü')
    keys = ('uptime_ms', 'oldest_seq', 'latest_seq', 'next_seq', 'lost', 'reset_reason')
    if any(type(batch.get(key)) is not int or not 0 <= batch[key] <= UINT32_MAX for key in keys):
        raise InvalidDiagnostics('Geçersiz tanılama imleci')
    oldest, latest = batch['oldest_seq'], batch['latest_seq']
    since = cursor_seq if boot == cursor_boot else 0
    if not 1 <= oldest <= latest or latest - oldest >= 32 or since > latest:
        raise InvalidDiagnostics('Geçersiz olay aralığı')
    start = max(oldest, since + 1)
    end = min(latest, start + 7)
    rows = batch.get('events')
    if (not isinstance(rows, list) or len(rows) != end - start + 1
            or batch['next_seq'] != end or batch['lost'] != max(0, oldest - since - 1)):
        raise InvalidDiagnostics('Tutarsız olay aralığı')
    for seq, row in enumerate(rows, start):
        if (not isinstance(row, dict) or any(type(row.get(key)) is not int or
                not 0 <= row[key] <= UINT32_MAX for key in ('seq', 'uptime_ms', 'code', 'arg'))
                or row['seq'] != seq):
            raise InvalidDiagnostics('Geçersiz olay kaydı')
    return boot, rows


@dataclass
class DevicePoll:
    boot: str = ''
    seq: int = 0
    next_poll: float = 0
    connected: bool | None = None
    supported: bool = False
    unsupported_reported: bool = False
    fault: str = ''
    ip: str = ''
    gap_boot: str = ''
    gap_until: int = 0
    delivery_key: tuple = ()
    delivery: dict = field(default_factory=dict)


class WemosDiagnostics:
    def __init__(self, log, log_history, devices):
        self.log = log
        self.devices = devices
        self.states = {}
        self.saved = {}
        for row in log_history:
            details = row.get('details')
            device_id = row.get('device_id')
            if row.get('source') == 'wemos' and isinstance(device_id, str) and isinstance(details, dict):
                boot, seq = details.get('boot_id'), details.get('seq')
                if isinstance(boot, str) and BOOT_ID.fullmatch(boot) and type(seq) is int and 1 <= seq <= UINT32_MAX:
                    self.saved[device_id] = boot, seq

    def _log(self, device, code, level, message, explanation, causes=(), details=None, count=1,
             source='wemos', delivery=None):
        self.log.log(level, message, extra={
            'source': source, 'code': code, 'device_id': device['id'], 'device_name': device['name'],
            'explanation': explanation, 'possible_causes': list(causes), 'count': count,
            'details': {'ip': device['ip'], **(details or {})},
            'delivery': delivery,
        })
        return delivery is None or delivery.get('state') == 'saved'

    @staticmethod
    def _delivery(state, key):
        if state.delivery_key != key:
            state.delivery_key = key
            state.delivery = {}
        return state.delivery

    def _event(self, device, state, boot, row, batch_uptime, received):
        number, arg = row['code'], row['arg']
        code, level, message, explanation = EVENTS.get(number, (
            'unknown_wemos_event', logging.INFO, 'Wemos tanılama olayı', UNKNOWN,
        ))
        causes = []
        if number == 1:
            explanation = RESET_REASONS.get(arg, UNKNOWN)
        elif number in (2, 15):
            causes = [WIFI_REASONS.get(arg, UNKNOWN)]
        elif number in (7, 8, 9, 14, 17):
            causes = [UNKNOWN]
        age_ms = (batch_uptime - row['uptime_ms']) & UINT32_MAX
        estimate = received - timedelta(milliseconds=age_ms)
        details = {
            'boot_id': boot, 'seq': row['seq'], 'uptime_ms': row['uptime_ms'], 'arg': arg,
            'event_code': number,
            'event_timestamp_estimated': estimate.isoformat(timespec='milliseconds'),
            'received_at': received.isoformat(timespec='milliseconds'),
        }
        if number == 3 and arg:
            message = 'Wemos Wi-Fi bağlantısı geri geldi'
            details['outage_ms'] = arg
        saved = self._log(
            device, code, level, message, explanation, causes, details,
            count=max(1, arg) if number in (8, 9) else 1,
            delivery=self._delivery(state, (boot, row['seq'])),
        )
        if saved:
            state.boot, state.seq = boot, row['seq']
        return saved

    def _poll(self, device, state):
        try:
            ip = str(ipaddress.IPv4Address(device['ip']))
        except ValueError:
            return
        try:
            if not state.supported:
                status = read_json(ip, '/status', 4096)
                if status.get('diagnostics_version') != 1:
                    self._reached(device, state)
                    if not state.unsupported_reported:
                        self._log(device, 'diagnostics_unsupported', logging.INFO,
                                  'Wemos firmware sürümü olay kaydı paylaşmıyor',
                                  'Cihazın durum servisi çalışıyor ancak tanılama desteği bildirmiyor.', source='pc')
                        state.unsupported_reported = True
                    state.next_poll = time.monotonic() + BACKOFF_SECONDS
                    return
            query = urllib.parse.urlencode({'boot': state.boot, 'after': state.seq})
            batch = read_json(ip, '/diagnostics?' + query, 4096)
            received = datetime.now(timezone.utc)
            boot, rows = validated_batch(batch, state.boot, state.seq)
            self._reached(device, state)
            state.supported = True
            state.unsupported_reported = False
            if state.boot != boot and batch['oldest_seq'] > 1:
                if not self._event(device, state, boot, {
                    'code': 1, 'arg': batch['reset_reason'], 'seq': 1, 'uptime_ms': 0,
                }, batch['uptime_ms'], received):
                    return
            if state.gap_boot != boot:
                state.gap_boot, state.gap_until = boot, 0
            gap_until = batch['oldest_seq'] - 1
            lost = gap_until - max(state.seq, state.gap_until)
            if batch['lost'] and lost > 0:
                delivery = self._delivery(state, (boot, 'gap'))
                sent_until = delivery.setdefault('gap_until', gap_until)
                if not self._log(device, 'history_gap', logging.WARNING, 'Bazı Wemos kayıtları alınamadı',
                                 'Cihazın tanılama tamponunda eski kayıtların üzerine yazıldı.',
                                 details={'boot_id': boot, 'gap_until': sent_until},
                                 count=sent_until - max(state.seq, state.gap_until), delivery=delivery):
                    return
                state.gap_until = sent_until
                state.delivery_key = ()
                if sent_until != gap_until:
                    return
            for row in rows:
                if not self._event(device, state, boot, row, batch['uptime_ms'], received):
                    return
            # A successful batch with events still queued gets priority on the next cycle.
            if batch['next_seq'] < batch['latest_seq']:
                state.next_poll = time.monotonic() + 1
        except urllib.error.HTTPError as error:
            with error:
                if error.code == 404 and state.supported:
                    state.supported = False
                    state.next_poll = time.monotonic() + BACKOFF_SECONDS
                    return
            self._fault(device, state, 'diagnostics_invalid', 'Wemos tanılama yanıtı alınamadı',
                        'Wemos HTTP servisi hata döndürdü.')
        except (ValueError, RecursionError):
            state.supported = False
            self._fault(device, state, 'diagnostics_invalid', 'Wemos tanılama yanıtı geçersiz',
                        'Wemos yanıtı beklenen biçimde okunamadı.')
        except (OSError, HTTPException):
            state.connected = False
            self._fault(device, state, 'wemos_unreachable', 'PC, Wemos HTTP servisine erişemedi',
                        'Cihaz bu tanılama sorgusuna yanıt vermedi.')

    def _fault(self, device, state, code, message, explanation):
        state.next_poll = time.monotonic() + BACKOFF_SECONDS
        if state.fault != code:
            self._log(device, code, logging.WARNING, message, explanation, [UNKNOWN], source='pc')
            state.fault = code

    def _reached(self, device, state):
        if state.connected is False:
            self._log(device, 'wemos_reachable', logging.INFO, 'PC, Wemos HTTP servisine yeniden ulaştı',
                      'Cihaz tanılama sorgusuna yeniden yanıt verdi.', source='pc')
        state.connected = True
        state.fault = ''

    def run(self, stop):
        devices = []
        next_refresh = 0
        with ThreadPoolExecutor(max_workers=4) as pool:
            while not stop.is_set():
                now = time.monotonic()
                if now >= next_refresh:
                    devices = self.devices()
                    keys = {device['id'] for device in devices}
                    self.states = {key: state for key, state in self.states.items() if key in keys}
                    next_refresh = now + POLL_SECONDS
                pending = []
                for device in devices:
                    key = device['id']
                    if key not in self.states:
                        boot, seq = self.saved.pop(key, ('', 0))
                        self.states[key] = DevicePoll(boot, seq)
                    state = self.states[key]
                    if state.ip != device['ip']:
                        state.ip = device['ip']
                        state.next_poll = 0
                        state.connected = None
                        state.supported = False
                        state.unsupported_reported = False
                        state.fault = ''
                    if now >= state.next_poll:
                        state.next_poll = now + POLL_SECONDS
                        pending.append((device, state, pool.submit(self._poll, device, state)))
                for device, state, job in pending:
                    try:
                        job.result()
                    except Exception:
                        self._fault(device, state, 'diagnostics_poll_failed',
                                    'PC tanılama sorgusu tamamlanamadı',
                                    'Tanılama toplayıcısı sorguyu işlerken hata oluştu.')
                stop.wait(1)
