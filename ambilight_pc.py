import socket
import numpy as np
from mss import mss
from PIL import Image
import time
import json
import os
import sys
import io

# Windows konsolunda emoji/Unicode desteği için encoding'i UTF-8'e ayarla
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
if sys.stderr.encoding != 'utf-8':
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
if sys.platform == 'win32':
    import winreg
import threading
import urllib.request
import urllib.parse
import subprocess
import re
from pathlib import Path
from http.server import HTTPServer, SimpleHTTPRequestHandler
import http.server
import struct

# Windows'ta subprocess çağrılarında CMD penceresi açılmasını engelle
CREATE_NO_WINDOW = 0x08000000 if sys.platform == 'win32' else 0

def _subprocess_kwargs():
    """Platform'a uygun subprocess parametreleri döndürür"""
    kwargs = {
        'capture_output': True,
        'text': True,
        'errors': 'ignore',
        'stdin': subprocess.DEVNULL
    }
    if sys.platform == 'win32':
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

import shutil

def get_config_path():
    """EXE çalışırken config dosyasının yolunu döndürür (Önceki ayarları kaybetmeden taşıyarak)"""
    # 1. Eski sorunlu (Permission Denied alan) system dizini lokasyonu
    if getattr(sys, 'frozen', False):
        old_path = os.path.join(os.path.dirname(sys.executable), "ambilight_config.json")
    else:
        old_path = CONFIG_FILE
        
    # 2. Yeni Kullanıcıya özel sorunsuz dizin
    if sys.platform == 'win32':
        config_dir = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'LuxEdge')
    else:
        config_dir = os.path.join(os.environ.get('XDG_CONFIG_HOME', os.path.join(os.path.expanduser('~'), '.config')), 'LuxEdge')
    
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
    """Konfigürasyon dosyasını yükler"""
    config_path = get_config_path()
    if os.path.exists(config_path):
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"[HATA] Konfigürasyon dosyası okunamadı: {e}")
            return None
    return None

def save_config(config):
    """Konfigürasyon dosyasını kaydeder"""
    config_path = get_config_path()
    try:
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"[HATA] Konfigürasyon dosyası kaydedilemedi: {e}")
        return False

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

    if sys.platform == 'win32':
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
    else:
        try:
            # Yöntem 2 (Linux): ip addr çıktısını analiz et
            result = subprocess.run(
                ['ip', '-4', 'addr', 'show'],
                capture_output=True, text=True, timeout=10,
                errors='ignore', stdin=subprocess.DEVNULL
            )
            for line in result.stdout.split('\n'):
                line = line.strip()
                if line.startswith('inet ') and '127.0.0.1' not in line:
                    ip_match = re.search(r'inet\s+(\d+\.\d+\.\d+\.\d+)', line)
                    if ip_match:
                        return ip_match.group(1)
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
    except:
        pass

    if sys.platform == 'win32':
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
        except:
            pass
    else:
        try:
            # ip addr ile daha detaylı (Linux)
            result = subprocess.run(['ip', '-4', 'addr', 'show'], capture_output=True, text=True, errors='ignore', stdin=subprocess.DEVNULL)
            for line in result.stdout.split('\n'):
                match = re.search(r'inet\s+(\d+\.\d+\.\d+\.\d+)', line)
                if match:
                    ip = match.group(1)
                    if not ip.startswith('127.'):
                        ips.add(ip)
        except:
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
    if sys.platform == 'win32':
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
    else:
        try:
            # Linux: ip addr çıktısından CIDR prefix'ini mask'a çevir
            local_ip = get_local_ip()
            if local_ip:
                result = subprocess.run(
                    ['ip', '-4', 'addr', 'show'],
                    capture_output=True, text=True, timeout=10,
                    errors='ignore', stdin=subprocess.DEVNULL
                )
                for line in result.stdout.split('\n'):
                    if local_ip in line:
                        cidr_match = re.search(r'/(\d+)', line)
                        if cidr_match:
                            prefix = int(cidr_match.group(1))
                            mask = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF
                            return f"{(mask >> 24) & 0xFF}.{(mask >> 16) & 0xFF}.{(mask >> 8) & 0xFF}.{mask & 0xFF}"
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
    if sys.platform == 'win32':
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
    else:
        # Linux: nmcli ile Wi-Fi tarama
        try:
            result = subprocess.run(
                ['nmcli', '-t', '-f', 'SSID', 'device', 'wifi', 'list', '--rescan', 'yes'],
                capture_output=True, text=True, timeout=15,
                errors='ignore', stdin=subprocess.DEVNULL
            )
            networks = []
            for line in result.stdout.split('\n'):
                ssid = line.strip()
                if ssid and ssid not in networks:
                    networks.append(ssid)
            return networks
        except Exception:
            return []

def find_wemos_hotspot():
    """Wemos hotspot'unu otomatik bulur"""
    wemos_keywords = ['wemos', 'ambilight', 'setup', 'config', 'esp']
    if sys.platform == 'win32':
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
                    if keyword in line_lower and 'ssid' in line_lower:
                        ssid_match = re.search(r'SSID\s*\d+\s*:\s*(.+)', line, re.IGNORECASE)
                        if ssid_match:
                            return ssid_match.group(1).strip()
            return None
        except Exception:
            return None
    else:
        # Linux: nmcli ile Wemos hotspot ara
        try:
            result = subprocess.run(
                ['nmcli', '-t', '-f', 'SSID', 'device', 'wifi', 'list'],
                capture_output=True, text=True, timeout=15,
                errors='ignore', stdin=subprocess.DEVNULL
            )
            for line in result.stdout.split('\n'):
                ssid = line.strip()
                for keyword in wemos_keywords:
                    if keyword in ssid.lower():
                        return ssid
            return None
        except Exception:
            return None

def connect_to_wifi(ssid, password=None):
    """Belirli bir Wi-Fi ağına bağlanır"""
    if sys.platform == 'win32':
        try:
            if password:
                result = subprocess.run(
                    ['netsh', 'wlan', 'connect', f'name={ssid}', f'key={password}'],
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
    else:
        # Linux: nmcli ile Wi-Fi bağlantısı
        try:
            if password:
                result = subprocess.run(
                    ['nmcli', 'device', 'wifi', 'connect', ssid, 'password', password],
                    capture_output=True, text=True, timeout=30,
                    errors='ignore', stdin=subprocess.DEVNULL
                )
            else:
                result = subprocess.run(
                    ['nmcli', 'device', 'wifi', 'connect', ssid],
                    capture_output=True, text=True, timeout=30,
                    errors='ignore', stdin=subprocess.DEVNULL
                )
            return result.returncode == 0
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
    except:
        return (255, 0, 0)

def get_system_accent_color():
    """Sistem vurgu rengini döndürür (Windows/Linux)"""
    if sys.platform == 'win32':
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
            pass
    else:
        # Linux: GNOME/KDE accent color okumayı dene
        try:
            result = subprocess.run(
                ['gsettings', 'get', 'org.gnome.desktop.interface', 'accent-color'],
                capture_output=True, text=True, timeout=5,
                errors='ignore', stdin=subprocess.DEVNULL
            )
            color_name = result.stdout.strip().strip("'")
            # GNOME accent color isimleri
            gnome_colors = {
                'blue': '#3584e4', 'teal': '#2190a4', 'green': '#3a944a',
                'yellow': '#c88800', 'orange': '#ed5b00', 'red': '#e62d42',
                'pink': '#d56199', 'purple': '#9141ac', 'slate': '#6f8396'
            }
            if color_name in gnome_colors:
                return gnome_colors[color_name]
        except Exception:
            pass
    return "#ff0000"  # fallback kırmızı

# Eski isimle uyumluluk
get_windows_accent_color = get_system_accent_color

# Linux fullscreen cache (60fps'de her frame'de subprocess çağırmamak için)
_fullscreen_cache = {'value': False, 'time': 0}
_FULLSCREEN_CACHE_TTL = 1.0  # 1 saniyede bir kontrol et

# Linux X11 display (tek sefer aç, tekrar kullan)
_x11_display = None
_x11_lib = None

def _get_x11_display():
    """X11 display bağlantısını aç ve cache'le"""
    global _x11_display, _x11_lib
    if _x11_lib is None:
        try:
            import ctypes.util
            x11_path = ctypes.util.find_library('X11')
            if not x11_path:
                return None, None
            _x11_lib = ctypes.CDLL(x11_path)
            
            # Fonksiyon imzaları (segfault önleme)
            _x11_lib.XOpenDisplay.restype = ctypes.c_void_p
            _x11_lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
            _x11_lib.XDefaultRootWindow.restype = ctypes.c_ulong
            _x11_lib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
            _x11_lib.XGetGeometry.restype = ctypes.c_int
            _x11_lib.XGetGeometry.argtypes = [
                ctypes.c_void_p, ctypes.c_ulong,
                ctypes.POINTER(ctypes.c_ulong),
                ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint),
                ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)
            ]
            _x11_lib.XTranslateCoordinates.restype = ctypes.c_int
            _x11_lib.XTranslateCoordinates.argtypes = [
                ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
                ctypes.c_int, ctypes.c_int,
                ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_ulong)
            ]
        except Exception:
            _x11_lib = False  # Başarısız — tekrar deneme
            return None, None
    
    if _x11_lib is False:
        return None, None
    
    if _x11_display is None:
        _x11_display = _x11_lib.XOpenDisplay(None)
    
    return _x11_lib, _x11_display

def _get_window_root_position(window_id_hex):
    """Pencerenin root (mutlak) X,Y koordinatlarını döndürür (ctypes X11)"""
    import ctypes
    xlib, display = _get_x11_display()
    if not xlib or not display:
        return None, None
    
    try:
        wid = int(window_id_hex, 16)
        root = xlib.XDefaultRootWindow(display)
        
        root_x = ctypes.c_int()
        root_y = ctypes.c_int()
        child_return = ctypes.c_ulong()
        
        xlib.XTranslateCoordinates(
            display, wid, root,
            0, 0,
            ctypes.byref(root_x), ctypes.byref(root_y),
            ctypes.byref(child_return)
        )
        return root_x.value, root_y.value
    except Exception:
        return None, None

def _is_window_on_led_monitor(window_id_hex):
    """Pencerenin LED monitöründe (mss monitors[1] = primary) olup olmadığını kontrol eder"""
    try:
        from mss import mss
        with mss() as sct:
            led_monitor = sct.monitors[1]  # Primary monitor = LED monitörü
        
        root_x, root_y = _get_window_root_position(window_id_hex)
        if root_x is None:
            return True  # Pozisyon alınamadıysa, varsayılan: True (eski davranış)
        
        # Pencere LED monitörünün x aralığında mı kontrol et
        mon_left = led_monitor['left']
        mon_right = mon_left + led_monitor['width']
        
        return mon_left <= root_x < mon_right
    except Exception:
        return True  # Hata durumunda varsayılan: True

def is_fullscreen():
    if sys.platform == 'win32':
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            if not hwnd: return False
            
            # Avoid desktop matching
            if hwnd == user32.GetDesktopWindow() or hwnd == user32.GetShellWindow():
                return False
                
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            
            screen_w = user32.GetSystemMetrics(0)
            screen_h = user32.GetSystemMetrics(1)
            
            w = rect.right - rect.left
            h = rect.bottom - rect.top
            
            # Gerçek tam ekran (YouTube, Oyunlar) monitör çözünürlüğüne eşittir.
            return (w == screen_w and h == screen_h and rect.top == 0 and rect.left == 0)
        except Exception:
            return False
    else:
        # Linux: xprop ile tam ekran kontrolü (X11) + çoklu monitör desteği
        # Performans: Cache kullan, her frame'de subprocess çağırma
        now = time.time()
        if now - _fullscreen_cache['time'] < _FULLSCREEN_CACHE_TTL:
            return _fullscreen_cache['value']
        
        result_val = False
        try:
            # xprop -root ile aktif pencereyi bul (xdotool bağımlılığı yok)
            result = subprocess.run(
                ['xprop', '-root', '_NET_ACTIVE_WINDOW'],
                capture_output=True, text=True, timeout=1,
                errors='ignore', stdin=subprocess.DEVNULL
            )
            match = re.search(r'window id # (0x[0-9a-fA-F]+)', result.stdout)
            if match:
                window_id = match.group(1)
                
                # Pencere durumunu kontrol et
                result2 = subprocess.run(
                    ['xprop', '-id', window_id, '_NET_WM_STATE'],
                    capture_output=True, text=True, timeout=1,
                    errors='ignore', stdin=subprocess.DEVNULL
                )
                is_fs = '_NET_WM_STATE_FULLSCREEN' in result2.stdout
                
                if is_fs:
                    # Çoklu monitör: fullscreen pencere LED monitöründe mi?
                    result_val = _is_window_on_led_monitor(window_id)
                else:
                    result_val = False
        except Exception:
            pass
        
        _fullscreen_cache['value'] = result_val
        _fullscreen_cache['time'] = now
        return result_val

def average_color(img):
    """Bölgesel ortalama rengi hesaplar"""
    arr = np.array(img).reshape(-1, 3)
    
    # Tüm piksellerin doğrudan ortalamasını al
    return tuple(np.mean(arr, axis=0).astype(int))

def grab_edge_colors(top_leds, bottom_leds, left_leds, right_leds, edge_width, edge_offset, sct):
    """Ekran kenarlarından renkleri toplar"""
    monitor = sct.monitors[1]
    screenshot = sct.grab(monitor)
    img = Image.frombytes("RGB", screenshot.size, screenshot.rgb)

    w, h = img.size
    final_colors = []

    # 1) RIGHT side (bottom → top)
    for i in range(right_leds):
        y1 = int((right_leds - 1 - i) * h / right_leds)
        y2 = int((right_leds - i) * h / right_leds)
        final_colors.append(average_color(img.crop((w - edge_width - edge_offset, y1, w - edge_offset, y2))))

    # 2) TOP side (right → left)
    for i in range(top_leds):
        x1 = int((top_leds - 1 - i) * w / top_leds)
        x2 = int((top_leds - i) * w / top_leds)
        final_colors.append(average_color(img.crop((x1, edge_offset, x2, edge_width + edge_offset))))

    # 3) LEFT side (top → bottom)
    for i in range(left_leds):
        y1 = int(i * h / left_leds)
        y2 = int((i + 1) * h / left_leds)
        final_colors.append(average_color(img.crop((edge_offset, y1, edge_width + edge_offset, y2))))

    # 4) BOTTOM side (left → right)
    for i in range(bottom_leds):
        x1 = int(i * w / bottom_leds)
        x2 = int((i + 1) * w / bottom_leds)
        final_colors.append(average_color(img.crop((x1, h - edge_width - edge_offset, x2, h - edge_offset))))

    return final_colors

# ============================================================
# GLOBAL DEĞİŞKENLER VE DURUM
# ============================================================

running = True
ambilight_thread = None
sock = None
sct = None
icon = None

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

def update_status(key, value):
    """Thread-safe durum güncelleme"""
    with status_lock:
        app_status[key] = value

def get_status():
    """Thread-safe durum okuma"""
    with status_lock:
        return dict(app_status)

# ============================================================
# WEB ARAYÜZÜ SUNUCUSU
# ============================================================

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
            elif self.path == '/api/scan':
                self.serve_scan()
            else:
                self.send_error(404)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
    
    def do_POST(self):
        try:
            if self.path == '/api/config':
                self.handle_config_update()
            elif self.path == '/api/restart':
                self.handle_restart()
            elif self.path == '/api/wemos/restart':
                self.handle_wemos_restart()
            elif self.path == '/api/wemos/sleep':
                self.handle_wemos_sleep()
            elif self.path == '/api/wemos/reset_wifi':
                self.handle_wemos_reset_wifi()
            else:
                self.send_error(404)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
    
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
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(b"<h1>Web UI dosyasi bulunamadi!</h1>")
    
    def serve_status(self):
        """Durum bilgisini JSON olarak sun"""
        status = get_status()
        
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
                if save_config(current_config):
                    print(f"[TARAMA] Wemos bulundu ve config'e kaydedildi: {found_ip}")
                    result["message"] = f"Wemos bulundu ve kaydedildi: {found_ip}"
                else:
                    print(f"[TARAMA] Wemos bulundu ama config kaydedilemedi: {found_ip}")
                
                # Durum bilgisini hemen güncelle
                update_status("wemos_ip", found_ip)
                update_status("connection", "bağlanıyor")
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
            current_config = load_config() or {}
            current_config.update(new_config)
            
            if save_config(current_config):
                # Anında global bellek durumunu güncelle (UI senkronizasyon bug'ını çözer)
                for k, v in new_config.items():
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
                resp_text = response.read().decode('utf-8').strip()
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
            
            # Wemos sıfırlandığı için yanıt gelemeyebilir
            try:
                urllib.request.urlopen(url, timeout=1)
            except:
                pass
                
            result = {"success": True, "message": "Wemos sıfırlanıyor, hotspot moduna dönülecek..."}
        except Exception as e:
            result = {"success": False, "message": f"Wemos iletişim hatası: {str(e)}"}
            
        self._safe_send_json(result)

def restart_app():
    """Uygulamayı yeniden başlat"""
    global running
    running = False
    time.sleep(1)
    os.execv(sys.executable, [sys.executable] + sys.argv)

def start_web_server(port=8888):
    """Web UI sunucusunu arka planda başlat"""
    try:
        server = HTTPServer(('0.0.0.0', port), WebUIHandler)
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
                server = HTTPServer(('0.0.0.0', alt_port), WebUIHandler)
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
    webbrowser.open('http://localhost:8888')

# ============================================================
# KONSOL YÖNETİMİ
# ============================================================

def hide_console():
    """Console penceresini tamamen gizler"""
    if sys.platform == 'win32':
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
    # Linux'ta konsol gizleme gerekli değil (Electron zaten arka planda çalıştırır)

def show_console():
    """Console penceresini gösterir"""
    if sys.platform == 'win32':
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
    # Linux'ta konsol gösterme gerekli değil

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
        return data == b"PONG"
    except Exception:
        try:
            sock.close()
        except Exception:
            pass
        return False

def wemos_connectivity_checker(ip, port):
    """
    Arka planda Wemos'un erişilebilir olup olmadığını kontrol eder.
    Her 3 saniyede bir Wemos'a HTTP GET isteği gönderir, yanıt gelirse bağlı.
    """
    global running
    consecutive_fails = 0
    MAX_FAILS = 2  # 2 ardışık başarısızlık = bağlı değil
    was_connected = False
    
    print(f"[BAĞLANTI] Connectivity checker başlatıldı (Hedef: {ip})")
    
    # İlk birkaç saniye bekle, worker başlasın
    for _ in range(30):
        if not running:
            return
        time.sleep(0.1)
    
    print(f"[BAĞLANTI] İlk kontrol yapılıyor...")
    
    while running:
        try:
            # UDP PING/PONG ile kontrol (port 7777 - Wemos firmware destekliyor)
            wemos_reachable = ping_wemos(ip, port=7777, timeout=3)
            
            if wemos_reachable:
                consecutive_fails = 0
                if not was_connected:
                    print(f"[BAĞLANTI] ✅ Wemos'a bağlantı kuruldu ({ip})")
                was_connected = True
                update_status("connection", "bağlı")
            else:
                consecutive_fails += 1
                print(f"[BAĞLANTI] Wemos yanıt vermedi (deneme {consecutive_fails}/{MAX_FAILS})")
                if consecutive_fails >= MAX_FAILS:
                    if was_connected:
                        print(f"[BAĞLANTI] ❌ Wemos bağlantısı kesildi ({ip})")
                    was_connected = False
                    update_status("connection", "bağlı değil")
        except Exception as e:
            consecutive_fails += 1
            print(f"[BAĞLANTI] Kontrol hatası: {e}")
            if consecutive_fails >= MAX_FAILS:
                was_connected = False
                update_status("connection", "bağlı değil")
        
        # 3 saniyede bir kontrol et
        for _ in range(30):
            if not running:
                return
            time.sleep(0.1)

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
    update_status("running", True)
    update_status("uptime_start", time.time())
    update_status("connection", "bağlanıyor" if WEMOS_IP else "IP Bekleniyor...")
    
    local_ip = get_local_ip()
    if local_ip:
        update_status("local_ip", local_ip)
        update_status("subnet", get_subnet_base(local_ip) + ".x")
    
    packets_sent = 0
    errors = 0
    fps_counter = 0
    fps_timer = time.time()
    
    # Wemos bağlantı kontrol thread'ini başlat (Eğer IP varsa)
    connectivity_thread = None
    if WEMOS_IP:
        connectivity_thread = threading.Thread(
            target=wemos_connectivity_checker,
            args=(WEMOS_IP, WEMOS_PORT),
            daemon=True
        )
        connectivity_thread.start()
    
    # Periyodik config kontrol sayacı
    config_check_timer = time.time()
    CONFIG_CHECK_INTERVAL = 3  # Her 3 saniyede bir config kontrol et
    
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sct = mss() # DÜZELTİLDİ
        
        while running:
            # Periyodik config kontrolü (IP değişikliğini canlı algıla)
            current_time_check = time.time()
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
                    
                    if (new_top != TOP_LEDS or new_bottom != BOTTOM_LEDS or 
                        new_left != LEFT_LEDS or new_right != RIGHT_LEDS or 
                        new_width != EDGE_WIDTH or new_offset != EDGE_OFFSET or
                        new_idle_mode != IDLE_MODE or new_idle_color != IDLE_COLOR or new_idle_brightness != IDLE_BRIGHTNESS or
                        new_idle_use_windows_color != IDLE_USE_WINDOWS_COLOR):
                        old_total_leds = TOTAL_LEDS
                        
                        TOP_LEDS, BOTTOM_LEDS = new_top, new_bottom
                        LEFT_LEDS, RIGHT_LEDS = new_left, new_right
                        EDGE_WIDTH, EDGE_OFFSET = new_width, new_offset
                        IDLE_MODE, IDLE_COLOR, IDLE_BRIGHTNESS = new_idle_mode, new_idle_color, new_idle_brightness
                        IDLE_USE_WINDOWS_COLOR = new_idle_use_windows_color
                        TOTAL_LEDS = TOP_LEDS + BOTTOM_LEDS + LEFT_LEDS + RIGHT_LEDS
                        
                        # EĞER LED SAYISI AZALDIYSA: Cihaz üzerindeki eski, arkada kalan LED'leri söndürmek için Blackout paketi at.
                        if old_total_leds > TOTAL_LEDS and WEMOS_IP:
                            try:
                                blackout = bytearray([0] * (old_total_leds * 3))
                                for _ in range(3):
                                    sock.sendto(blackout, (WEMOS_IP, WEMOS_PORT))
                                    time.sleep(0.05)
                            except: pass
                        
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
                        print("✓ (Worker) LED / Kenar / Bekleme Modu Konfigürasyonları Canlı Olarak Güncellendi.")
                    
                    new_ip = new_config.get("wemos_ip", "")
                    if new_ip and new_ip != WEMOS_IP:
                        print(f"✓ (Worker) IP değişti: '{WEMOS_IP}' → '{new_ip}'")
                        WEMOS_IP = new_ip
                        update_status("wemos_ip", WEMOS_IP)
                        update_status("connection", "bağlanıyor")
                        
                        # Bağlantı kontrol thread'ini yeniden başlat
                        if connectivity_thread is None or not connectivity_thread.is_alive():
                            connectivity_thread = threading.Thread(
                                target=wemos_connectivity_checker,
                                args=(WEMOS_IP, WEMOS_PORT),
                                daemon=True
                            )
                            connectivity_thread.start()
            
            # IP ayarlanmamışsa bekle
            if not WEMOS_IP:
                time.sleep(1)
                continue

            try:
                data = bytearray()
                
                if IDLE_MODE and not is_fullscreen():
                    if IDLE_USE_WINDOWS_COLOR:
                        current_color = get_windows_accent_color()
                    else:
                        current_color = IDLE_COLOR
                        
                    ir, ig, ib = hex_to_rgb(current_color)
                    b_ratio = IDLE_BRIGHTNESS / 100.0
                    r = int(ir * b_ratio)
                    g = int(ig * b_ratio)
                    b = int(ib * b_ratio)
                    for _ in range(TOTAL_LEDS):
                        data += bytes([r, g, b])
                else:
                    colors = grab_edge_colors(TOP_LEDS, BOTTOM_LEDS, LEFT_LEDS, RIGHT_LEDS, EDGE_WIDTH, EDGE_OFFSET, sct)
                    for r, g, b in colors:
                        data += bytes([r, g, b])

                sock.sendto(data, (WEMOS_IP, WEMOS_PORT))
                packets_sent += 1
                fps_counter += 1
                
                # Her 2 saniyede bir FPS ve paket sayısını güncelle
                current_time = time.time()
                if current_time - fps_timer >= 2.0:
                    actual_fps = fps_counter / (current_time - fps_timer)
                    update_status("actual_fps", round(actual_fps, 1))
                    update_status("packets_sent", packets_sent)
                    # Paketler başarıyla gönderiliyorsa = bağlı
                    if fps_counter > 0:
                        update_status("connection", "bağlı")
                    fps_counter = 0
                    fps_timer = current_time
                
                time.sleep(sleep_time)
            except Exception as e:
                errors += 1
                update_status("errors", errors)
                update_status("last_error", str(e))
                if running:
                    time.sleep(1)  # Hata durumunda biraz bekle
                else:
                    break
    except Exception as e:
        update_status("connection", "bağlantı hatası")
        update_status("last_error", str(e))
    finally:
        update_status("running", False)
        update_status("connection", "kapalı")
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
    
    print("\n" + "="*60)
    print("  AMBILIGHT PC v1.5.0 - Linux & Windows")
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
            "edge_offset": 0
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
    web_server = start_web_server(8888)
    if web_server:
        print(f"🌐 Web Arayüzü: http://localhost:8888")
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
    print(f"Web Arayüzü: http://localhost:8888\n")
    
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

