import socket
import numpy as np
from mss import mss
from PIL import Image
import time
import json
import os
import sys
import io
import copy
import ipaddress
import uuid
import logging
import logging.handlers
from collections import deque
from frame_processing import FrameCapture, edge_colors, edge_rgb, pack_rgb, solid_frame, frame_delay
from monitor_catalog import MonitorCatalog
from connectivity import Connectivity, legacy_connection

monitor_catalog = MonitorCatalog()
BACKEND_PORT = int(os.environ.get('LUXEDGE_BACKEND_PORT', '8888'))

# Windows konsolunda emoji/Unicode desteği için encoding'i UTF-8'e ayarla.
# PyInstaller --noconsole çalıştırmalarında stdout/stderr None olabilir.
if sys.stdout is not None and hasattr(sys.stdout, 'buffer') and sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
if sys.stderr is not None and hasattr(sys.stderr, 'buffer') and sys.stderr.encoding != 'utf-8':
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
import winreg
import ctypes
import threading
import urllib.request
import urllib.parse
import subprocess
import re
from pathlib import Path
from http.server import HTTPServer, SimpleHTTPRequestHandler
import http.server
import struct

# ============================================================
# LOG SİSTEMİ
# ============================================================

# Bellek içi log tamponu (son 200 satır)
_log_buffer = deque(maxlen=200)
_log_buffer_lock = threading.Lock()

class _MemoryLogHandler(logging.Handler):
    """Logları hem belleğe hem de print'e yazar"""
    def emit(self, record):
        try:
            msg = self.format(record)
            entry = {
                "time": time.strftime('%H:%M:%S', time.localtime(record.created)),
                "level": record.levelname,
                "msg": record.getMessage()
            }
            with _log_buffer_lock:
                _log_buffer.append(entry)
            # Konsola da yaz
            print(msg, flush=True)
        except Exception:
            pass

def _get_log_dir():
    """Log dosyası dizinini döndürür"""
    return os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'LuxEdge')

def setup_logger():
    """Logger'ı rotating dosya + bellek handler ile kur"""
    log_dir = _get_log_dir()
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, 'luxedge.log')

    logger = logging.getLogger('luxedge')
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    if logger.handlers:
        return logger  # Zaten kurulmuş

    fmt = logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

    # Rotating file handler (500 KB, 3 yedek)
    try:
        fh = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=512*1024, backupCount=3, encoding='utf-8'
        )
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception:
        pass

    # Bellek + konsol handler
    mh = _MemoryLogHandler()
    mh.setFormatter(logging.Formatter('[%(levelname)s] %(message)s'))
    logger.addHandler(mh)

    return logger

# Global logger
log = setup_logger()

# Windows'ta subprocess çağrılarında CMD penceresi açılmasını engelle
CREATE_NO_WINDOW = 0x08000000

def _subprocess_kwargs():
    """Platform'a uygun subprocess parametreleri döndürür"""
    kwargs = {
        'capture_output': True,
        'text': True,
        'errors': 'ignore',
        'stdin': subprocess.DEVNULL
    }
    kwargs['creationflags'] = CREATE_NO_WINDOW
    return kwargs

try:
    import pystray
    from pystray import MenuItem as item
    PYSTRAY_AVAILABLE = True
except ImportError:
    PYSTRAY_AVAILABLE = False

# ============================================================
# KONFIGÜRASYON YÖNETİMİ
# ============================================================

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ambilight_config.json")

# Çoklu cihaz geçişi için config şeması. Bu aşamada worker hâlâ eski tekli
# alanları kullanır; yeni şema yalnız sonraki API/UI adımlarına hazırlıktır.
MULTI_DEVICE_CONFIG_SCHEMA_VERSION = 2
LEGACY_PRIMARY_DEVICE_ID = "legacy-primary"

import shutil

def get_config_path():
    """EXE çalışırken config dosyasının yolunu döndürür (Önceki ayarları kaybetmeden taşıyarak)"""
    # 1. Eski sorunlu (Permission Denied alan) system dizini lokasyonu
    if getattr(sys, 'frozen', False):
        old_path = os.path.join(os.path.dirname(sys.executable), "ambilight_config.json")
    else:
        old_path = CONFIG_FILE
        
    # 2. Yeni Kullanıcıya özel sorunsuz dizin
    config_dir = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'LuxEdge')
    
    os.makedirs(config_dir, exist_ok=True)
    new_path = os.path.join(config_dir, "ambilight_config.json")
    
    # 3. Eğer kullanıcı eski ayarlarını taşıyorsa ve yeni dosya henüz yoksa, ESKİSİNİ KOPYALA ki ledleri sıfırlanmasın!
    if not os.path.exists(new_path) and os.path.exists(old_path):
        try:
            shutil.copy2(old_path, new_path)
        except Exception:
            pass
            
    return new_path

def load_config():
    """Konfigürasyon dosyasını yükler (bozuksa yedekten)"""
    config_path = get_config_path()
    if os.path.exists(config_path):
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"[HATA] Konfigürasyon dosyası okunamadı: {e}")
            # Yedekten okumayı dene
            backup_path = config_path + '.bak'
            if os.path.exists(backup_path):
                try:
                    print("[BİLGİ] Yedek konfigürasyon dosyası okunuyor...")
                    with open(backup_path, 'r', encoding='utf-8') as f:
                        config = json.load(f)
                    # Yedekten başarılı okuduysa, ana dosyayı düzelt
                    shutil.copy2(backup_path, config_path)
                    print("[BİLGİ] Yedek konfigürasyon dosyası geri yüklendi.")
                    return config
                except Exception:
                    pass
            return None
    return None

def save_config(config):
    """Konfigürasyon dosyasını atomik olarak kaydeder (yedek alarak)"""
    config_path = get_config_path()
    tmp_path = config_path + '.tmp'
    try:
        # Mevcut config'i yedekle
        if os.path.exists(config_path):
            backup_path = config_path + '.bak'
            try:
                shutil.copy2(config_path, backup_path)
            except Exception:
                pass

        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
        # Atomik replace (Windows/POSIX uyumlu)
        os.replace(tmp_path, config_path)
        return True
    except Exception as e:
        print(f"[HATA] Konfigürasyon dosyası kaydedilemedi: {e}")
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        return False


def legacy_config_to_primary_device(config):
    """Tekli config'i geçici olarak ilk çoklu-cihaz kaydına dönüştür.

    Bu fonksiyon dosya yazmaz ve aldığı config'i değiştirmez. Böylece sonraki
    geçiş adımları eski kurulumları kesintisiz okuyabilir.
    """
    if not isinstance(config, dict):
        return None

    ip = str(config.get("wemos_ip", "")).strip()
    if not ip:
        return None

    return {
        "id": LEGACY_PRIMARY_DEVICE_ID,
        "name": "Ana Wemos",
        "ip": ip,
        "port": config.get("wemos_port", 7777),
        "role": "ambilight",
        "monitor_index": config.get("led_monitor_index", 1),
        "leds": {
            "top": config.get("top_leds", 0),
            "bottom": config.get("bottom_leds", 0),
            "left": config.get("left_leds", 0),
            "right": config.get("right_leds", 0),
        },
        "enabled": True,
    }


def normalize_config_for_multi_device(config):
    """Config'i çoklu-cihaz şeması için bellekte hazırla.

    Eski tekli alanlar korunur; bu aşamada config diske yazılmaz ve worker'ın
    kullandığı tekli UDP davranışı değiştirilmez.
    """
    normalized = copy.deepcopy(config) if isinstance(config, dict) else {}
    normalized.setdefault("schema_version", MULTI_DEVICE_CONFIG_SCHEMA_VERSION)
    normalized.setdefault("multi_device_enabled", False)

    if not isinstance(normalized.get("wemos_devices"), list):
        primary_device = legacy_config_to_primary_device(normalized)
        normalized["wemos_devices"] = [primary_device] if primary_device else []

    return normalized


def _device_integer(value, field_name, minimum=0, maximum=None):
    """Cihaz payload'ındaki bir tam sayıyı doğrula."""
    if isinstance(value, bool):
        raise ValueError(f"{field_name} tam sayı olmalı")
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} tam sayı olmalı")
    if result < minimum or (maximum is not None and result > maximum):
        raise ValueError(f"{field_name} geçerli aralıkta değil")
    return result


def validate_wemos_device(payload, device_id=None, default_enabled=False):
    """Pasif cihaz kayıtları için güvenli ve tutarlı bir şema üret."""
    if not isinstance(payload, dict):
        raise ValueError("Cihaz verisi JSON nesnesi olmalı")

    name = str(payload.get("name", "")).strip()
    if not name or len(name) > 64:
        raise ValueError("Cihaz adı 1-64 karakter olmalı")

    ip = str(payload.get("ip", "")).strip()
    try:
        if ipaddress.ip_address(ip).version != 4:
            raise ValueError
    except ValueError:
        raise ValueError("Geçerli bir IPv4 adresi girin")

    role = payload.get("role", "ambilight")
    if role != "ambilight":
        raise ValueError("Bu sürüm yalnız ambilight cihazlarını destekler")

    leds_payload = payload.get("leds", {})
    if not isinstance(leds_payload, dict):
        raise ValueError("leds nesnesi gerekli")
    leds = {
        edge: _device_integer(leds_payload.get(edge, 0), f"leds.{edge}")
        for edge in ("top", "bottom", "left", "right")
    }

    enabled = payload.get("enabled", default_enabled)
    if not isinstance(enabled, bool):
        raise ValueError("enabled doğru veya yanlış olmalı")

    return {
        "id": device_id or uuid.uuid4().hex,
        "name": name,
        "ip": ip,
        "port": _device_integer(payload.get("port", 7777), "port", 1, 65535),
        "role": role,
        "monitor_index": _device_integer(payload.get("monitor_index", 1), "monitor_index", 1),
        "leds": leds,
        "enabled": enabled,
    }


def create_wemos_device(config, payload):
    """Yeni cihazı çoklu-device config'e pasif olarak ekle."""
    normalized = normalize_config_for_multi_device(config)
    device = validate_wemos_device(payload, default_enabled=False)
    if any(existing.get("ip") == device["ip"] for existing in normalized["wemos_devices"]):
        raise ValueError("Bu IP adresi zaten kayıtlı")
    normalized["wemos_devices"].append(device)
    return normalized, device


def update_wemos_device(config, device_id, payload):
    """Bir cihazı güncelle; kimliğini değiştirmeden doğrulamayı uygula."""
    normalized = normalize_config_for_multi_device(config)
    for index, existing in enumerate(normalized["wemos_devices"]):
        if existing.get("id") != device_id:
            continue
        merged = copy.deepcopy(existing)
        merged.update(payload if isinstance(payload, dict) else {})
        if isinstance(payload, dict) and "leds" in payload:
            merged["leds"] = payload["leds"]
        device = validate_wemos_device(merged, device_id=device_id, default_enabled=False)
        if any(other.get("id") != device_id and other.get("ip") == device["ip"]
               for other in normalized["wemos_devices"]):
            raise ValueError("Bu IP adresi zaten kayıtlı")
        normalized["wemos_devices"][index] = device
        return normalized, device
    raise ValueError("Cihaz bulunamadı")


def delete_wemos_device(config, device_id):
    """Cihazı config'ten kaldır; tekli worker davranışına dokunma."""
    normalized = normalize_config_for_multi_device(config)
    remaining = [device for device in normalized["wemos_devices"] if device.get("id") != device_id]
    if len(remaining) == len(normalized["wemos_devices"]):
        raise ValueError("Cihaz bulunamadı")
    normalized["wemos_devices"] = remaining
    return normalized


def set_multi_device_mode(config, enabled):
    """Çoklu modu yalnız etkin ve firmware LED toplamı doğrulanmış cihazlarla aç."""
    if not isinstance(enabled, bool):
        raise ValueError("enabled doğru veya yanlış olmalı")
    normalized = normalize_config_for_multi_device(config)
    if enabled:
        active_devices = [device for device in normalized["wemos_devices"] if device.get("enabled")]
        if not active_devices:
            raise ValueError("Etkinleştirilecek cihaz yok")
        invalid = [get_device_validation(device) for device in active_devices]
        invalid = [result for result in invalid if not result["compatible"]]
        if invalid:
            raise ValueError("Tüm etkin cihazların LED toplamı doğrulanmalı")
    normalized["multi_device_enabled"] = enabled
    return normalized

# ============================================================
# AĞ YARDIMCI FONKSİYONLARI
# ============================================================

def get_local_ip():
    """
    Bilgisayarın yerel IP adresini otomatik olarak bulur.
    Herhangi bir subnet'te çalışır (192.168.x.x, 10.x.x.x, 33.x.x.x vb.)
    """
    try:
        # Yöntem 1: Bir UDP soket açarak yerel IP'yi bul
        # Bu yöntem gerçek bir bağlantı kurmaz, sadece routing tablosunu kullanır
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0)
        try:
            s.connect(('10.255.255.255', 1))
            ip = s.getsockname()[0]
        except Exception:
            ip = None
        finally:
            s.close()
        
        if ip and ip != '127.0.0.1':
            return ip
    except Exception:
        pass

    try:
        # Yöntem 2 (Windows): ipconfig çıktısını analiz et
        result = subprocess.run(
            ['ipconfig'],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=CREATE_NO_WINDOW,
            errors='ignore',
            stdin=subprocess.DEVNULL
        )
        lines = result.stdout.split('\n')
        in_active_section = False
        for line in lines:
            if 'Wireless LAN adapter' in line or 'Kablosuz LAN' in line:
                in_active_section = True
            elif 'Ethernet adapter' in line or 'Ethernet Bağdaştırıcısı' in line:
                in_active_section = True
            elif line.strip() == '' and in_active_section:
                pass
            elif in_active_section and ('IPv4' in line or 'IPv4' in line):
                ip_match = re.search(r'(\d+\.\d+\.\d+\.\d+)', line)
                if ip_match:
                    found_ip = ip_match.group(1)
                    if found_ip != '127.0.0.1':
                        return found_ip
            elif 'adapter' in line.lower() and ':' in line:
                in_active_section = False
    except Exception:
        pass

    try:
        # Yöntem 3: hostname üzerinden
        hostname = socket.gethostname()
        ip = socket.gethostbyname(hostname)
        if ip and ip != '127.0.0.1':
            return ip
    except Exception:
        pass

    return None

def get_all_active_ips():
    """Tüm aktif IPv4 adreslerini döndürür"""
    ips = set()
    try:
        # socket ile hostname üzerinden
        hostname = socket.gethostname()
        for ip in socket.gethostbyname_ex(hostname)[2]:
            if not ip.startswith('127.'):
                ips.add(ip)
    except Exception:
        pass

    try:
        # ipconfig ile daha detaylı (Windows)
        result = subprocess.run(['ipconfig'], capture_output=True, text=True, creationflags=CREATE_NO_WINDOW, errors='ignore', stdin=subprocess.DEVNULL)
        for line in result.stdout.split('\n'):
            if 'IPv4' in line or 'IPv4' in line:
                match = re.search(r'(\d+\.\d+\.\d+\.\d+)', line)
                if match:
                    ip = match.group(1)
                    if not ip.startswith('127.'):
                        ips.add(ip)
    except Exception:
        pass
        
    return list(ips)

def get_subnet_base(ip):
    """IP adresinden subnet base'i döndürür (örn: 33.33.33)"""
    if ip:
        parts = ip.split('.')
        if len(parts) == 4:
            return '.'.join(parts[:3])
    return None

def get_subnet_mask():
    """Ağ maskesini döndürür"""
    try:
        result = subprocess.run(
            ['ipconfig'],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=CREATE_NO_WINDOW,
            errors='ignore',
            stdin=subprocess.DEVNULL
        )
        lines = result.stdout.split('\n')
        for i, line in enumerate(lines):
            if 'Subnet Mask' in line or 'Alt Ağ Maskesi' in line:
                mask_match = re.search(r'(\d+\.\d+\.\d+\.\d+)', line)
                if mask_match:
                    return mask_match.group(1)
    except Exception:
        pass
    return '255.255.255.0'

def find_wemos_ip_on_network(progress_callback=None):
    """
    Yönetici izni gerektirmeden Wemos'u bulur.
    1) Akıllı UDP Broadcast dener
    2) Bulamazsa HTTP ile subnet taraması yapar (fallback)
    """
    print("DEBUG: Wemos arama başlatıldı (Akıllı UDP)...")
    
    found_ip = None
    
    # 1. Bilgisayarın kendi IP'sini ve Broadcast adresini bul
    local_ip = get_local_ip()
    broadcast_list = ['255.255.255.255'] # Genel yayın
    
    if local_ip:
        # Örnek: IP 192.168.1.20 ise Broadcast 192.168.1.255 olur
        parts = local_ip.split('.')
        parts[3] = '255'
        subnet_broadcast = '.'.join(parts)
        broadcast_list.append(subnet_broadcast) # Yerel yayın (Daha garantidir)
        print(f"DEBUG: Hedef Broadcast Adresleri: {broadcast_list}")

    # --- YÖNTEM 1: UDP Broadcast ---
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(1.0) # 1 saniye bekle
        
        # Soketi dinlemeye hazırla (Port 0 = Rastgele boş port ver)
        sock.bind(('', 0))
        
        # Tüm yayın adreslerine mesaj gönder
        message = b"AMBLIGHT_DISCOVERY"
        
        # 3 kere dene (Paket kaybına karşı)
        for _ in range(3):
            if found_ip: break
            
            for target_ip in broadcast_list:
                try:
                    sock.sendto(message, (target_ip, 7777))
                except Exception as e:
                    print(f"Yayın hatası ({target_ip}): {e}")
            
            # Cevap bekle
            start_time = time.time()
            while time.time() - start_time < 1.5:
                try:
                    data, addr = sock.recvfrom(1024)
                    # Gelen cevap bizim istediğimiz mi?
                    if b"AMBLIGHT_RESPONSE" in data:
                        raw_data = data.decode('utf-8', errors='ignore')
                        # Cevap formatı: "AMBLIGHT_RESPONSE:192.168.1.50"
                        if ":" in raw_data:
                            found_ip = raw_data.split(":")[1].strip()
                        else:
                            found_ip = addr[0] # Formatta IP yoksa gönderen IP'yi al
                            
                        print(f"DEBUG: WEMOS BULUNDU (UDP)! IP: {found_ip}")
                        if progress_callback:
                            progress_callback(f"Bulundu: {found_ip}")
                        sock.close()
                        return found_ip
                        
                except socket.timeout:
                    break # Bu turda cevap gelmedi, tekrar dene
                except Exception as e:
                    print(f"Dinleme hatası: {e}")
                    break
        
        sock.close()
    except Exception as e:
        print(f"Socket hatası: {e}")

    # --- YÖNTEM 2: UDP Unicast Subnet Taraması (Fallback) ---
    if not found_ip and local_ip:
        print("DEBUG: UDP broadcast yanıt yok, unicast subnet taraması başlatılıyor...")
        if progress_callback:
            progress_callback("Broadcast yanıt yok, subnet taranıyor...")
        
        subnet_base = get_subnet_base(local_ip)
        if subnet_base:
            import concurrent.futures
            
            def check_wemos_udp_unicast(ip):
                """Verilen IP'ye doğrudan UDP discovery paketi gönder"""
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    s.settimeout(0.5)
                    s.sendto(b"AMBLIGHT_DISCOVERY", (ip, 7777))
                    try:
                        data, addr = s.recvfrom(1024)
                        if b"AMBLIGHT_RESPONSE" in data:
                            s.close()
                            return ip
                    except socket.timeout:
                        pass
                    s.close()
                except Exception:
                    pass
                return None
            
            my_ip_last_octet = int(local_ip.split('.')[-1])
            all_ips = [f"{subnet_base}.{i}" for i in range(1, 255) if i != my_ip_last_octet]
            
            # Config'deki kaydedilmiş IP'yi listeye en başa koy (hızlı bağlanma)
            config = load_config()
            if config and config.get('wemos_ip'):
                saved_ip = config['wemos_ip']
                if saved_ip in all_ips:
                    all_ips.remove(saved_ip)
                    all_ips.insert(0, saved_ip)
            
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
                    futures = {executor.submit(check_wemos_udp_unicast, ip): ip for ip in all_ips}
                    for future in concurrent.futures.as_completed(futures, timeout=15):
                        result_ip = future.result()
                        if result_ip:
                            found_ip = result_ip
                            print(f"DEBUG: WEMOS BULUNDU (Unicast)! IP: {found_ip}")
                            if progress_callback:
                                progress_callback(f"Bulundu: {found_ip}")
                            for f in futures:
                                f.cancel()
                            return found_ip
            except Exception as e:
                print(f"Unicast tarama hatası: {e}")

    return found_ip


def find_all_wemos_on_network(timeout=3.0):
    """UDP discovery yanıtlarını toplayıp tüm benzersiz Wemos IP'lerini döndür."""
    discovered = set()
    local_ip = get_local_ip()
    broadcast_list = ['255.255.255.255']
    if local_ip:
        parts = local_ip.split('.')
        parts[3] = '255'
        broadcast_list.append('.'.join(parts))

    try:
        discovery_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        discovery_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        discovery_socket.settimeout(0.25)
        discovery_socket.bind(('', 0))
        for target_ip in broadcast_list:
            discovery_socket.sendto(b"AMBLIGHT_DISCOVERY", (target_ip, 7777))

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, addr = discovery_socket.recvfrom(1024)
                if b"AMBLIGHT_RESPONSE" not in data:
                    continue
                response = data.decode('utf-8', errors='ignore')
                reported_ip = response.split(':', 1)[1].strip() if ':' in response else addr[0]
                if ipaddress.ip_address(reported_ip).version != 4:
                    continue
                discovered.add(reported_ip)
            except socket.timeout:
                continue
            except (ValueError, OSError):
                continue
        discovery_socket.close()
    except OSError:
        pass

    return sorted(discovered, key=lambda ip: tuple(map(int, ip.split('.'))))

def check_wemos_connection(ip, port=7777, timeout=2):
    """Wemos'un erişilebilir olup olmadığını kontrol eder"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        sock.sendto(b"AMBLIGHT_DISCOVERY", (ip, port))
        try:
            data, addr = sock.recvfrom(1024)
            sock.close()
            return True
        except socket.timeout:
            sock.close()
            # Timeout olsa bile paket gönderebildiysek bir şans daha ver
            # Wemos cihazı discovery'ye yanıt vermeyebilir ama LED verisi alabilir
            return False
    except Exception:
        return False

# ============================================================
# Wi-Fi KURULUM FONKSİYONLARI
# ============================================================

def send_wifi_config_to_wemos(hotspot_ip, wifi_ssid, wifi_password, method='http'):
    """Wemos'a Wi-Fi konfigürasyonunu gönderir"""
    if method == 'http':
        try:
            url = f"http://{hotspot_ip}/wifi_config"
            data = urllib.parse.urlencode({
                'ssid': wifi_ssid,
                'password': wifi_password
            }).encode('utf-8')
            
            req = urllib.request.Request(url, data=data, method='POST')
            req.add_header('Content-Type', 'application/x-www-form-urlencoded')
            
            with urllib.request.urlopen(req, timeout=10) as response:
                result = response.read().decode('utf-8')
                return True, result
        except urllib.error.URLError as e:
            return False, f"HTTP bağlantı hatası: {e}"
        except Exception as e:
            return False, f"HTTP hatası: {e}"
    else:
        try:
            message = f"WIFI_CONFIG:{wifi_ssid}:{wifi_password}"
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(5)
            sock.sendto(message.encode('utf-8'), (hotspot_ip, 7777))
            
            try:
                response, addr = sock.recvfrom(1024)
                sock.close()
                return True, response.decode('utf-8', errors='ignore')
            except socket.timeout:
                sock.close()
                return True, "UDP paketi gönderildi (yanıt alınamadı)"
        except Exception as e:
            return False, f"UDP hatası: {e}"

def scan_available_wifi_networks():
    """Görünen Wi-Fi ağlarını tarar"""
    try:
        result = subprocess.run(
            ['netsh', 'wlan', 'show', 'networks', 'mode=Bssid'],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=CREATE_NO_WINDOW,
            errors='ignore',
            stdin=subprocess.DEVNULL
        )
        networks = []
        for line in result.stdout.split('\n'):
            line = line.strip()
            if line.startswith('SSID'):
                parts = line.split(':', 1)
                if len(parts) > 1:
                    current_ssid = parts[1].strip()
                    if current_ssid and current_ssid not in networks:
                        networks.append(current_ssid)
        return networks
    except Exception:
        return []

def find_wemos_hotspot():
    """Wemos hotspot'unu otomatik bulur (locale uyumlu)"""
    wemos_keywords = ['wemos', 'ambilight', 'setup', 'config', 'esp']
    # Her Windows diline göre en olası SSID etiketleri
    ssid_labels = ['ssid', 'ID', 'AG', 'WLAN', 'Ad', 'Nomi', 'ชื่อ', '네트워크', 'Réseau']
    try:
        result = subprocess.run(
            ['netsh', 'wlan', 'show', 'networks', 'mode=Bssid'],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=CREATE_NO_WINDOW,
            errors='ignore',
            stdin=subprocess.DEVNULL
        )
        for line in result.stdout.split('\n'):
            line_lower = line.strip().lower()
            for keyword in wemos_keywords:
                # Her SSID etiketine göre etiket arama
                for ssid_label in ssid_labels:
                    if keyword in line_lower and ssid_label in line_lower:
                        ssid_match = re.search(rf'{re.escape(ssid_label)}\s*\d*\s*:\s*(.+)', line, re.IGNORECASE)
                        if ssid_match:
                            return ssid_match.group(1).strip()
        return None
    except Exception:
        return None

def connect_to_wifi(ssid, password=None):
    """Belirli bir Wi-Fi ağına bağlanır"""
    try:
        if password:
            result = subprocess.run(
                ['netsh', 'wlan', 'connect', f'name={ssid}'],
                capture_output=True, text=True, timeout=30,
                creationflags=CREATE_NO_WINDOW, errors='ignore', stdin=subprocess.DEVNULL
            )
        else:
            result = subprocess.run(
                ['netsh', 'wlan', 'connect', f'name={ssid}'],
                capture_output=True, text=True, timeout=30,
                creationflags=CREATE_NO_WINDOW, errors='ignore', stdin=subprocess.DEVNULL
            )
        return 'başarıyla bağlandı' in result.stdout.lower() or 'successfully' in result.stdout.lower()
    except Exception:
        return False

# ============================================================
# KULLANICI GİRDİ FONKSİYONLARI
# ============================================================

def get_user_input(prompt, input_type=int, default=None, min_val=None, max_val=None):
    """Kullanıcıdan güvenli input alır"""
    while True:
        try:
            if default is not None:
                user_input = input(f"{prompt} (Varsayılan: {default}): ").strip()
                if not user_input:
                    return default
            else:
                user_input = input(f"{prompt}: ").strip()
                if not user_input:
                    print("Bu alan boş bırakılamaz!")
                    continue
            
            if input_type == int:
                value = int(user_input)
                if min_val is not None and value < min_val:
                    print(f"Değer {min_val}'den küçük olamaz!")
                    continue
                if max_val is not None and value > max_val:
                    print(f"Değer {max_val}'den büyük olamaz!")
                    continue
                return value
            elif input_type == str:
                return user_input
        except ValueError:
            print("Geçersiz giriş! Lütfen tekrar deneyin.")
        except KeyboardInterrupt:
            print("\nİşlem iptal edildi.")
            sys.exit(0)

# ============================================================
# İLK KURULUM
# ============================================================

def first_time_setup():
    """İlk kurulum - tamamen otomatik"""
    print("\n" + "="*60)
    print("AMBLIGHT OTOMATIK KURULUM")
    print("="*60)
    print("\nLütfen LED konfigürasyonunuzu girin:\n")
    
    top_leds = get_user_input("Üst kenarda kaç LED var?", min_val=1, max_val=200)
    bottom_leds = get_user_input("Alt kenarda kaç LED var?", min_val=0, max_val=200)
    left_leds = get_user_input("Sol kenarda kaç LED var?", min_val=1, max_val=200)
    right_leds = get_user_input("Sağ kenarda kaç LED var?", min_val=1, max_val=200)
    
    print("\n" + "="*60)
    print("WEMOS Wi-Fi OTOMATIK KURULUMU")
    print("="*60)
    
    # Kullanıcıya seçenek sun: otomatik veya manuel
    print("\nWemos'u nasıl bağlamak istiyorsunuz?")
    print("  1. Otomatik (Wemos hotspot üzerinden)")
    print("  2. Manuel IP girişi (Wemos zaten ağda)")
    
    choice = get_user_input("Seçiminiz", min_val=1, max_val=2)
    
    if choice == 1:
        # Otomatik kurulum
        print("\nWemos cihazınızı güç verin ve birkaç saniye bekleyin...")
        print("Wemos otomatik olarak hotspot modunda açılacaktır.\n")
        
        print("Wemos hotspot'u aranıyor...")
        wemos_hotspot = None
        for attempt in range(10):
            wemos_hotspot = find_wemos_hotspot()
            if wemos_hotspot:
                print(f"✓ Wemos hotspot bulundu: {wemos_hotspot}")
                break
            time.sleep(1)
            print(".", end="", flush=True)
        
        if not wemos_hotspot:
            print("\n✗ Wemos hotspot bulunamadı!")
            wemos_hotspot = input("\nWemos hotspot SSID'sini manuel olarak girin (veya Enter): ").strip()
            if not wemos_hotspot:
                print("Kurulum iptal edildi.")
                return None
        
        print(f"\nWemos hotspot'una bağlanılıyor: {wemos_hotspot}...")
        if connect_to_wifi(wemos_hotspot):
            print("✓ Hotspot'a bağlandı!")
            time.sleep(3)
        else:
            print("⚠ Hotspot'a otomatik bağlanılamadı. Lütfen manuel olarak bağlanın.")
            input("Bağlandıktan sonra Enter'a basın...")
        
        hotspot_ip = "192.168.4.1"
        
        print("\nMevcut Wi-Fi ağları taranıyor...")
        available_networks = scan_available_wifi_networks()
        
        if available_networks:
            print(f"\n✓ {len(available_networks)} Wi-Fi ağı bulundu:\n")
            for i, network in enumerate(available_networks[:15], 1):
                print(f"  {i}. {network}")
            
            network_choice = get_user_input("Bağlanmak istediğiniz ağın numarasını seçin", 
                                           min_val=1, max_val=min(15, len(available_networks)))
            wifi_ssid = available_networks[network_choice - 1]
        else:
            wifi_ssid = get_user_input("Bağlanmak istediğiniz Wi-Fi ağının adını girin", input_type=str)
        
        wifi_password = get_user_input("Wi-Fi şifresini girin", input_type=str)
        
        print(f"\nWi-Fi bilgileri Wemos'a gönderiliyor...")
        success, message = send_wifi_config_to_wemos(hotspot_ip, wifi_ssid, wifi_password, 'http')
        
        if success:
            print(f"✓ Wi-Fi konfigürasyonu başarıyla gönderildi!")
            print(f"\nLütfen bilgisayarınızı '{wifi_ssid}' ağına bağlayın.")
            input("Bağlandıktan sonra Enter'a basın...")
            
            print("\nWemos'un IP adresi aranıyor...")
            wemos_ip = None
            for attempt in range(20):
                wemos_ip = find_wemos_ip_on_network(
                    progress_callback=lambda msg: print(f"  {msg}")
                )
                if wemos_ip:
                    print(f"✓ Wemos bulundu! IP: {wemos_ip}")
                    break
                time.sleep(1)
                print(".", end="", flush=True)
            
            if not wemos_ip:
                print("\n⚠ Wemos IP'si otomatik bulunamadı.")
                local_ip = get_local_ip()
                subnet = get_subnet_base(local_ip) if local_ip else "192.168.1"
                wemos_ip = get_user_input("Wemos IP adresini manuel olarak girin", 
                                         input_type=str, default=f"{subnet}.20")
        else:
            print(f"✗ Wi-Fi konfigürasyonu gönderilemedi: {message}")
            local_ip = get_local_ip()
            subnet = get_subnet_base(local_ip) if local_ip else "192.168.1"
            wemos_ip = get_user_input("Wemos IP adresini manuel olarak girin", 
                                     input_type=str, default=f"{subnet}.20")
    else:
        # Manuel IP girişi
        local_ip = get_local_ip()
        if local_ip:
            subnet = get_subnet_base(local_ip)
            print(f"\n📡 Mevcut ağ bilgileriniz:")
            print(f"  Yerel IP: {local_ip}")
            print(f"  Subnet: {subnet}.x\n")
        
        # Otomatik tarama teklifi
        print("Wemos'u ağda otomatik aramak ister misiniz?")
        auto_scan = input("(E/H, Varsayılan: E): ").strip().upper()
        
        if auto_scan != 'H':
            print("\nAğ taranıyor...")
            wemos_ip = find_wemos_ip_on_network(
                progress_callback=lambda msg: print(f"  {msg}")
            )
            if wemos_ip:
                print(f"\n✓ Wemos bulundu: {wemos_ip}")
            else:
                print("\n⚠ Wemos otomatik bulunamadı.")
                default_ip = f"{subnet}.20" if local_ip else "192.168.1.20"
                wemos_ip = get_user_input("Wemos IP adresini girin", input_type=str, default=default_ip)
        else:
            default_ip = f"{subnet}.20" if local_ip else "192.168.1.20"
            wemos_ip = get_user_input("Wemos IP adresini girin", input_type=str, default=default_ip)
    
    wemos_port = 7777
    
    config = {
        "top_leds": top_leds,
        "bottom_leds": bottom_leds,
        "left_leds": left_leds,
        "right_leds": right_leds,
        "wemos_ip": wemos_ip,
        "wemos_port": wemos_port,
        "fps": 60,
        "edge_width": 20
    }
    
    if save_config(config):
        print("\n✓ Konfigürasyon başarıyla kaydedildi!")
        return config
    else:
        print("\n✗ Konfigürasyon kaydedilemedi!")
        return None

# ============================================================

# ============================================================
# EKRAN YAKALAMA
# ============================================================

def hex_to_rgb(hex_color):
    hex_color = str(hex_color).lstrip('#')
    try:
        return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))
    except Exception:
        return (255, 0, 0)

def get_system_accent_color():
    """Sistem vurgu rengini döndürür (Windows), güvenli fallback ile"""
    try:
        try:
            registry = winreg.ConnectRegistry(None, winreg.HKEY_CURRENT_USER)
            key = winreg.OpenKey(registry, r"Software\Microsoft\Windows\DWM")
            value, _ = winreg.QueryValueEx(key, "ColorizationColor")
            winreg.CloseKey(key)

            # ColorizationColor genelde ARGB (AARRGGBB) formatındadır, bize RGB lazım
            color_hex = f"{value:08x}"
            if len(color_hex) == 8:
                r = int(color_hex[2:4], 16)
                g = int(color_hex[4:6], 16)
                b = int(color_hex[6:8], 16)
                return f"#{r:02x}{g:02x}{b:02x}"
        except Exception:
            log.warning(f"[ACCENT] Windows DWM registry'dan vurgu rengi okunamadı")
            pass

        # Tüm yöntemler başarısız oldu: güvenli bir fallback kullan
        log.warning(f"[ACCENT] Tüm sistem vurgu rengi yöntemleri başarısız, varsayılan mavi (#3584e4) kullanılıyor")
        return "#3584e4"
    except Exception as e:
        log.error(f"[ACCENT] get_system_accent_color hatası: {e}")
        return "#3584e4"  # kritik durumda güvenli fallback

# Eski isimle uyumluluk
get_windows_accent_color = get_system_accent_color

# Fullscreen cache (60fps'de her frame'de ağır kontroller yapmamak için - Windows)
_fullscreen_cache = {'value': False, 'time': 0}
_FULLSCREEN_CACHE_TTL = 0.5  # 500ms'de bir kontrol et (2. ekrana tıklama anında hızlı tepki)


# --- LED Monitörü Seçimi ---
# --- LED Monitörü Seçimi ---
# mss kütüphanesinde:
#   sct.monitors[0]: TÜM monitörlerin birleşimidir (Virtual Desktop). LED için KULLANILMAZ!
#   sct.monitors[1]: 1. fiziksel monitör (genellikle birincil)
#   sct.monitors[2]: 2. fiziksel monitör vb.
def get_primary_monitor_index(sct_inst=None):
    """sct.monitors içindeki birincil fiziksel monitörün indeksini döndürür"""
    try:
        if sct_inst is None or not hasattr(sct_inst, 'monitors'):
            with mss() as s:
                return get_primary_monitor_index(s)
        if len(sct_inst.monitors) <= 1:
            return 0
        for idx in range(1, len(sct_inst.monitors)):
            m = sct_inst.monitors[idx]
            if m.get('is_primary') or (m.get('left') == 0 and m.get('top') == 0):
                return idx
        return 1
    except Exception:
        return 1

def get_led_monitor_index(sct_inst=None):
    """Kullanılacak LED monitörünün indeksini döndürür (asla virtual 0 dönmez)"""
    # 1) Ortam değişkeninden oku (öncelikli)
    env_idx = os.environ.get("LED_MONITOR_INDEX")
    if env_idx is not None:
        try:
            idx = int(env_idx)
            if idx > 0:
                return idx
        except ValueError:
            pass

    # 2) Config dosyasından oku
    try:
        config = load_config()
        if config and "led_monitor_index" in config:
            idx = int(config["led_monitor_index"])
            if idx > 0:
                return idx
    except Exception:
        pass

    # 3) Varsayılan: birincil fiziksel monitör (asla 0 / sanal ekran dönmez)
    return get_primary_monitor_index(sct_inst)

def get_available_monitors(sct_inst=None):
    """Sistemdeki fiziksel monitörleri listeler (Web UI için)"""
    monitors_list = []
    try:
        def _extract(s):
            for idx in range(1, len(s.monitors)):
                m = s.monitors[idx]
                monitors_list.append({
                    "index": idx,
                    "name": m.get("name", f"Monitör {idx}"),
                    "width": m.get("width", 0),
                    "height": m.get("height", 0),
                    "left": m.get("left", 0),
                    "top": m.get("top", 0),
                    "is_primary": bool(m.get("is_primary") or (m.get("left") == 0 and m.get("top") == 0))
                })
        if sct_inst:
            _extract(sct_inst)
        else:
            with mss() as s:
                _extract(s)
    except Exception:
        pass
    return monitors_list




def _is_window_fullscreen_on_primary_win32(hwnd, monitor=None):
    """Windows: Verilen pencerenin seçili LED monitöründe tam ekran olup olmadığını kontrol eder"""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        
        # Desktop ve Shell pencerelerini atla
        if hwnd == user32.GetDesktopWindow() or hwnd == user32.GetShellWindow():
            return False
        
        # Görünür olmayan pencereleri atla
        if not user32.IsWindowVisible(hwnd):
            return False
        
        # DWM Cloaked pencerelerini atla (örn: "Windows Giriş Deneyimi" gibi
        # görünmez ama ekranı kaplayan system pencereleri)
        try:
            dwmapi = ctypes.windll.dwmapi
            cloaked = ctypes.c_int(0)
            DWMWA_CLOAKED = 14
            hr = dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
            if hr == 0 and cloaked.value != 0:
                return False  # Cloaked pencere, gerçek fullscreen değil
        except Exception:
            pass  # dwmapi yoksa devam et
        
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        
        if monitor is not None:
            return (w == monitor['width'] and h == monitor['height']
                    and rect.left == monitor['left'] and rect.top == monitor['top'])

        screen_w = user32.GetSystemMetrics(0)
        screen_h = user32.GetSystemMetrics(1)
        return (w == screen_w and h == screen_h and rect.top == 0 and rect.left == 0)
    except Exception:
        return False

def is_fullscreen(monitor=None):
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32

        # Önce foreground window'u kontrol et (en hızlı yol, her frame'de çalışabilir)
        hwnd = user32.GetForegroundWindow()
        if hwnd and _is_window_fullscreen_on_primary_win32(hwnd, monitor):
            _fullscreen_cache['value'] = True
            _fullscreen_cache['time'] = time.time()
            return True

        # Foreground fullscreen değilse, cache'li EnumWindows taraması yap
        # (2. ekrana tıklanmışsa, 1. ekranda hala fullscreen pencere olabilir)
        now = time.time()
        if now - _fullscreen_cache['time'] < _FULLSCREEN_CACHE_TTL:
            return _fullscreen_cache['value']

        found_fullscreen = [False]

        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def enum_callback(hwnd, lParam):
            if _is_window_fullscreen_on_primary_win32(hwnd, monitor):
                found_fullscreen[0] = True
                return False  # Taramayı durdur, bulduk
            return True  # Devam et

        user32.EnumWindows(WNDENUMPROC(enum_callback), 0)
        _fullscreen_cache['value'] = found_fullscreen[0]
        _fullscreen_cache['time'] = now
        return found_fullscreen[0]
    except Exception:
        return False

def grab_edge_colors(top_leds, bottom_leds, left_leds, right_leds, edge_width, edge_offset, sct, monitor_index=None):
    """Ekran kenarlarını önbellekli bölgeler ve toplu RGB toplamlarıyla örnekle."""
    if sct is None or not hasattr(sct, 'monitors'):
        sct = mss()
    led_idx = monitor_index if monitor_index is not None else get_led_monitor_index(sct)
    if not hasattr(sct, 'monitors') or led_idx >= len(sct.monitors) or led_idx <= 0:
        led_idx = get_primary_monitor_index(sct)
    monitor = sct.monitors[led_idx]
    screenshot = sct.grab(monitor)
    return edge_colors(screenshot, top_leds, bottom_leds, left_leds, right_leds, edge_width, edge_offset)



def build_device_frame(device, edge_width, edge_offset, capture):
    """Bir cihazın atanmış monitörü için UDP RGB paketini hesapla.

    Bu yardımcı yalnız paket üretir; gönderim ve bağlantı kontrolü yapmaz.
    """
    leds = device.get("leds", {})
    return grab_edge_frame(
        int(leds.get("top", 0)),
        int(leds.get("bottom", 0)),
        int(leds.get("left", 0)),
        int(leds.get("right", 0)),
        edge_width,
        edge_offset,
        capture,
        monitor_index=int(device.get("monitor_index", 1)),
    )


def grab_edge_frame(top, bottom, left, right, width, offset, capture, monitor_index, brightness=255):
    if not 0 < monitor_index < len(capture.monitors):
        monitor_index = get_primary_monitor_index(capture)
    screenshot = capture.grab(capture.monitors[monitor_index])
    return pack_rgb(edge_rgb(screenshot, top, bottom, left, right, width, offset), brightness)

# ============================================================
# GLOBAL DEĞİŞKENLER VE DURUM
# ============================================================

running = True
ambilight_thread = None
sock = None
sct = None
icon = None

# Connectivity thread iptal mekanizması
connectivity_cancel_event = threading.Event()

# Web UI durum bilgisi
app_status = {
    "connection": "bağlantı yok",
    "wemos_ip": "",
    "wemos_port": 7777,
    "top_leds": 0,
    "bottom_leds": 0,
    "left_leds": 0,
    "right_leds": 0,
    "total_leds": 0,
    "fps": 60,
    "edge_width": 20,
    "edge_offset": 0,
    "idle_mode": False,
    "idle_use_windows_color": False,
    "idle_color": "#ff0000",
    "idle_brightness": 50,
    "fullscreen_max_brightness": 255,
    "local_ip": "",
    "subnet": "",
    "packets_sent": 0,
    "errors": 0,
    "uptime_start": 0,
    "running": False,
    "last_error": "",
    "actual_fps": 0
}
status_lock = threading.Lock()
connectivity = Connectivity()


def set_connection_target(ip, port, active=True, force=False):
    with status_lock:
        connectivity.reset(ip, port, active=active, force=force)
        app_status['wemos_ip'] = ip
        app_status['wemos_port'] = int(port)

def update_status(key, value):
    """Thread-safe durum güncelleme"""
    with status_lock:
        app_status[key] = value

def get_status():
    """Thread-safe durum okuma"""
    with status_lock:
        status = dict(app_status)
        status['connectivity'] = connectivity.snapshot()
        status['connection'] = legacy_connection(status['connectivity']['state'])
        return status

# ============================================================
# WEB ARAYÜZÜ SUNUCUSU
# ============================================================

# Sadece bu anahtarlar /api/config üzerinden kaydedilebilir
# (OTA kilitli ayarlar, OTA update öncesi veri kaybı önleme)
ALLOWED_CONFIG_KEYS = {
    "wemos_ip", "wemos_port", "fps",
    "top_leds", "bottom_leds", "left_leds", "right_leds",
    "edge_width", "led_offset", "edge_offset",
    "idle_mode", "idle_color", "idle_brightness",
    "idle_use_windows_color",
    "fullscreen_max_brightness",
    "led_monitor_index",
}

class WebUIHandler(http.server.BaseHTTPRequestHandler):
    """Web arayüzü için HTTP istek işleyicisi"""

    def log_message(self, format, *args):
        """HTTP loglarını sustur (konsolu temiz tut)"""
        pass
    
    def _safe_send_json(self, data):
        """JSON yanıtını güvenli şekilde gönder (BrokenPipeError koruması)"""
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # İstemci bağlantıyı kapattı, sorun değil
    
    def do_GET(self):
        try:
            if self.path == '/' or self.path == '/index.html':
                self.serve_html()
            elif self.path == '/api/status':
                self.serve_status()
            elif self.path == '/api/devices':
                self.handle_devices_list()
            elif self.path == '/api/devices/scan':
                self.handle_devices_scan()
            elif self.path == '/api/devices/validation':
                self.handle_devices_validation()
            elif self.path == '/api/scan':
                self.serve_scan()
            elif self.path == '/api/logs':
                self.serve_logs()
            elif self.path == '/api/test-wemos-connection':
                self.handle_test_wemos_connection()
            else:
                self.send_error(404)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
    
    def do_POST(self):
        try:
            if self.path == '/api/config':
                self.handle_config_update()
            elif self.path == '/api/monitors/refresh':
                self._safe_send_json(monitor_catalog.refresh(get_available_monitors))
            elif self.path == '/api/devices/mode':
                self.handle_devices_mode()
            elif self.path == '/api/devices':
                self.handle_device_create()
            elif self.path == '/api/restart':
                self.handle_restart()
            elif self.path == '/api/wemos/restart':
                self.handle_wemos_restart()
            elif self.path == '/api/wemos/sleep':
                self.handle_wemos_sleep()
            elif self.path == '/api/wemos/reset_wifi':
                self.handle_wemos_reset_wifi()
            elif self.path == '/api/wemos/ota':
                self.handle_wemos_ota()
            else:
                self.send_error(404)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def do_PUT(self):
        try:
            device_id = self._device_id_from_path()
            if device_id:
                self.handle_device_update(device_id)
            else:
                self.send_error(404)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def do_DELETE(self):
        try:
            device_id = self._device_id_from_path()
            if device_id:
                self.handle_device_delete(device_id)
            else:
                self.send_error(404)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _read_json_body(self):
        content_length = int(self.headers.get('Content-Length', 0))
        if content_length <= 0:
            raise ValueError("JSON gövdesi gerekli")
        return json.loads(self.rfile.read(content_length).decode('utf-8'))

    def _device_id_from_path(self):
        path = urllib.parse.urlparse(self.path).path
        prefix = "/api/devices/"
        if not path.startswith(prefix):
            return None
        device_id = urllib.parse.unquote(path[len(prefix):])
        return device_id if device_id and "/" not in device_id else None
    
    def serve_html(self):
        """Ana HTML sayfasını sun"""
        html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web_ui', 'index.html')
        try:
            with open(html_path, 'r', encoding='utf-8') as f:
                content = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(content.encode('utf-8'))
        except FileNotFoundError:
            self.send_response(404)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(b"<h1>Web UI dosyasi bulunamadi!</h1>")
    
    def serve_status(self):
        """Durum bilgisini JSON olarak sun"""
        status = get_status()
        
        # Monitör listesini ve seçili monitörü ekle
        status.update(monitor_catalog.snapshot())
        status.setdefault('led_monitor_index', 1)

        # Uptime hesapla
        if status['uptime_start'] > 0:
            uptime_seconds = int(time.time() - status['uptime_start'])
            hours = uptime_seconds // 3600
            minutes = (uptime_seconds % 3600) // 60
            seconds = uptime_seconds % 60
            status['uptime'] = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        else:
            status['uptime'] = "00:00:00"
        
        self._safe_send_json(status)

    def serve_logs(self):
        """Son logları JSON olarak sun"""
        with _log_buffer_lock:
            logs = list(_log_buffer)
        self._safe_send_json({"logs": logs})

    def handle_devices_list(self):
        """Pasif çoklu-cihaz kayıtlarını döndür; tekli config'i değiştirme."""
        config = normalize_config_for_multi_device(load_config() or {})
        self._safe_send_json({
            "success": True,
            "schema_version": config["schema_version"],
            "multi_device_enabled": config["multi_device_enabled"],
            "devices": config["wemos_devices"],
        })

    def handle_devices_scan(self):
        """Ağdaki cihazları listeler; kullanıcı seçmeden config'e yazmaz."""
        devices = [{"ip": ip, "port": 7777} for ip in find_all_wemos_on_network()]
        self._safe_send_json({"success": True, "devices": devices})

    def handle_devices_validation(self):
        """Kayıtlı cihazların firmware LED toplamlarını doğrular."""
        config = normalize_config_for_multi_device(load_config() or {})
        validations = [get_device_validation(device) for device in config["wemos_devices"]]
        self._safe_send_json({"success": True, "devices": validations})

    def handle_devices_mode(self):
        try:
            payload = self._read_json_body()
            config = set_multi_device_mode(load_config() or {}, payload.get("enabled"))
            if not save_config(config):
                raise ValueError("Çoklu mod kaydedilemedi")
            result = {"success": True, "multi_device_enabled": config["multi_device_enabled"]}
        except Exception as e:
            result = {"success": False, "message": str(e)}
        self._safe_send_json(result)

    def handle_device_create(self):
        try:
            config, device = create_wemos_device(load_config() or {}, self._read_json_body())
            if not save_config(config):
                raise ValueError("Cihaz kaydedilemedi")
            result = {"success": True, "device": device, "message": "Cihaz pasif olarak eklendi."}
        except Exception as e:
            result = {"success": False, "message": str(e)}
        self._safe_send_json(result)

    def handle_device_update(self, device_id):
        try:
            config, device = update_wemos_device(load_config() or {}, device_id, self._read_json_body())
            if not save_config(config):
                raise ValueError("Cihaz kaydedilemedi")
            result = {"success": True, "device": device, "message": "Cihaz güncellendi."}
        except Exception as e:
            result = {"success": False, "message": str(e)}
        self._safe_send_json(result)

    def handle_device_delete(self, device_id):
        try:
            config = delete_wemos_device(load_config() or {}, device_id)
            if not save_config(config):
                raise ValueError("Cihaz kaydedilemedi")
            result = {"success": True, "message": "Cihaz silindi."}
        except Exception as e:
            result = {"success": False, "message": str(e)}
        self._safe_send_json(result)

    def serve_scan(self):
        """Ağ taraması yap ve sonucu döndür"""
        result = {"found": False, "ip": None, "message": "Taranıyor..."}
        
        try:
            found_ip = find_wemos_ip_on_network()
            if found_ip:
                result["found"] = True
                result["ip"] = found_ip
                result["message"] = f"Wemos bulundu: {found_ip}"
                
                # Bulunan IP'yi config dosyasına kaydet
                current_config = load_config() or {}
                current_config["wemos_ip"] = found_ip
                save_ok = save_config(current_config)
                if save_ok:
                    print(f"[TARAMA] Wemos bulundu ve config'e kaydedildi: {found_ip}")
                    result["message"] = f"Wemos bulundu ve kaydedildi: {found_ip}"
                else:
                    print(f"[TARAMA] Wemos bulundu ama config kaydedilemedi: {found_ip}")
                
                # Durum bilgisini hemen güncelle
                if save_ok:
                    set_connection_target(found_ip, current_config.get('wemos_port', 7777), active=running)
            else:
                result["message"] = "Wemos ağda bulunamadı"
        except Exception as e:
            result["message"] = f"Tarama hatası: {str(e)}"
        
        self._safe_send_json(result)

    def handle_config_update(self):
        """Konfigürasyon güncelleme"""
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)

        try:
            new_config = json.loads(body.decode('utf-8'))
            # Sadece whitelisted anahtarları kabul et (OTA kilitli anahtarları filtrele)
            filtered_config = {}
            for k, v in new_config.items():
                if k in ALLOWED_CONFIG_KEYS:
                    # Boş veya sadece boşluklardan oluşan wemos_ip gelirse mevcut IP'yi koru
                    if k == "wemos_ip" and (v is None or not str(v).strip()):
                        continue
                    filtered_config[k] = v
                else:
                    log.warning(f"[CONFIG] Engellenen anahtar atlandı: {k}")
            if filtered_config != new_config:
                log.info(f"[CONFIG] Filtrelenen anahtarlar: {set(new_config.keys()) - ALLOWED_CONFIG_KEYS}")

            current_config = load_config() or {}
            current_config.update(filtered_config)

            if save_config(current_config):
                if 'wemos_ip' in filtered_config or 'wemos_port' in filtered_config:
                    set_connection_target(current_config.get('wemos_ip', ''),
                                          current_config.get('wemos_port', 7777), active=running)
                # Anında global bellek durumunu güncelle (UI senkronizasyon bug'ını çözer)
                for k, v in filtered_config.items():
                    update_status(k, v)

                # Toplam led sayısını hemen hesaplayıp senkronize et ki arayüzde değer geri zıplamasın
                c_top = current_config.get("top_leds", 0)
                c_bot = current_config.get("bottom_leds", 0)
                c_lft = current_config.get("left_leds", 0)
                c_rgt = current_config.get("right_leds", 0)
                update_status("total_leds", c_top + c_bot + c_lft + c_rgt)

                result = {"success": True, "message": "Konfigürasyon kaydedildi."}
            else:
                result = {"success": False, "message": "Konfigürasyon kaydedilemedi."}
        except Exception as e:
            result = {"success": False, "message": f"Hata: {str(e)}"}

        self._safe_send_json(result)

    def handle_test_wemos_connection(self):
        """Wemos bağlantı durumunu test eder (UDP + HTTP)"""
        try:
            status = get_status()
            wemos_ip = status.get('wemos_ip')
            wemos_port = status.get('wemos_port', 7777)

            if not wemos_ip:
                result = {"success": False, "message": "Wemos IP adresi ayarlanmamış", "udp_ok": False, "http_ok": False}
            else:
                token = connectivity.begin(wemos_ip, wemos_port)
                udp_ok, http_ok, message = test_wemos_connection(wemos_ip, wemos_port)
                applied = connectivity.complete(token, udp_ok, http_ok)
                result = {
                    "success": udp_ok or http_ok,
                    "message": message,
                    "udp_ok": udp_ok,
                    "http_ok": http_ok,
                    "applied": applied,
                    "ip": wemos_ip,
                    "port": wemos_port,
                }

        except Exception as e:
            result = {"success": False, "message": f"Test hatası: {str(e)}", "udp_ok": False, "http_ok": False}

        self._safe_send_json(result)

    def handle_restart(self):
        """Uygulamayı yeniden başlat"""
        result = {"success": True, "message": "Uygulama yeniden başlatılıyor..."}
        
        self._safe_send_json(result)
        
        # Uygulamayı yeniden başlat (kısa bir gecikme ile)
        threading.Timer(1.0, restart_app).start()

    def handle_wemos_restart(self):
        """Wemos'u yeniden başlat"""
        try:
            wemos_ip = get_status().get('wemos_ip')
            url = f"http://{wemos_ip}/restart"
            urllib.request.urlopen(url, timeout=2)
            result = {"success": True, "message": "Wemos yeniden başlatılıyor..."}
        except Exception as e:
            result = {"success": False, "message": f"Wemos hatası: {str(e)}"}
            
        self._safe_send_json(result)

    def handle_wemos_sleep(self):
        """Wemos uyku modunu değiştir"""
        try:
            wemos_ip = get_status().get('wemos_ip')
            url = f"http://{wemos_ip}/toggle_sleep"
            with urllib.request.urlopen(url, timeout=2) as response:
                resp_text = response.read().decode('utf-8').strip().upper()
                state = "AÇIK" if resp_text == "SLEEP_ON" else "KAPALI"
                result = {"success": True, "message": f"Wemos uyku modu: {state}", "state": resp_text}
        except Exception as e:
            result = {"success": False, "message": f"Wemos hatası: {str(e)}"}

        self._safe_send_json(result)

    def handle_wemos_reset_wifi(self):
        """Wemos Wi-Fi ayarlarını sıfırla"""
        try:
            wemos_ip = get_status().get('wemos_ip')
            url = f"http://{wemos_ip}/reset_wifi"
            urllib.request.urlopen(url, timeout=2)
            result = {"success": True, "message": "Wemos sıfırlanıyor, hotspot moduna dönülecek..."}
        except Exception as e:
            result = {"success": False, "message": f"Wemos iletişim hatası: {str(e)}"}

        self._safe_send_json(result)

    def handle_wemos_ota(self):
        """Wemos'a HTTP üzerinden firmware yükle"""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length == 0:
                self._safe_send_json({"success": False, "message": "Dosya boş"})
                return
            
            firmware_data = self.rfile.read(content_length)
            wemos_ip = get_status().get('wemos_ip')
            
            if not wemos_ip:
                self._safe_send_json({"success": False, "message": "Wemos IP adresi ayarlanmamış"})
                return
            
            # Wemos'un HTTP Update Server'ına firmware'ı gönder
            import urllib.request
            url = f"http://{wemos_ip}/firmware"
            
            # multipart/form-data olarak gönder
            boundary = '----FirmwareBoundary'
            body = []
            body.append(f'--{boundary}'.encode())
            body.append(b'Content-Disposition: form-data; name="firmware"; filename="firmware.bin"')
            body.append(b'Content-Type: application/octet-stream')
            body.append(b'')
            body.append(firmware_data)
            body.append(f'--{boundary}--'.encode())
            body.append(b'')
            
            data = b'\r\n'.join(body)
            
            req = urllib.request.Request(url, data=data, method='POST')
            req.add_header('Content-Type', f'multipart/form-data; boundary={boundary}')
            
            with urllib.request.urlopen(req, timeout=120) as response:
                result_text = response.read().decode('utf-8', errors='ignore')
                self._safe_send_json({"success": True, "message": f"Firmware yüklendi! Wemos yeniden başlatılıyor... ({result_text})"})
        except urllib.error.URLError as e:
            # Wemos güncelleme sonrası restart oluyor, bağlantı kopabilir
            self._safe_send_json({"success": True, "message": "Firmware gönderildi, Wemos yeniden başlatılıyor..."})
        except Exception as e:
            self._safe_send_json({"success": False, "message": f"Firmware yükleme hatası: {str(e)}"})

def restart_app():
    """Uygulamayı güvenli şekilde yeniden başlat"""
    global running
    running = False
    time.sleep(1)
    try:
        python = sys.executable
        subprocess.Popen([python] + sys.argv, close_fds=True)
    except Exception as e:
        print(f"[HATA] Yeniden başlatma başarısız: {e}")
    finally:
        os._exit(0)

def start_web_server(port=8888):
    """Web UI sunucusunu arka planda başlat"""
    try:
        server = HTTPServer(('127.0.0.1', port), WebUIHandler)
        server.daemon_threads = True
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        print(f"[WEB UI] http://localhost:{port} adresinde çalışıyor")
        return server
    except OSError as e:
        if "10048" in str(e) or "Address already in use" in str(e):
            # Port kullanılıyorsa alternatif port dene
            alt_port = port + 1
            print(f"[WEB UI] Port {port} kullanımda, {alt_port} deneniyor...")
            try:
                server = HTTPServer(('127.0.0.1', alt_port), WebUIHandler)
                server.daemon_threads = True
                server_thread = threading.Thread(target=server.serve_forever, daemon=True)
                server_thread.start()
                print(f"[WEB UI] http://localhost:{alt_port} adresinde çalışıyor")
                return server
            except Exception:
                print(f"[HATA] Web UI başlatılamadı!")
                return None
        else:
            print(f"[HATA] Web UI başlatılamadı: {e}")
            return None

# ============================================================
# TRAY İKON
# ============================================================

def create_icon_image():
    """Sistem tepsi ikonu için görüntü oluşturur"""
    img = Image.new('RGB', (64, 64), color=(100, 150, 255))
    return img

def quit_app(tray_icon, item):
    """Uygulamayı kapatır"""
    global running, icon
    running = False
    if tray_icon:
        tray_icon.stop()
    sys.exit(0)

def setup_tray_icon(config):
    """Sistem tepsi ikonunu oluşturur"""
    global icon
    
    if not PYSTRAY_AVAILABLE:
        return None
    
    image = create_icon_image()
    
    menu = pystray.Menu(
        item('Web Arayüzü', lambda ico, itm: open_web_ui(), default=True),
        item('Çıkış', quit_app)
    )
    
    icon = pystray.Icon(
        "AmbilightPC",
        image,
        "Ambilight PC - Çalışıyor",
        menu
    )
    
    return icon

def open_web_ui():
    """Web arayüzünü varsayılan tarayıcıda açar"""
    import webbrowser
    webbrowser.open(f'http://localhost:{BACKEND_PORT}')

# ============================================================
# KONSOL YÖNETİMİ
# ============================================================

def hide_console():
    """Console penceresini tamamen gizler"""
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32
        hwnd = kernel32.GetConsoleWindow()
        if hwnd:
            user32.ShowWindow(hwnd, 0)
            kernel32.FreeConsole()
    except Exception:
        pass

def show_console():
    """Console penceresini gösterir"""
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32
        hwnd = kernel32.GetConsoleWindow()
        if not hwnd:
            kernel32.AllocConsole()
            import msvcrt
            os.close(0)
            os.close(1)
            os.close(2)
            os.open('CONIN$', os.O_RDWR)
            os.open('CONOUT$', os.O_WRONLY)
            os.open('CONOUT$', os.O_WRONLY)
        else:
            user32.ShowWindow(hwnd, 1)
    except Exception:
        pass

# ============================================================
# AMBILIGHT WORKER
# ============================================================

def ping_wemos(ip, port=7777, timeout=3):
    """
    Wemos'a UDP üzerinden PING göndererek bağlantı durumunu kontrol eder.
    Wemos firmware'ında PING mesajına PONG ile yanıt veren handler var.
    Bu yöntem HTTP/TCP/ICMP'ye bağımlı değildir - doğrudan Wemos ile konuşur.
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        # Wemos'a PING gönder (wemos_code.ino satır 397-400)
        sock.sendto(b"PING", (ip, int(port)))
        # PONG yanıtını bekle
        data, addr = sock.recvfrom(64)
        sock.close()
        return data == b"PONG" and addr == (ip, int(port))
    except Exception:
        try:
            sock.close()
        except Exception:
            pass
        return False

def check_wemos_http(ip, timeout=2):
    """
    Wemos'un /status HTTP endpoint'ine GET isteği göndererek
    erişilebilirliğini kontrol eder. UDP engellendiğinde veya Wemos
    HTTP server'ı çalışıyorsa (port 80) yardımcı olur.
    """
    try:
        import urllib.request
        import json
        url = f"http://{ip}/status"
        req = urllib.request.Request(url, method='GET')
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if response.status == 200:
                data = json.loads(response.read().decode('utf-8'))
                # Wemos /status endpoint'inden temel alan doğrulama
                if isinstance(data, dict) and 'ip' in data:
                    return True
    except Exception:
        pass
    return False


def get_wemos_status(ip, timeout=2):
    """Bir Wemos'un firmware durumunu döndür; erişilemezse None."""
    try:
        url = f"http://{ip}/status"
        with urllib.request.urlopen(url, timeout=timeout) as response:
            data = json.loads(response.read().decode('utf-8'))
        return data if isinstance(data, dict) and 'ip' in data else None
    except Exception:
        return None


def get_device_validation(device, timeout=2):
    """PC kenar LED toplamını Wemos firmware /status değeriyle karşılaştır."""
    leds = device.get("leds", {}) if isinstance(device, dict) else {}
    expected_led_count = sum(int(leds.get(edge, 0)) for edge in ("top", "bottom", "left", "right"))
    status = get_wemos_status(device.get("ip", ""), timeout=timeout)
    firmware_led_count = status.get("led_count") if status else None
    compatible = isinstance(firmware_led_count, int) and firmware_led_count == expected_led_count
    if status is None:
        message = "Wemos /status erişilemiyor"
    elif compatible:
        message = "LED toplamı eşleşiyor"
    else:
        message = f"Uygulama: {expected_led_count}, firmware: {firmware_led_count} LED"
    return {
        "id": device.get("id"),
        "reachable": status is not None,
        "compatible": compatible,
        "expected_led_count": expected_led_count,
        "firmware_led_count": firmware_led_count,
        "message": message,
    }

def probe_wemos(ip, port=7777, full=False):
    """None means HTTP was not tested, not that it failed."""
    udp_ok = ping_wemos(ip, port=port, timeout=1.0)
    http_ok = check_wemos_http(ip, timeout=1.5) if full or not udp_ok else None
    return udp_ok, http_ok


def test_wemos_connection(ip, port=7777):
    """
    Wemos'a bağlantı durumu test eder:
    - UDP PING/PONG kontrolü (port 7777)
    - HTTP /status endpoint kontrolü (port 80)
    Returns tuple (udp_ok, http_ok, message)
    """
    udp_ok, http_ok = probe_wemos(ip, port, full=True)

    if udp_ok and http_ok:
        return True, True, f"✅ Her iki protokol da çalışıyor ({ip}:{port}, http://{ip}/status)"
    elif udp_ok:
        return True, False, f"✅ UDP PING/PONG çalışıyor ({ip}:{port}), HTTP endpoint erişilemiyor"
    elif http_ok:
        return False, True, f"✅ HTTP endpoint çalışıyor ({ip}/status), UDP PING/PONG erişilemiyor"
    else:
        return False, False, f"❌ Wemos'a bağlanılamıyor ({ip}:{port})"

def wemos_connectivity_checker(ip, port, cancel_event=None):
    """Probe the current target; canceled/obsolete results cannot publish."""
    cancel = cancel_event or threading.Event()
    while running and not cancel.is_set():
        token = connectivity.begin(ip, port)
        if token is None:
            return
        previous = connectivity.snapshot()['state']
        try:
            udp_ok, http_ok = probe_wemos(ip, port)
        except Exception as error:
            log.warning(f'[BAĞLANTI] Kontrol hatası ({ip}:{port}): {error}')
            udp_ok, http_ok = False, False
        if not running or cancel.is_set():
            return
        if connectivity.complete(token, udp_ok, http_ok):
            current = connectivity.snapshot()['state']
            if current != previous:
                log.info(f'[BAĞLANTI] {ip}:{port}: {previous} -> {current}')
        if cancel.wait(3):
            return


def ambilight_worker(config):
    """Ambilight'i arka planda çalıştıran thread fonksiyonu"""
    global running, sock, sct
    
    TOP_LEDS = config.get("top_leds", 34)
    BOTTOM_LEDS = config.get("bottom_leds", 34)
    LEFT_LEDS = config.get("left_leds", 20)
    RIGHT_LEDS = config.get("right_leds", 20)
    WEMOS_IP = config.get("wemos_ip", "") # Varsayılan BOŞ olabilir
    WEMOS_PORT = config.get("wemos_port", 7777)
    FPS = config.get("fps", 60)
    EDGE_WIDTH = config.get("edge_width", 20)
    EDGE_OFFSET = config.get("edge_offset", 0)
    IDLE_MODE = config.get("idle_mode", False)
    IDLE_USE_WINDOWS_COLOR = config.get("idle_use_windows_color", False)
    IDLE_COLOR = config.get("idle_color", "#ff0000")
    IDLE_BRIGHTNESS = config.get("idle_brightness", 50)
    FULLSCREEN_MAX_BRIGHTNESS = config.get("fullscreen_max_brightness", 255)
    
    TOTAL_LEDS = TOP_LEDS + BOTTOM_LEDS + LEFT_LEDS + RIGHT_LEDS
    
    sleep_time = 1.0 / FPS
    
    # Durum bilgisini güncelle
    update_status("wemos_ip", WEMOS_IP)
    update_status("wemos_port", WEMOS_PORT)
    update_status("top_leds", TOP_LEDS)
    update_status("bottom_leds", BOTTOM_LEDS)
    update_status("left_leds", LEFT_LEDS)
    update_status("right_leds", RIGHT_LEDS)
    update_status("total_leds", TOTAL_LEDS)
    update_status("fps", FPS)
    update_status("edge_width", EDGE_WIDTH)
    update_status("edge_offset", EDGE_OFFSET)
    update_status("idle_mode", IDLE_MODE)
    update_status("idle_use_windows_color", IDLE_USE_WINDOWS_COLOR)
    update_status("idle_color", IDLE_COLOR)
    update_status("idle_brightness", IDLE_BRIGHTNESS)
    update_status("fullscreen_max_brightness", FULLSCREEN_MAX_BRIGHTNESS)
    update_status("running", True)
    update_status("uptime_start", time.time())
    set_connection_target(WEMOS_IP, WEMOS_PORT, force=True)
    
    local_ip = get_local_ip()
    if local_ip:
        update_status("local_ip", local_ip)
        update_status("subnet", get_subnet_base(local_ip) + ".x")
    
    # v1.6.1: Her yeniden başlatmada sayaçları sıfırla
    packets_sent = 0
    errors = 0
    fps_counter = 0
    fps_timer = time.perf_counter()
    update_status("errors", 0)
    update_status("packets_sent", 0)
    update_status("actual_fps", 0)
    log.info(f"[WORKER] Ambilight worker başlatıldı (Hedef FPS: {FPS}, LED: {TOTAL_LEDS})")
    if WEMOS_IP:
        log.info(f"[WORKER] Wemos hedefi: {WEMOS_IP}:{WEMOS_PORT}")
    else:
        log.info("[WORKER] Wemos hedefi ayarlanmamış; IP bekleniyor...")

    # Wemos bağlantı kontrol thread'ini başlat (Eğer IP varsa)
    connectivity_thread = None
    connectivity_cancel = None
    if WEMOS_IP:
        connectivity_cancel = threading.Event()
        connectivity_thread = threading.Thread(
            target=wemos_connectivity_checker,
            args=(WEMOS_IP, WEMOS_PORT, connectivity_cancel),
            daemon=True
        )
        connectivity_thread.start()
    
    # Periyodik config kontrol sayacı
    config_check_timer = time.perf_counter()
    CONFIG_CHECK_INTERVAL = 3  # Her 3 saniyede bir config kontrol et
    multi_device_mode = False
    active_multi_devices = []
    
    timer_started = False
    try:
        try:
            timer_started = ctypes.windll.winmm.timeBeginPeriod(1) == 0
        except (AttributeError, OSError):
            pass
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sct = mss()
        frame_capture = FrameCapture(sct)
        monitor_version = monitor_catalog.version
        led_idx = get_led_monitor_index(sct)
        if not hasattr(sct, 'monitors') or led_idx >= len(sct.monitors) or led_idx <= 0:
            led_idx = get_primary_monitor_index(sct)
        update_status("led_monitor_index", led_idx)
        mon_name = sct.monitors[led_idx].get('name', f'Monitör {led_idx}') if (hasattr(sct, 'monitors') and led_idx < len(sct.monitors)) else 'Bilinmeyen'
        log.info(f"[MONITOR] Ambilight için kullanılacak monitör: {led_idx} ({mon_name})")

        while running:
            if monitor_catalog.version != monitor_version:
                # MSS is thread-local: rebuild on the capture thread after the button refresh.
                monitor_version = monitor_catalog.version
                replacement = None
                try:
                    replacement = mss()
                    replacement_capture = FrameCapture(replacement)
                    if len(replacement_capture.monitors) <= 1:
                        raise ValueError('Kullanılabilir monitör bulunamadı')
                    replacement_index = get_led_monitor_index(replacement)
                    if not 0 < replacement_index < len(replacement_capture.monitors):
                        replacement_index = get_primary_monitor_index(replacement)
                except Exception as error:
                    if replacement is not None:
                        replacement.close()
                    log.warning(f'[MONITOR] Yakalama yenilenemedi; mevcut ekran korunuyor: {error}')
                else:
                    previous_capture = sct
                    sct, frame_capture, led_idx = replacement, replacement_capture, replacement_index
                    previous_capture.close()
                    update_status('led_monitor_index', led_idx)
                    _fullscreen_cache['time'] = 0
            # Periyodik config kontrolü (IP değişikliğini canlı algıla)
            current_time_check = time.perf_counter()
            if current_time_check - config_check_timer >= CONFIG_CHECK_INTERVAL:
                config_check_timer = current_time_check
                new_config = load_config()
                if new_config:
                    # Canlı config güncelleme (Yeniden başlatmaya gerek kalmadan tüm ayarları yansıt)
                    new_top = new_config.get("top_leds", TOP_LEDS)
                    new_bottom = new_config.get("bottom_leds", BOTTOM_LEDS)
                    new_left = new_config.get("left_leds", LEFT_LEDS)
                    new_right = new_config.get("right_leds", RIGHT_LEDS)
                    new_width = new_config.get("edge_width", EDGE_WIDTH)
                    new_offset = new_config.get("edge_offset", EDGE_OFFSET)
                    new_idle_mode = new_config.get("idle_mode", IDLE_MODE)
                    new_idle_use_windows_color = new_config.get("idle_use_windows_color", IDLE_USE_WINDOWS_COLOR)
                    new_idle_color = new_config.get("idle_color", IDLE_COLOR)
                    new_idle_brightness = new_config.get("idle_brightness", IDLE_BRIGHTNESS)
                    new_fullscreen_max_brightness = new_config.get("fullscreen_max_brightness", FULLSCREEN_MAX_BRIGHTNESS)
                    new_fps = new_config.get("fps", FPS)
                    
                    if (new_top != TOP_LEDS or new_bottom != BOTTOM_LEDS or 
                        new_left != LEFT_LEDS or new_right != RIGHT_LEDS or 
                        new_width != EDGE_WIDTH or new_offset != EDGE_OFFSET or
                        new_idle_mode != IDLE_MODE or new_idle_color != IDLE_COLOR or new_idle_brightness != IDLE_BRIGHTNESS or
                        new_fullscreen_max_brightness != FULLSCREEN_MAX_BRIGHTNESS or
                        new_idle_use_windows_color != IDLE_USE_WINDOWS_COLOR or
                        new_fps != FPS):
                        old_total_leds = TOTAL_LEDS
                        
                        TOP_LEDS, BOTTOM_LEDS = new_top, new_bottom
                        LEFT_LEDS, RIGHT_LEDS = new_left, new_right
                        EDGE_WIDTH, EDGE_OFFSET = new_width, new_offset
                        IDLE_MODE, IDLE_COLOR, IDLE_BRIGHTNESS = new_idle_mode, new_idle_color, new_idle_brightness
                        FULLSCREEN_MAX_BRIGHTNESS = new_fullscreen_max_brightness
                        IDLE_USE_WINDOWS_COLOR = new_idle_use_windows_color
                        if new_fps != FPS:
                            FPS = new_fps
                            sleep_time = 1.0 / FPS
                            log.info(f"[CONFIG] Hedef FPS güncellendi: {FPS}")
                        TOTAL_LEDS = TOP_LEDS + BOTTOM_LEDS + LEFT_LEDS + RIGHT_LEDS
                        
                        # EĞER LED SAYISI AZALDIYSA: Cihaz üzerindeki eski, arkada kalan LED'leri söndürmek için Blackout paketi at.
                        if old_total_leds > TOTAL_LEDS and WEMOS_IP:
                            try:
                                blackout = bytearray([0] * (old_total_leds * 3))
                                for _ in range(3):
                                    sock.sendto(blackout, (WEMOS_IP, WEMOS_PORT))
                                    time.sleep(0.05)
                            except Exception: pass
                        
                        update_status("top_leds", TOP_LEDS)
                        update_status("bottom_leds", BOTTOM_LEDS)
                        update_status("left_leds", LEFT_LEDS)
                        update_status("right_leds", RIGHT_LEDS)
                        update_status("total_leds", TOTAL_LEDS)
                        update_status("edge_width", EDGE_WIDTH)
                        update_status("edge_offset", EDGE_OFFSET)
                        update_status("idle_mode", IDLE_MODE)
                        update_status("idle_use_windows_color", IDLE_USE_WINDOWS_COLOR)
                        update_status("idle_color", IDLE_COLOR)
                        update_status("idle_brightness", IDLE_BRIGHTNESS)
                        update_status("fullscreen_max_brightness", FULLSCREEN_MAX_BRIGHTNESS)
                        update_status("fps", FPS)
                        print("✓ (Worker) LED / Kenar / Bekleme Modu Konfigürasyonları Canlı Olarak Güncellendi.")
                    
                    new_mon_idx = os.environ.get("LED_MONITOR_INDEX") or new_config.get("led_monitor_index", get_primary_monitor_index(sct))
                    if new_mon_idx is not None and int(new_mon_idx) != led_idx:
                        led_idx = int(new_mon_idx)
                        if not 0 < led_idx < len(sct.monitors):
                            led_idx = get_primary_monitor_index(sct)
                        _fullscreen_cache['time'] = 0
                        update_status("led_monitor_index", led_idx)
                        log.info(f"[CONFIG] Hedef LED monitörü güncellendi: {led_idx}")

                    new_ip = new_config.get("wemos_ip", "")
                    new_port = int(new_config.get("wemos_port", 7777))
                    if (new_ip, new_port) != (WEMOS_IP, WEMOS_PORT):
                        WEMOS_IP, WEMOS_PORT = new_ip, new_port
                        if connectivity_cancel:
                            connectivity_cancel.set()
                        set_connection_target(WEMOS_IP, WEMOS_PORT)
                        connectivity_cancel = threading.Event()
                        if WEMOS_IP:
                            connectivity_thread = threading.Thread(
                                target=wemos_connectivity_checker,
                                args=(WEMOS_IP, WEMOS_PORT, connectivity_cancel),
                                daemon=True
                            )
                            connectivity_thread.start()

                    # Compatibility field: diagnostics must not capture unused screens.
                    update_status("shadow_device_frames", [])

                    multi_config = normalize_config_for_multi_device(new_config)
                    multi_device_mode = multi_config["multi_device_enabled"]
                    active_multi_devices = []
                    if multi_device_mode:
                        for multi_device in multi_config["wemos_devices"]:
                            if not multi_device.get("enabled"):
                                continue
                            validation = get_device_validation(multi_device)
                            if validation["compatible"]:
                                active_multi_devices.append(multi_device)
                            else:
                                log.warning(f"[MULTI] {multi_device['id']} devre dışı: {validation['message']}")
                    update_status("multi_device_enabled", multi_device_mode)
                    update_status("active_multi_devices", [device["id"] for device in active_multi_devices])

            # IP ayarlanmamışsa bekle
            if not WEMOS_IP and not multi_device_mode:
                time.sleep(1)
                continue

            # v1.6.1: Adaptive sleep - frame işlem süresini hesaba kat
            frame_start = time.perf_counter()

            try:
                data = bytearray()

                frame_capture.begin_frame()
                monitor = sct.monitors[led_idx] if 0 < led_idx < len(sct.monitors) else None
                is_full = is_fullscreen(monitor) if IDLE_MODE or FULLSCREEN_MAX_BRIGHTNESS < 255 else False

                if IDLE_MODE and not is_full:
                    if IDLE_USE_WINDOWS_COLOR:
                        current_color = get_windows_accent_color()
                    else:
                        current_color = IDLE_COLOR

                    ir, ig, ib = hex_to_rgb(current_color)
                    b_ratio = IDLE_BRIGHTNESS / 100.0
                    r = int(ir * b_ratio)
                    g = int(ig * b_ratio)
                    b = int(ib * b_ratio)
                    data = solid_frame(r, g, b, TOTAL_LEDS)
                elif not multi_device_mode:
                    brightness = FULLSCREEN_MAX_BRIGHTNESS if is_full else 255
                    data = grab_edge_frame(TOP_LEDS, BOTTOM_LEDS, LEFT_LEDS, RIGHT_LEDS,
                                           EDGE_WIDTH, EDGE_OFFSET, frame_capture, led_idx, brightness)

                if multi_device_mode:
                    for multi_device in active_multi_devices:
                        if IDLE_MODE and not is_full:
                            multi_data = solid_frame(r, g, b, sum(multi_device["leds"].values()))
                        else:
                            multi_data = build_device_frame(multi_device, EDGE_WIDTH, EDGE_OFFSET, frame_capture)
                        sock.sendto(multi_data, (multi_device["ip"], multi_device["port"]))
                        packets_sent += 1
                else:
                    sock.sendto(data, (WEMOS_IP, WEMOS_PORT))
                    packets_sent += 1
                fps_counter += 1

                # Her 2 saniyede bir FPS ve paket sayısını güncelle
                current_time = time.perf_counter()
                if current_time - fps_timer >= 2.0:
                    actual_fps = fps_counter / (current_time - fps_timer)
                    update_status("actual_fps", round(actual_fps, 1))
                    update_status("packets_sent", packets_sent)
                    fps_counter = 0
                    fps_timer = current_time
                    if actual_fps < 25:
                        log.warning(f"[WORKER] Düşük FPS uyarısı: {actual_fps:.1f} FPS (hedef: {FPS})")

                # v1.6.2: Adaptive sleep - timeBeginPeriod(1) sayesinde 1ms hassasiyetle uyuyabiliriz
                time.sleep(frame_delay(frame_start, time.perf_counter(), sleep_time))

            except Exception as e:
                errors += 1
                update_status("errors", errors)
                update_status("last_error", str(e))
                log.error(f"[WORKER] Frame hatası: {e}")
                if running:
                    time.sleep(1)  # Hata durumunda biraz bekle
                else:
                    break
    except Exception as e:
        update_status("connection", "bağlantı hatası")
        update_status("last_error", str(e))
    finally:
        if timer_started:
            try:
                ctypes.windll.winmm.timeEndPeriod(1)
            except (AttributeError, OSError):
                pass
        update_status("running", False)
        if connectivity_cancel:
            connectivity_cancel.set()
        set_connection_target(WEMOS_IP, WEMOS_PORT, active=False)
        if sock:
            sock.close()
        if sct:
            sct.close()

# ============================================================
# ANA PROGRAM
# ============================================================

def main():
    """Ana program"""
    global running, ambilight_thread, icon
    monitor_catalog.refresh(get_available_monitors)
    
    print("\n" + "="*60)
    print("  AMBILIGHT PC v1.6.4.2 - Windows")
    print("="*60)
    
    # Konfigürasyonu yükle
    config = load_config()

    if not config:
        print("\n⚠ Konfigürasyon bulunamadı. Varsayılan ayarlarla başlatılıyor...")
        print("💡 Lütfen Web Arayüzü (http://localhost:8888) üzerinden 'Ağda Ara' butonunu kullanarak Wemos'u bulun ve ayarlarınızı yapıp KAYDET butonuna basın.")
        
        # Varsayılan konfigürasyon (Otomatik arama KALDIRILDI)
        config = {
            "top_leds": 20,
            "bottom_leds": 0,
            "left_leds": 15,
            "right_leds": 15,
            "wemos_ip": "",  # BOŞ BAŞLASIN (Yanlış yere bağlanmasın)
            "wemos_port": 7777,
            "fps": 60,
            "edge_width": 20,
            "edge_offset": 0,
            "fullscreen_max_brightness": 255
        }

    # Konfigürasyon değerlerini al
    TOP_LEDS = config.get("top_leds", 34)
    BOTTOM_LEDS = config.get("bottom_leds", 34)
    LEFT_LEDS = config.get("left_leds", 20)
    RIGHT_LEDS = config.get("right_leds", 20)
    WEMOS_IP = config.get("wemos_ip", "") # Varsayılan boş
    WEMOS_PORT = config.get("wemos_port", 7777)
    FPS = config.get("fps", 60)
    EDGE_WIDTH = config.get("edge_width", 20)
    EDGE_OFFSET = config.get("edge_offset", 0)
    TOTAL_LEDS = TOP_LEDS + RIGHT_LEDS + BOTTOM_LEDS + LEFT_LEDS
    
    print(f"\n📺 LED Konfigürasyonu:")
    print(f"   Üst: {TOP_LEDS} | Alt: {BOTTOM_LEDS} | Sol: {LEFT_LEDS} | Sağ: {RIGHT_LEDS}")
    print(f"   Toplam: {TOTAL_LEDS} LED")
    
    if WEMOS_IP:
        print(f"\n📡 Wemos Hedef: {WEMOS_IP}:{WEMOS_PORT}")
    else:
        print(f"\n📡 Wemos Hedef: [AYARLANMADI] - Lütfen arayüzden ayarlayın.")

    local_ip = get_local_ip()
    if local_ip:
        print(f"🖥️  Yerel IP: {local_ip}")
        print(f"🌐 Subnet: {get_subnet_base(local_ip)}.x")
    
    print(f"🎯 Hedef FPS: {FPS}")

    # Durum bilgisini güncelle
    update_status("wemos_ip", WEMOS_IP)
    update_status("wemos_port", WEMOS_PORT)
    update_status("top_leds", TOP_LEDS)
    update_status("bottom_leds", BOTTOM_LEDS)
    update_status("left_leds", LEFT_LEDS)
    update_status("right_leds", RIGHT_LEDS)
    update_status("total_leds", TOTAL_LEDS)
    update_status("fps", FPS)
    update_status("edge_width", EDGE_WIDTH)
    update_status("edge_offset", EDGE_OFFSET)
    update_status("running", True)
    update_status("uptime_start", time.time())
    update_status("connection", "bağlanıyor" if WEMOS_IP else "IP bekleniyor...")
    if local_ip:
        update_status("local_ip", local_ip)
        update_status("subnet", get_subnet_base(local_ip) + ".x")
    
    # Web UI sunucusunu başlat
    print(f"\n{'='*60}")
    web_server = start_web_server(BACKEND_PORT)
    if web_server:
        print(f"🌐 Web Arayüzü: http://localhost:{BACKEND_PORT}")
    print(f"{'='*60}\n")
    
    # NOT: Otomatik başlatma sadece Electron tarafından ilk kurulumda ayarlanır.
    # Kullanıcı görev yöneticisinden kapatırsa tekrar açılmaz.
    
    # Ambilight thread'ini başlat
    running = True
    ambilight_thread = threading.Thread(target=ambilight_worker, args=(config,), daemon=False)
    ambilight_thread.start()
    
    # Sistem tepsi ikonunu oluştur (--no-tray argümanı yoksa)
    no_tray = '--no-tray' in sys.argv
    if not no_tray and PYSTRAY_AVAILABLE:
        icon = setup_tray_icon(config)
        if icon:
            icon.run()
        else:
            _run_console_mode(config, TOTAL_LEDS, WEMOS_IP, WEMOS_PORT, FPS)
    else:
        _run_console_mode(config, TOTAL_LEDS, WEMOS_IP, WEMOS_PORT, FPS)

def _run_console_mode(config, total_leds, wemos_ip, wemos_port, fps):
    """Tray icon olmadan konsol modunda çalışır"""
    global running
    
    print("Ambilight çalışıyor... Ctrl+C ile kapatabilirsiniz.")
    print(f"Web Arayüzü: http://localhost:{BACKEND_PORT}\n")
    
    try:
        while running:
            time.sleep(1)
    except KeyboardInterrupt:
        running = False
        print("\nKapatılıyor...")
        sys.exit(0)

if __name__ == "__main__":
    if sys.platform == 'win32' and getattr(sys, 'frozen', False):
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
        except Exception:
            pass
    
    main()

