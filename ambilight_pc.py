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
import hmac
import uuid
import logging
from datetime import datetime, timezone
from diagnostic_logs import DiagnosticLogHandler, HISTORY_LIMIT, STREAM_THREAD
from wemos_diagnostics import WemosDiagnostics
from windows_audit_session import WindowsAuditSession
from windows_wfp import TCP, UDP, WfpDiagnostics, transport_error
from stream_diagnostics import StreamDiagnostics
from frame_processing import FrameCapture, FrameTimings, edge_colors, edge_rgb, pack_rgb, solid_frame, frame_delay
from monitor_catalog import MonitorCatalog
from connectivity import Connectivity, describe_probe, legacy_connection, next_probe_at, probe_error, probe_result

monitor_catalog = MonitorCatalog()
APP_VERSION = "1.7.4"
BACKEND_PORT = int(os.environ.get('LUXEDGE_BACKEND_PORT', '8888'))
# Electron portu kendisi seçip ortam değişkeniyle verir; o zaman başka porta kaymak yasak.
BACKEND_PORT_STRICT = 'LUXEDGE_BACKEND_PORT' in os.environ
WINDOWS_DIAGNOSTICS_TOKEN = os.environ.pop('LUXEDGE_WINDOWS_DIAGNOSTICS_TOKEN', '').encode('utf-8')
LIVE_LOG_TOKEN = os.environ.pop('LUXEDGE_LIVE_LOG_TOKEN', '').encode('utf-8')

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
from device_overview import device_overview

# ============================================================
# LOG SİSTEMİ
# ============================================================

def setup_logger():
    logger = logging.getLogger('luxedge')
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    log_dir = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'LuxEdge')
    if not any(isinstance(handler, DiagnosticLogHandler) for handler in logger.handlers):
        handler = DiagnosticLogHandler(log_dir)
        handler.setFormatter(logging.Formatter('[%(levelname)s] %(message)s'))
        logger.addHandler(handler)
    return logger


log = setup_logger()
log_history = next(handler for handler in log.handlers if isinstance(handler, DiagnosticLogHandler))
windows_audit_session = WindowsAuditSession(log)
wfp_diagnostics = WfpDiagnostics(log, audit_session=windows_audit_session)

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

# Cihaz rolleri:
#   ambilight : Dört kenar düzeni; Wemos'taki tüm şeritler tek zincir gibi akış alır.
#   strips    : Her fiziksel şerit ayrı görev alır (ekran kenarı akışı, sabit renk, animasyon).
DEVICE_ROLES = ("ambilight", "strips")
# Firmware'deki SegmentMode isimleriyle birebir aynı olmalı (wemos_code.ino).
STRIP_MODES = ("stream", "off", "solid", "breathe", "rainbow", "chase", "twinkle")
LOCAL_STRIP_MODES = tuple(mode for mode in STRIP_MODES if mode != "stream")
EDGES = ("top", "bottom", "left", "right")
FIRMWARE_STRIP_COUNT = 4
FIRMWARE_MAX_LED_COUNT = 170

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
            log.error("[CONFIG] Konfigürasyon dosyası okunamadı: %s", e, extra={
                "code": "config_read_failed",
                "explanation": "PC, LuxEdge ayar dosyasını okuyamadı.",
                "possible_causes": ["Nedeni belirlenemedi"],
            })
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
        log.error("[CONFIG] Konfigürasyon dosyası kaydedilemedi: %s", e, extra={
            "code": "config_save_failed",
            "explanation": "PC, LuxEdge ayar dosyasına yazamadı.",
            "possible_causes": ["Nedeni belirlenemedi"],
        })
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
    else:
        # Ana Wemos kaydı tekli panelden yönetilir; diske yazılmış eski bir
        # kopyası varsa IP/LED alanlarını her zaman tekli ayarlardan tazele.
        primary_device = legacy_config_to_primary_device(normalized)
        for index, device in enumerate(normalized["wemos_devices"]):
            if not isinstance(device, dict) or device.get("id") != LEGACY_PRIMARY_DEVICE_ID:
                continue
            if primary_device is None:
                continue
            refreshed = dict(device)
            for key in ("ip", "port", "monitor_index", "leds"):
                refreshed[key] = primary_device[key]
            normalized["wemos_devices"][index] = refreshed

    return normalized


def _hex_color(value, field_name):
    text = str(value or "").strip().lower()
    if not text.startswith("#"):
        text = "#" + text
    if len(text) != 7 or any(ch not in "0123456789abcdef" for ch in text[1:]):
        raise ValueError(f"{field_name} #rrggbb biçiminde olmalı")
    return text


def validate_strip_settings(payload, index=None):
    """Tek bir şeridin PC tarafındaki ayarlarını doğrula.

    led_count ve pin firmware'den gelir (hotspot kurulumunda seçilir); burada
    yalnız saklanır. Mod, renk, parlaklık, hız ve akış kaynağı kullanıcı seçimidir.
    """
    if not isinstance(payload, dict):
        raise ValueError("Şerit verisi JSON nesnesi olmalı")
    strip_index = _device_integer(payload.get("index", index), "strips.index", 1, FIRMWARE_STRIP_COUNT)
    mode = str(payload.get("mode", "stream")).strip().lower()
    if mode not in STRIP_MODES:
        raise ValueError(f"Şerit {strip_index} için geçersiz mod")
    edge = str(payload.get("edge", "top")).strip().lower()
    if edge not in EDGES:
        raise ValueError(f"Şerit {strip_index} için geçersiz kenar")
    reverse = payload.get("reverse", False)
    if not isinstance(reverse, bool):
        raise ValueError("reverse doğru veya yanlış olmalı")
    pin = str(payload.get("pin", "")).strip().upper()
    if len(pin) > 3:
        raise ValueError("pin en fazla 3 karakter olabilir")
    return {
        "index": strip_index,
        "led_count": _device_integer(payload.get("led_count", 0), "strips.led_count", 0, FIRMWARE_MAX_LED_COUNT),
        "pin": pin,
        "mode": mode,
        "color": _hex_color(payload.get("color", "#ff7800"), "strips.color"),
        "brightness": _device_integer(payload.get("brightness", 128), "strips.brightness", 0, 255),
        "speed": _device_integer(payload.get("speed", 50), "strips.speed", 1, 100),
        "monitor_index": _device_integer(payload.get("monitor_index", 1), "strips.monitor_index", 1),
        "edge": edge,
        "reverse": reverse,
    }


def validate_strips(payload):
    if payload is None:
        return []
    if not isinstance(payload, list):
        raise ValueError("strips bir liste olmalı")
    strips = []
    seen = set()
    for position, item in enumerate(payload, start=1):
        strip = validate_strip_settings(item, index=position)
        if strip["index"] in seen:
            raise ValueError(f"Şerit {strip['index']} birden fazla kez tanımlı")
        seen.add(strip["index"])
        strips.append(strip)
    strips.sort(key=lambda strip: strip["index"])
    return strips


def device_role(device):
    role = device.get("role", "ambilight") if isinstance(device, dict) else "ambilight"
    return role if role in DEVICE_ROLES else "ambilight"


def device_strips(device):
    """Cihazın şerit listesini (eksikse boş) döndür."""
    strips = device.get("strips") if isinstance(device, dict) else None
    return strips if isinstance(strips, list) else []


def stream_strips(device):
    """Ekran akışı alan şeritler (firmware sırasıyla)."""
    return [strip for strip in device_strips(device)
            if strip.get("mode", "stream") == "stream" and int(strip.get("led_count", 0)) > 0]


def expected_stream_led_count(device):
    """PC'nin bu cihaza her karede göndereceği LED sayısı."""
    if device_role(device) == "strips":
        return sum(int(strip.get("led_count", 0)) for strip in stream_strips(device))
    leds = device.get("leds", {}) if isinstance(device, dict) else {}
    return sum(int(leds.get(edge, 0)) for edge in EDGES)


def merge_firmware_strips(device, firmware_status):
    """Firmware'in bildirdiği şerit sayısı/pin bilgisini PC kaydına işle.

    Kullanıcının seçtiği mod/renk korunur; PC'de kaydı olmayan şeritler için
    firmware'in o anki modu (varsa) devralınır. Diske yazmaz.
    """
    merged = copy.deepcopy(device)
    existing = {int(strip.get("index", 0)): strip for strip in device_strips(device)}
    result = []
    for firmware_strip in firmware_status.get("strips", []) or []:
        if not isinstance(firmware_strip, dict):
            continue
        try:
            index = int(firmware_strip.get("index", 0))
        except (TypeError, ValueError):
            continue
        if not 1 <= index <= FIRMWARE_STRIP_COUNT or not firmware_strip.get("enabled"):
            continue
        strip = dict(existing.get(index) or {"index": index})
        strip["led_count"] = int(firmware_strip.get("led_count", 0) or 0)
        strip["pin"] = str(firmware_strip.get("led_pin", "") or "")
        if index not in existing:
            for key in ("mode", "color", "brightness", "speed"):
                if key in firmware_strip:
                    strip[key] = firmware_strip[key]
        result.append(validate_strip_settings(strip, index=index))
    merged["strips"] = result
    return merged


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
    if role not in DEVICE_ROLES:
        raise ValueError("Cihaz rolü 'ambilight' veya 'strips' olmalı")

    leds_payload = payload.get("leds", {})
    if leds_payload is None:
        leds_payload = {}
    if not isinstance(leds_payload, dict):
        raise ValueError("leds nesnesi gerekli")
    leds = {
        edge: _device_integer(leds_payload.get(edge, 0), f"leds.{edge}")
        for edge in ("top", "bottom", "left", "right")
    }

    enabled = payload.get("enabled", default_enabled)
    if not isinstance(enabled, bool):
        raise ValueError("enabled doğru veya yanlış olmalı")

    strips = validate_strips(payload.get("strips"))
    total_strip_leds = sum(strip["led_count"] for strip in strips)
    if total_strip_leds > FIRMWARE_MAX_LED_COUNT:
        raise ValueError(f"Şerit LED toplamı {FIRMWARE_MAX_LED_COUNT} değerini aşamaz")

    return {
        "id": device_id or uuid.uuid4().hex,
        "name": name,
        "ip": ip,
        "port": _device_integer(payload.get("port", 7777), "port", 1, 65535),
        "role": role,
        "monitor_index": _device_integer(payload.get("monitor_index", 1), "monitor_index", 1),
        "leds": leds,
        "strips": strips,
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
                    log.warning("[TARAMA] UDP yayın hatası (%s): %s", target_ip, e)
            
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
                    log.warning("[TARAMA] UDP dinleme hatası: %s", e)
                    break
        
        sock.close()
    except Exception as e:
        log.error("[TARAMA] UDP soket hatası: %s", e)

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
                log.warning("[TARAMA] Unicast tarama hatası: %s", e)

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



def build_device_frame(device, edge_width, edge_offset, capture, brightness=255):
    """Bir cihazın atanmış monitörü için UDP RGB paketini hesapla.

    Bu yardımcı yalnız paket üretir; gönderim ve bağlantı kontrolü yapmaz.
    Şerit bazlı cihazlarda yalnız 'stream' modundaki şeritler, firmware
    sırasıyla art arda eklenir; yerel modlar cihaz üzerinde çalışır.
    """
    if device_role(device) == "strips":
        parts = []
        for strip in stream_strips(device):
            counts = {edge: 0 for edge in EDGES}
            counts[strip.get("edge", "top")] = int(strip.get("led_count", 0))
            frame = grab_edge_frame(
                counts["top"], counts["bottom"], counts["left"], counts["right"],
                edge_width, edge_offset, capture,
                monitor_index=int(strip.get("monitor_index", 1)), brightness=brightness,
            )
            if strip.get("reverse"):
                frame = reverse_rgb_frame(frame)
            parts.append(frame)
        return b"".join(parts)

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
        brightness=brightness,
    )


def reverse_rgb_frame(frame):
    """RGB üçlülerinin sırasını ters çevir (şerit ters takılmışsa)."""
    if len(frame) < 6:
        return bytes(frame)
    return np.frombuffer(frame, dtype=np.uint8).reshape(-1, 3)[::-1].tobytes()


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
        previous = connectivity.snapshot()
        connectivity.reset(ip, port, active=active, force=force)
        app_status['wemos_ip'] = ip
        app_status['wemos_port'] = int(port)
    if (previous['ip'], previous['port']) != (ip or '', int(port)):
        log.info('[BAĞLANTI] Hedef değişti: %s:%s → %s:%s',
                 previous['ip'] or 'ayarsız', previous['port'], ip or 'ayarsız', port, extra={
                     'code': 'connectivity_target_changed', 'source': 'pc',
                     'details': {'previous_ip': previous['ip'], 'previous_port': previous['port'],
                                 'ip': ip or '', 'port': int(port)},
                 })

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
    
    def _safe_send_json(self, data, *, allow_origin=True):
        """JSON yanıtını güvenli şekilde gönder (BrokenPipeError koruması)"""
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            if allow_origin:
                self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # İstemci bağlantıyı kapattı, sorun değil

    def handle_windows_diagnostics(self, operation):
        supplied = self.headers.get('X-LuxEdge-Windows-Diagnostics-Token', '').encode('utf-8')
        if not WINDOWS_DIAGNOSTICS_TOKEN or not hmac.compare_digest(WINDOWS_DIAGNOSTICS_TOKEN, supplied):
            self.send_error(403)
            return
        if operation == 'start':
            result = windows_audit_session.start()
        elif operation == 'stop':
            result = windows_audit_session.stop()
        else:
            result = windows_audit_session.status()
        self._safe_send_json(result)
    
    def do_GET(self):
        try:
            if self.path == '/' or self.path == '/index.html':
                self.serve_html()
            elif self.path == '/api/status':
                self.serve_status()
            elif self.path == '/api/windows-diagnostics':
                self.handle_windows_diagnostics('status')
            elif self.path == '/api/devices':
                self.handle_devices_list()
            elif self.path == '/api/devices/scan':
                self.handle_devices_scan()
            elif self.path == '/api/devices/validation':
                self.handle_devices_validation()
            elif self.path == '/api/devices/overview':
                self.handle_devices_overview()
            elif self.path == '/api/scan':
                self.serve_scan()
            elif self.path.split('?', 1)[0] == '/api/logs':
                self.serve_logs()
            elif self.path.split('?', 1)[0] == '/api/live-log-rows':
                self.serve_live_log_rows()
            elif self.path.startswith('/api/test-wemos-connection'):
                self.handle_test_wemos_connection()
            elif self.path.startswith('/api/wemos/status'):
                self.handle_wemos_status()
            else:
                self.send_error(404)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
    
    def do_POST(self):
        try:
            if self.path == '/api/config':
                self.handle_config_update()
            elif self.path == '/api/windows-diagnostics/start':
                self.handle_windows_diagnostics('start')
            elif self.path == '/api/windows-diagnostics/stop':
                self.handle_windows_diagnostics('stop')
            elif self.path == '/api/monitors/refresh':
                self._safe_send_json(monitor_catalog.refresh(get_available_monitors))
            elif self.path == '/api/devices/mode':
                self.handle_devices_mode()
            elif self.path == '/api/devices':
                self.handle_device_create()
            elif self._device_action_from_path() is not None:
                device_id, action = self._device_action_from_path()
                if action == 'sync':
                    self.handle_device_sync(device_id)
                else:
                    self.send_error(404)
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

    def _query_param(self, name):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(getattr(self, 'path', '') or '').query)
        values = query.get(name)
        return values[0].strip() if values else ""

    def _target_wemos_ip(self, payload=None):
        """İstek bir cihaz IP'si belirtiyorsa onu, yoksa Ana Wemos'u hedefle."""
        ip = ""
        if isinstance(payload, dict):
            ip = str(payload.get("ip", "") or "").strip()
        if not ip:
            ip = self._query_param("ip")
        if ip:
            try:
                if ipaddress.ip_address(ip).version != 4:
                    raise ValueError
            except ValueError:
                raise ValueError("Geçerli bir IPv4 adresi girin")
            return ip
        return get_status().get('wemos_ip') or ""

    def _read_optional_json_body(self):
        try:
            return self._read_json_body()
        except ValueError:
            return {}

    def _device_id_from_path(self):
        path = urllib.parse.urlparse(self.path).path
        prefix = "/api/devices/"
        if not path.startswith(prefix):
            return None
        device_id = urllib.parse.unquote(path[len(prefix):])
        return device_id if device_id and "/" not in device_id else None

    def _device_action_from_path(self):
        """/api/devices/{id}/{action} -> (id, action); eşleşmezse None."""
        path = urllib.parse.urlparse(self.path).path
        prefix = "/api/devices/"
        if not path.startswith(prefix):
            return None
        parts = path[len(prefix):].split("/")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            return None
        return urllib.parse.unquote(parts[0]), parts[1]
    
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
        status['backend_version'] = APP_VERSION
        status['backend_port'] = BACKEND_PORT
        status['backend_pid'] = os.getpid()

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
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        try:
            limit = max(1, min(HISTORY_LIMIT, int(params.get('limit', [HISTORY_LIMIT])[0])))
            after = max(0, int(params['after'][0])) if 'after' in params else None
        except ValueError:
            limit, after = HISTORY_LIMIT, None
        self._safe_send_json(log_history.snapshot(after, limit))

    def serve_live_log_rows(self):
        supplied = self.headers.get('X-LuxEdge-Live-Log-Token', '').encode('utf-8')
        if not LIVE_LOG_TOKEN or not hmac.compare_digest(LIVE_LOG_TOKEN, supplied):
            self.send_error(403)
            return
        try:
            params = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query, max_num_fields=3)
            since, through, after = (int(params[key][0]) for key in ('since', 'through', 'after'))
            if not 0 <= after < 2**53 or not 0 < since <= through < 2**53:
                raise ValueError
        except (ValueError, KeyError):
            self.send_error(400)
            return
        self._safe_send_json(log_history.export_snapshot(since, through, after), allow_origin=False)

    def handle_devices_list(self):
        """Pasif çoklu-cihaz kayıtlarını döndür; tekli config'i değiştirme."""
        config = normalize_config_for_multi_device(load_config() or {})
        devices = [dict(device, role=device_role(device), strips=device_strips(device))
                   for device in config["wemos_devices"] if isinstance(device, dict)]
        self._safe_send_json({
            "success": True,
            "schema_version": config["schema_version"],
            "multi_device_enabled": config["multi_device_enabled"],
            "strip_modes": list(STRIP_MODES),
            "devices": devices,
        })

    def handle_devices_scan(self):
        """Ağdaki cihazları listeler; kullanıcı seçmeden config'e yazmaz."""
        devices = [{"ip": ip, "port": 7777} for ip in find_all_wemos_on_network()]
        self._safe_send_json({"success": True, "devices": devices})

    def handle_devices_overview(self):
        config = normalize_config_for_multi_device(load_config() or {})
        self._safe_send_json({
            "success": True,
            "multi_device_enabled": config["multi_device_enabled"],
            "devices": device_overview.snapshot(config["wemos_devices"], get_wemos_status),
        })

    def handle_devices_validation(self):
        """Kayıtlı cihazların firmware LED toplamlarını doğrular."""
        config = normalize_config_for_multi_device(load_config() or {})
        validations = [get_device_validation(device) for device in config["wemos_devices"]]
        invalidate_device_validation()
        self._safe_send_json({"success": True, "devices": validations})

    def handle_devices_mode(self):
        try:
            payload = self._read_json_body()
            config = set_multi_device_mode(load_config() or {}, payload.get("enabled"))
            if not save_config(config):
                raise ValueError("Çoklu mod kaydedilemedi")
            invalidate_device_validation()
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
            invalidate_device_validation(device_id)
            result = {"success": True, "device": device, "message": "Cihaz güncellendi."}
        except Exception as e:
            result = {"success": False, "message": str(e)}
        self._safe_send_json(result)

    def handle_device_delete(self, device_id):
        try:
            config = delete_wemos_device(load_config() or {}, device_id)
            if not save_config(config):
                raise ValueError("Cihaz kaydedilemedi")
            invalidate_device_validation(device_id)
            result = {"success": True, "message": "Cihaz silindi."}
        except Exception as e:
            result = {"success": False, "message": str(e)}
        self._safe_send_json(result)

    def handle_device_sync(self, device_id):
        """Wemos'tan şerit bilgisini al, ayarları gönder ve kaydı güncelle."""
        try:
            try:
                payload = self._read_json_body()
            except ValueError:
                payload = {}
            push = payload.get("push", True) if isinstance(payload, dict) else True
            config, device, outcome = sync_wemos_device(load_config() or {}, device_id, push=bool(push))
            if not save_config(config):
                raise ValueError("Cihaz kaydedilemedi")
            result = {"success": True, "device": device, **outcome}
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

    def handle_wemos_status(self):
        """Seçilen Wemos'un firmware /status çıktısını döndür (tanılama)."""
        try:
            ip = self._target_wemos_ip()
            if not ip:
                raise ValueError("Wemos IP adresi ayarlanmamış")
            status = get_wemos_status(ip, timeout=3)
            if status is None:
                result = {"success": False, "ip": ip, "message": "Wemos /status erişilemiyor"}
            else:
                result = {"success": True, "ip": ip, "status": status}
        except Exception as e:
            result = {"success": False, "message": str(e)}
        self._safe_send_json(result)

    def handle_test_wemos_connection(self):
        """Wemos bağlantı durumunu test eder (UDP + HTTP)"""
        try:
            status = get_status()
            wemos_ip = status.get('wemos_ip')
            wemos_port = status.get('wemos_port', 7777)
            requested_ip = self._query_param("ip")

            if requested_ip and requested_ip != wemos_ip:
                # Ek cihaz: ana bağlantı izleyicisine dokunmadan doğrudan test et.
                requested_port = int(self._query_param("port") or 7777)
                udp_ok, http_ok, message = test_wemos_connection(requested_ip, requested_port)
                result = {"success": udp_ok or http_ok, "message": message, "udp_ok": udp_ok,
                          "http_ok": http_ok, "applied": None, "ip": requested_ip, "port": requested_port}
            elif not wemos_ip:
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
        """Wemos'u yeniden başlat (gövdede ip verilirse o cihaz)"""
        try:
            wemos_ip = self._target_wemos_ip(self._read_optional_json_body())
            url = f"http://{wemos_ip}/restart"
            urllib.request.urlopen(url, timeout=2)
            result = {"success": True, "message": "Wemos yeniden başlatılıyor..."}
        except Exception as e:
            result = {"success": False, "message": f"Wemos hatası: {str(e)}"}
            
        self._safe_send_json(result)

    def handle_wemos_sleep(self):
        """Wemos uyku modunu değiştir (gövdede ip verilirse o cihaz)"""
        try:
            wemos_ip = self._target_wemos_ip(self._read_optional_json_body())
            url = f"http://{wemos_ip}/toggle_sleep"
            with urllib.request.urlopen(url, timeout=2) as response:
                resp_text = response.read().decode('utf-8').strip().upper()
                state = "AÇIK" if resp_text == "SLEEP_ON" else "KAPALI"
                result = {"success": True, "message": f"Wemos uyku modu: {state}", "state": resp_text}
        except Exception as e:
            result = {"success": False, "message": f"Wemos hatası: {str(e)}"}

        self._safe_send_json(result)

    def handle_wemos_reset_wifi(self):
        """Wemos Wi-Fi ayarlarını sıfırla (gövdede ip verilirse o cihaz)"""
        try:
            wemos_ip = self._target_wemos_ip(self._read_optional_json_body())
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
        log.error("[PC] Yeniden başlatma başarısız: %s", e)
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
        if BACKEND_PORT_STRICT:
            log.error(f"[WEB UI] Port {port} kullanımda; Electron'un verdiği port dışına çıkılmaz. "
                      f"Başka bir LuxEdge (tepsi) çalışıyor olabilir: {e}")
            return None
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
                log.error("[WEB UI] Web UI başlatılamadı")
                return None
        else:
            log.error("[WEB UI] Web UI başlatılamadı: %s", e)
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

def socket_local_address(sock):
    try:
        return sock.getsockname()
    except OSError:
        return None


def ping_wemos(ip, port=7777, timeout=3, observation=None):
    """
    Wemos'a UDP üzerinden PING göndererek bağlantı durumunu kontrol eder.
    Wemos firmware'ında PING mesajına PONG ile yanıt veren handler var.
    Bu yöntem HTTP/TCP/ICMP'ye bağımlı değildir - doğrudan Wemos ile konuşur.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            started = time.time()
            directions = ('outbound',)
            try:
                sock.sendto(b"PING", (ip, int(port)))
                directions = ('outbound', 'inbound')
                data, addr = sock.recvfrom(64)
            except OSError:
                wfp_diagnostics.submit('UDP PING/PONG', ip, int(port), UDP, started,
                                       directions=directions, local=socket_local_address(sock))
                raise
            valid = data == b"PONG" and addr == (ip, int(port))
            if observation is not None and not valid:
                observation['failure_reason'] = 'unexpected_pong'
            return valid
    except Exception as error:
        if observation is not None:
            observation.update(probe_error(error))
        return False

def check_wemos_http(ip, timeout=2, observation=None):
    """
    Wemos'un /status HTTP endpoint'ine GET isteği göndererek
    erişilebilirliğini kontrol eder. UDP engellendiğinde veya Wemos
    HTTP server'ı çalışıyorsa (port 80) yardımcı olur.
    """
    started = time.time()
    try:
        import urllib.request
        import json
        url = f"http://{ip}/status"
        req = urllib.request.Request(url, method='GET')
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if observation is not None:
                observation['http_status'] = response.status
            if response.status == 200:
                data = json.loads(response.read().decode('utf-8'))
                # Wemos /status endpoint'inden temel alan doğrulama
                if isinstance(data, dict) and 'ip' in data:
                    return True
                if observation is not None:
                    observation['failure_reason'] = 'invalid_status'
    except Exception as error:
        if transport_error(error):
            wfp_diagnostics.submit('HTTP /status', ip, 80, TCP, started)
        if observation is not None:
            observation.update(probe_error(error))
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


def firmware_supports_segments(status):
    return isinstance(status, dict) and bool(status.get("segments_supported")) and "stream_led_count" in status


def get_device_validation(device, timeout=2, status=None):
    """PC'nin göndereceği akış LED sayısını Wemos firmware değeriyle karşılaştır.

    Yeni firmware (1.7+) 'stream_led_count' bildirir: yalnız akış modundaki
    şeritlerin toplamı. Eski firmware için 'led_count' kullanılır.
    """
    device = device if isinstance(device, dict) else {}
    role = device_role(device)
    expected_led_count = expected_stream_led_count(device)
    if status is None:
        status = get_wemos_status(device.get("ip", ""), timeout=timeout)
    supports_segments = firmware_supports_segments(status)
    firmware_led_count = None
    if status:
        firmware_led_count = status.get("stream_led_count") if supports_segments else status.get("led_count")
    compatible = isinstance(firmware_led_count, int) and firmware_led_count == expected_led_count
    needs_segments = role == "strips" and any(
        strip.get("mode", "stream") != "stream" for strip in device_strips(device))
    if status is None:
        message = "Wemos /status erişilemiyor"
    elif needs_segments and not supports_segments:
        compatible = False
        message = "Firmware şerit modlarını desteklemiyor; 1.7.0 veya üstünü yükleyin"
    elif compatible:
        message = "Akış LED sayısı eşleşiyor" if role == "strips" else "LED toplamı eşleşiyor"
    else:
        message = f"Uygulama: {expected_led_count}, firmware: {firmware_led_count} LED"
    return {
        "id": device.get("id"),
        "role": role,
        "reachable": status is not None,
        "compatible": compatible,
        "expected_led_count": expected_led_count,
        "firmware_led_count": firmware_led_count,
        "firmware_total_led_count": status.get("led_count") if status else None,
        "firmware_version": status.get("firmware_version") if status else None,
        "segments_supported": supports_segments,
        "firmware_strips": (status.get("strips") if status else None) or [],
        "message": message,
    }


def push_device_segments(device, save=True, timeout=3):
    """Şerit mod/renk ayarlarını Wemos'a HTTP ile gönder (POST /segments).

    Ambilight rolündeki cihazlarda tüm şeritler 'stream' moduna alınır; böylece
    daha önce şerit bazlı kullanılmış bir Wemos tekrar tam zincir olarak çalışır.
    Döner: (başarılı, mesaj, firmware_yanıtı|None)
    """
    fields = {"save": "1" if save else "0"}
    if device_role(device) == "strips":
        for strip in device_strips(device):
            prefix = f"s{int(strip['index'])}_"
            fields[prefix + "mode"] = strip.get("mode", "stream")
            fields[prefix + "color"] = str(strip.get("color", "#ff7800")).lstrip("#")
            fields[prefix + "brightness"] = str(int(strip.get("brightness", 128)))
            fields[prefix + "speed"] = str(int(strip.get("speed", 50)))
    else:
        for index in range(1, FIRMWARE_STRIP_COUNT + 1):
            fields[f"s{index}_mode"] = "stream"
    try:
        data = urllib.parse.urlencode(fields).encode("utf-8")
        request = urllib.request.Request(f"http://{device.get('ip', '')}/segments", data=data, method="POST")
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="ignore")
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = None
        return True, "Şerit ayarları Wemos'a gönderildi", parsed
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="ignore") if hasattr(error, "read") else str(error)
        return False, f"Wemos ayarı reddetti: {detail.strip() or error}", None
    except Exception as error:
        return False, f"Wemos'a gönderilemedi: {error}", None


def sync_wemos_device(config, device_id, push=True):
    """Firmware şerit bilgisini al, PC kaydıyla birleştir ve ayarları Wemos'a yaz.

    Diske yazmaz; (normalized_config, device, result) döndürür.
    """
    normalized = normalize_config_for_multi_device(config)
    for index, existing in enumerate(normalized["wemos_devices"]):
        if existing.get("id") != device_id:
            continue
        status = get_wemos_status(existing.get("ip", ""), timeout=3)
        if status is None:
            raise ValueError("Wemos /status erişilemiyor; IP ve ağ bağlantısını kontrol edin")
        device = copy.deepcopy(existing)
        if device_role(device) == "strips":
            device = merge_firmware_strips(device, status)
        push_ok, push_message, firmware_reply = (True, "Ayar gönderilmedi", None)
        if push:
            if firmware_supports_segments(status):
                push_ok, push_message, firmware_reply = push_device_segments(device)
                if push_ok and isinstance(firmware_reply, dict):
                    # Doğrulamayı gönderim sonrası güncel firmware verisiyle yap.
                    status = dict(status, **{key: firmware_reply[key] for key in ("strips", "stream_led_count", "led_count") if key in firmware_reply})
            elif device_role(device) == "strips":
                push_ok, push_message = False, "Firmware şerit modlarını desteklemiyor; 1.7.0 veya üstünü yükleyin"
        normalized["wemos_devices"][index] = device
        validation = get_device_validation(device, status=status)
        invalidate_device_validation(device_id)
        return normalized, device, {
            "pushed": push_ok,
            "message": push_message,
            "validation": validation,
        }
    raise ValueError("Cihaz bulunamadı")


# Worker tarafı doğrulama önbelleği: kare döngüsü her 3 saniyede HTTP beklemesin.
_validation_cache = {}
_validation_cache_lock = threading.Lock()
VALIDATION_RETRY_SECONDS = 15.0


def invalidate_device_validation(device_id=None):
    with _validation_cache_lock:
        if device_id is None:
            _validation_cache.clear()
        else:
            _validation_cache.pop(device_id, None)


def cached_device_validation(device, now=None):
    """Aynı ayarla doğrulanmış cihazı tekrar sorgulama; başarısızları aralıklı dene."""
    now = time.time() if now is None else now
    signature = json.dumps({
        "ip": device.get("ip"), "port": device.get("port"), "role": device_role(device),
        "expected": expected_stream_led_count(device),
        "strips": [(s.get("index"), s.get("mode"), s.get("led_count")) for s in device_strips(device)],
    }, sort_keys=True, default=str)
    device_id = device.get("id")
    with _validation_cache_lock:
        entry = _validation_cache.get(device_id)
    if entry and entry["signature"] == signature:
        if entry["result"]["compatible"] or now - entry["checked_at"] < VALIDATION_RETRY_SECONDS:
            return entry["result"]
    result = get_device_validation(device)
    with _validation_cache_lock:
        _validation_cache[device_id] = {"signature": signature, "result": result, "checked_at": now}
    return result

def probe_wemos(ip, port=7777, full=False, observations=None):
    """None means HTTP was not tested, not that it failed."""
    udp = probe_result('UDP PING/PONG', ip, int(port), 1.0)
    http = probe_result('HTTP /status', ip, 80, 1.5, 'UDP yanıt verdi')
    started = time.perf_counter()
    udp_ok = ping_wemos(ip, port=port, timeout=1.0, observation=udp)
    udp.update(tested=True, ok=udp_ok, elapsed_ms=round((time.perf_counter() - started) * 1000, 1))
    http_ok = None
    if full or not udp_ok:
        started = time.perf_counter()
        http_ok = check_wemos_http(ip, timeout=1.5, observation=http)
        http.update(tested=True, ok=http_ok, skip_reason=None, elapsed_ms=round((time.perf_counter() - started) * 1000, 1))
    if observations is not None:
        observations.extend((udp, http))
    return udp_ok, http_ok


def test_wemos_connection(ip, port=7777):
    """
    Wemos'a bağlantı durumu test eder:
    - UDP PING/PONG kontrolü (port 7777)
    - HTTP /status endpoint kontrolü (port 80)
    Returns tuple (udp_ok, http_ok, message)
    """
    observations = []
    udp_ok, http_ok = probe_wemos(ip, port, True, observations)
    log.info('[BAĞLANTI] Elle yoklama %s | %s', ip, ' | '.join(map(describe_probe, observations)), extra={
        'code': 'connectivity_manual_probe', 'source': 'pc',
        'details': {'ip': ip, 'port': int(port), 'probes': observations, 'next_try_at': None},
    })

    if udp_ok and http_ok:
        return True, True, f"Her iki protokol de çalışıyor ({ip}:{port}, http://{ip}/status)"
    elif udp_ok:
        return True, False, f"UDP PING/PONG çalışıyor ({ip}:{port}); HTTP /status erişilemiyor"
    elif http_ok:
        return False, True, f"HTTP /status çalışıyor ({ip}); UDP PING/PONG yanıt vermiyor"
    else:
        return False, False, f"Wemos'a bağlanılamıyor ({ip}:{port})"

def wemos_connectivity_checker(ip, port, cancel_event=None):
    """Probe the current target; canceled/obsolete results cannot publish."""
    cancel = cancel_event or threading.Event()
    interval = 3
    while running and not cancel.is_set():
        token = connectivity.begin(ip, port)
        if token is None:
            return
        previous = connectivity.snapshot()['state']
        observations = []
        try:
            udp_ok, http_ok = probe_wemos(ip, port, False, observations)
        except Exception as error:
            log.warning(f'[BAĞLANTI] Kontrol hatası ({ip}:{port}): {error}')
            udp_ok, http_ok = False, False
        if not running or cancel.is_set():
            return
        snapshot = connectivity.complete(token, udp_ok, http_ok, with_snapshot=True)
        next_try = time.monotonic() + interval
        if snapshot is not None:
            current = snapshot['state']
            if observations:
                observations[0]['consecutive_failures'] = snapshot['udp_consecutive_failures']
                observations[1]['consecutive_failures'] = snapshot['http_consecutive_failures']
            failures = snapshot['consecutive_failures']
            log.info('[BAĞLANTI] %s | %s | %s -> %s, ardışık hata: %s, sonraki yoklama: %s sn',
                     ip, ' | '.join(map(describe_probe, observations)), previous, current, failures, interval, extra={
                         'code': 'connectivity_probe', 'source': 'pc',
                         'details': {'ip': ip, 'port': int(port), 'probes': observations, 'previous_state': previous,
                                     'state': current, 'consecutive_failures': failures,
                                     'backoff_ms': 0, 'next_try_in_ms': interval * 1000,
                                     'next_try_at': next_probe_at(interval)},
                     })
        if cancel.wait(max(0, next_try - time.monotonic())):
            return


def frame_error_details(error, timing):
    details = {
        'error_timestamp': datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
        'error_type': type(error).__name__,
        'stage': timing.failed_stage,
        'timings_ms': {stage: round(ns / 1_000_000, 3) for stage, ns in timing.durations_ns().items()},
    }
    if timing.failed_stage_ns is not None:
        details['stage_ms'] = round(timing.failed_stage_ns / 1_000_000, 3)
    if timing.target is not None:
        details['device_id'], details['ip'], details['port'] = timing.target
    if timing.failed_monitor is not None:
        details['monitor'] = {key: timing.failed_monitor.get(key) for key in ('left', 'top', 'width', 'height')}
    winerror = getattr(error, 'winerror', None)
    mss_details = getattr(error, 'details', None)
    if isinstance(mss_details, dict):
        if winerror is None:
            winerror = mss_details.get('error_code')
        if isinstance(mss_details.get('func'), str):
            details['windows_function'] = mss_details['func'][:80]
    if winerror is None:
        winerror = getattr(error.__cause__, 'winerror', None)
    if type(winerror) is int:
        details['winerror'] = winerror
    errno = getattr(error, 'errno', None)
    if type(errno) is int:
        details['errno'] = errno
    return details


def frame_error_context(details):
    stage_name = {
        'capture': 'ekran yakalama',
        'color': 'LED rengi hesaplama',
        'udp_send': 'UDP gönderimi',
        'frame': 'kare işleme',
    }[details['stage']]
    fields = [f'aşama: {stage_name}']
    if 'stage_ms' in details:
        fields.append(f"süre: {details['stage_ms']:.3f} ms")
    if details.get('ip'):
        fields.append(f"hedef: {details['ip']}:{details['port']}")
    if 'winerror' in details:
        fields.append(f"WinError: {details['winerror']}")
    return ', '.join(fields)


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
    frame_errors_since_log = 0
    suppressed_frame_error = None
    reported_frame_errors = set()
    next_frame_error_log = 0
    low_fps_since_log = 0
    next_low_fps_log = 0
    timing_totals = {}
    timing_peaks = {}
    timing_frame_count = 0
    next_timing_log = 0
    stream_diagnostics = StreamDiagnostics(log)

    def send_stream_frame(timing, payload):
        target = timing.target
        stream_diagnostics.attempt(target)
        try:
            byte_count = timing.send(sock, payload, target[1:])
        except Exception as error:
            if isinstance(error, OSError):
                finished = time.time()
                elapsed_ns = timing.failed_stage_ns or 0
                wfp_diagnostics.submit('UDP LED', target[1], target[2], UDP, finished - elapsed_ns / 1e9,
                                       finished, directions=('outbound',), local=socket_local_address(sock))
            stream_diagnostics.failed(target, error)
            raise
        stream_diagnostics.accepted(target, byte_count)

    def log_frame_error_summary():
        log.warning("[WORKER] Ek kare işleme hataları: %s (son hata: %s)",
                    frame_errors_since_log, frame_error_context(suppressed_frame_error), extra={
            "code": "frame_failed",
            "explanation": "Son uyarıdan sonra ekran karesi yakalama, işleme veya gönderme hataları tekrarlandı.",
            "possible_causes": ["Nedeni belirlenemedi"],
            "details": suppressed_frame_error,
            "count": frame_errors_since_log,
        })

    def log_low_fps_summary():
        log.warning("[WORKER] Ek düşük FPS ölçümleri: %s", low_fps_since_log, extra={
            "code": "low_fps",
            "explanation": "Son uyarıdan sonraki bazı ölçümlerde ekran işleme hızı hedefe göre düşük kaldı.",
            "possible_causes": ["Nedeni belirlenemedi"],
            "count": low_fps_since_log,
        })
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
        legacy_target = ('legacy-primary', WEMOS_IP, WEMOS_PORT)
        stream_diagnostics.configure([legacy_target] if WEMOS_IP else [], FPS)
        stream_diagnostics.thread.start()

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
                        log.info('[CONFIG] LED, kenar ve bekleme modu ayarları güncellendi.')
                    
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
                            validation = cached_device_validation(multi_device)
                            if validation["compatible"]:
                                # Akış LED'i olmayan şerit cihazları tamamen cihaz üzerinde çalışır.
                                if expected_stream_led_count(multi_device) > 0:
                                    active_multi_devices.append(multi_device)
                            else:
                                log.warning(f"[MULTI] {multi_device['id']} devre dışı: {validation['message']}")
                    update_status("multi_device_enabled", multi_device_mode)
                    update_status("active_multi_devices", [device["id"] for device in active_multi_devices])
                    legacy_target = ('legacy-primary', WEMOS_IP, WEMOS_PORT)
                    if multi_device_mode:
                        targets = [(device['id'], device['ip'], device['port']) for device in active_multi_devices]
                    else:
                        targets = [legacy_target] if WEMOS_IP else []
                    stream_diagnostics.configure(targets, FPS)

            # IP ayarlanmamışsa bekle
            if not WEMOS_IP and not multi_device_mode:
                time.sleep(1)
                continue

            # v1.6.1: Adaptive sleep - frame işlem süresini hesaba kat
            frame_start = time.perf_counter()
            frame_capture.begin_frame()
            timing = FrameTimings(frame_capture)
            if not multi_device_mode:
                timing.target = legacy_target

            try:
                data = bytearray()

                monitor = sct.monitors[led_idx] if 0 < led_idx < len(sct.monitors) else None
                is_full = is_fullscreen(monitor) if IDLE_MODE or FULLSCREEN_MAX_BRIGHTNESS < 255 else False

                if IDLE_MODE and not is_full:
                    with timing.color():
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
                    with timing.color():
                        data = grab_edge_frame(TOP_LEDS, BOTTOM_LEDS, LEFT_LEDS, RIGHT_LEDS,
                                               EDGE_WIDTH, EDGE_OFFSET, frame_capture, led_idx, brightness)

                if multi_device_mode:
                    multi_brightness = FULLSCREEN_MAX_BRIGHTNESS if is_full else 255
                    for multi_device in active_multi_devices:
                        timing.target = multi_device['id'], multi_device['ip'], multi_device['port']
                        with timing.color():
                            if IDLE_MODE and not is_full:
                                multi_data = solid_frame(r, g, b, expected_stream_led_count(multi_device))
                            else:
                                multi_data = build_device_frame(multi_device, EDGE_WIDTH, EDGE_OFFSET,
                                                                frame_capture, brightness=multi_brightness)
                        if not multi_data:
                            continue
                        send_stream_frame(timing, multi_data)
                        device_overview.record_frame(multi_device['id'], multi_device['ip'],
                                                     multi_device['port'], multi_data,
                                                     'solid' if IDLE_MODE and not is_full else 'stream',
                                                     strips=stream_strips(multi_device))
                        packets_sent += 1
                else:
                    send_stream_frame(timing, data)
                    device_overview.record_frame('legacy-primary', WEMOS_IP, WEMOS_PORT, data,
                                                 'solid' if IDLE_MODE and not is_full else 'stream')
                    packets_sent += 1
                fps_counter += 1

                # Her 2 saniyede bir FPS ve paket sayısını güncelle
                current_time = time.perf_counter()
                if timing.udp_send_ns:
                    timing_frame_count += 1
                    for stage, duration in timing.durations_ns().items():
                        timing_totals[stage] = timing_totals.get(stage, 0) + duration
                        timing_peaks[stage] = max(timing_peaks.get(stage, 0), duration)
                    if current_time >= next_timing_log:
                        average_ms = {stage: round(ns / timing_frame_count / 1_000_000, 3)
                                      for stage, ns in timing_totals.items()}
                        log.info('[WORKER] Ortalama kare süreleri (%s kare): ekran yakalama %.3f ms, '
                                 'LED rengi hesaplama %.3f ms, UDP gönderimi %.3f ms',
                                 timing_frame_count, average_ms['capture'], average_ms['color'],
                                 average_ms['udp_send'], extra={
                            'code': 'frame_timings',
                            'explanation': 'Başarılı karelerde ekran yakalama, LED rengi hesaplama ve UDP gönderme süreleri.',
                            'details': {
                                'frame_count': timing_frame_count,
                                'average_ms': average_ms,
                                'max_ms': {stage: round(ns / 1_000_000, 3) for stage, ns in timing_peaks.items()},
                            },
                        })
                        timing_frame_count = 0
                        timing_totals.clear()
                        timing_peaks.clear()
                        next_timing_log = current_time + 30
                if frame_errors_since_log and current_time >= next_frame_error_log:
                    log_frame_error_summary()
                    frame_errors_since_log = 0
                if current_time - fps_timer >= 2.0:
                    actual_fps = fps_counter / (current_time - fps_timer)
                    update_status("actual_fps", round(actual_fps, 1))
                    update_status("packets_sent", packets_sent)
                    fps_counter = 0
                    fps_timer = current_time
                    low_fps_threshold = min(25, FPS * 0.8)
                    if actual_fps < low_fps_threshold:
                        low_fps_since_log += 1
                        if current_time >= next_low_fps_log:
                            log.warning("[WORKER] Düşük FPS: %.1f FPS (hedef: %s)", actual_fps, FPS, extra={
                                "code": "low_fps",
                                "explanation": f"PC'nin ölçtüğü ekran işleme hızı {low_fps_threshold:g} FPS eşiğinin altında.",
                                "possible_causes": ["Nedeni belirlenemedi"],
                                "details": {"fps": round(actual_fps, 1), "target_fps": FPS,
                                            "threshold_fps": low_fps_threshold},
                                "count": low_fps_since_log,
                            })
                            low_fps_since_log = 0
                            next_low_fps_log = current_time + 10
                    elif low_fps_since_log:
                        log_low_fps_summary()
                        low_fps_since_log = 0

                # v1.6.2: Adaptive sleep - timeBeginPeriod(1) sayesinde 1ms hassasiyetle uyuyabiliriz
                time.sleep(frame_delay(frame_start, time.perf_counter(), sleep_time))

            except Exception as e:
                details = frame_error_details(e, timing)
                errors += 1
                update_status("errors", errors)
                update_status("last_error", str(e))
                now = time.perf_counter()
                if now >= next_frame_error_log:
                    if frame_errors_since_log:
                        log_frame_error_summary()
                        frame_errors_since_log = 0
                    reported_frame_errors.clear()
                    next_frame_error_log = now + 10
                signature = (details['stage'], timing.target, details['error_type'], details.get('winerror'))
                if signature not in reported_frame_errors and len(reported_frame_errors) < 8:
                    reported_frame_errors.add(signature)
                    log.error("[WORKER] Frame hatası (%s): %s",
                              frame_error_context(details), e, extra={
                        "code": "frame_failed",
                        "explanation": "Ekran karesi yakalanırken, işlenirken veya gönderilirken hata oluştu.",
                        "possible_causes": ["Nedeni belirlenemedi"],
                        "details": details,
                    })
                else:
                    frame_errors_since_log += 1
                    suppressed_frame_error = details
                if running:
                    time.sleep(1)  # Hata durumunda biraz bekle
                else:
                    break
    except Exception as e:
        log.error("[WORKER] Akış işçisi durdu: %s", e, extra={
            "code": "stream_worker_failed",
            "explanation": "PC'nin LED akışını işleyen arka plan işçisi hata nedeniyle durdu.",
            "possible_causes": ["Nedeni belirlenemedi"],
            "details": {"error_type": type(e).__name__},
        })
        update_status("connection", "bağlantı hatası")
        update_status("last_error", str(e))
    finally:
        stream_diagnostics.stop()
        if frame_errors_since_log:
            log_frame_error_summary()
        if low_fps_since_log:
            log_low_fps_summary()
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

def diagnostic_devices():
    try:
        config = load_config()
    except OSError:
        return []
    normalized = normalize_config_for_multi_device(config)
    primary = legacy_config_to_primary_device(normalized)
    devices = list(normalized['wemos_devices'])
    if primary and not normalized['multi_device_enabled']:
        devices.insert(0, primary)
    seen = set()
    result = []
    for device in devices:
        if not isinstance(device, dict) or not device.get('enabled'):
            continue
        ip, device_id = device.get('ip'), device.get('id')
        if not isinstance(ip, str) or not ip or not isinstance(device_id, str) or not device_id:
            continue
        device_id = device_id[:128]
        if ip in seen or device_id in seen:
            continue
        seen.update((ip, device_id))
        result.append({'id': device_id, 'name': str(device.get('name') or device_id)[:128], 'ip': ip})
    return result


def main():
    """Ana program"""
    global running, ambilight_thread, icon
    monitor_catalog.refresh(get_available_monitors)
    
    print("\n" + "="*60)
    print(f"  AMBILIGHT PC v{APP_VERSION} - Windows (port {BACKEND_PORT})")
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
    log_history.start_stream_writer()
    wfp_diagnostics.start()
    ambilight_thread = threading.Thread(target=ambilight_worker, args=(config,), name=STREAM_THREAD, daemon=False)
    ambilight_thread.start()
    
    diagnostic_stop = threading.Event()
    diagnostics = WemosDiagnostics(log, log_history.snapshot()['logs'], diagnostic_devices,
                                   wfp_submit=wfp_diagnostics.submit)
    diagnostic_thread = threading.Thread(target=diagnostics.run, args=(diagnostic_stop,),
                                         name='wemos-diagnostics', daemon=True)
    diagnostic_thread.start()
    log.info('LuxEdge PC arka ucu başlatıldı', extra={'code': 'pc_started'})
    try:
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
    finally:
        running = False
        diagnostic_stop.set()
        windows_audit_session.close()
        diagnostic_thread.join(timeout=2)
        ambilight_thread.join()
        wfp_diagnostics.stop()
        log_history.stop_stream_writer()


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

