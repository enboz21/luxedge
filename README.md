<p align="center">
  <h1 align="center">✨ LuxEdge — DIY Ambilight Sistemi</h1>
  <p align="center">
    <strong>Monitör arkası LED aydınlatmasını otomatik olarak yöneten, Electron + Python tabanlı masaüstü uygulaması</strong>
  </p>
  <p align="center">
    <img src="https://img.shields.io/badge/version-1.2.0-blue?style=flat-square" alt="Version">
    <img src="https://img.shields.io/badge/platform-Windows-0078D6?style=flat-square&logo=windows" alt="Platform">
    <img src="https://img.shields.io/badge/license-CC%20BY--NC%204.0-green?style=flat-square" alt="License">
    <img src="https://img.shields.io/badge/hardware-ESP8266%20(Wemos)-red?style=flat-square" alt="Hardware">
  </p>
</p>

---

## 📖 Proje Hakkında

**LuxEdge**, monitörünüzün arkasına taktığınız LED şeritleri ekrandaki renklere göre otomatik olarak yöneten bir **DIY Ambilight** sistemidir. Bilgisayarınızın ekranını gerçek zamanlı analiz eder ve renk verilerini **Wi-Fi üzerinden** Wemos (ESP8266) mikrodenetleyicisine gönderir.

### 🎬 Nasıl Çalışır?

```
┌─────────────┐    UDP (7777)    ┌──────────────┐    NeoPixel    ┌────────────┐
│  PC (Python) │ ──────────────> │ Wemos D1 Mini│ ────────────> │  LED Şerit │
│  Ekran Analiz│                 │  (ESP8266)   │               │  (WS2812B) │
└─────────────┘                 └──────────────┘               └────────────┘
       ↑
  Electron UI
  (Kontrol Paneli)
```

1. **Python backend** ekranın kenarlarındaki renkleri yakalar (`mss` + `PIL`)
2. Renk verileri **UDP paketleri** olarak Wemos'a gönderilir
3. **Wemos** gelen verilere göre **NeoPixel LED'leri** kontrol eder
4. **Electron arayüzü** sistemi yönetmenizi sağlar

---

## ✨ Özellikler

| Özellik | Açıklama |
|---|---|
| 🖥️ **Gerçek Zamanlı Ekran Yakalama** | Ekranın 4 kenarındaki renkleri ~60 FPS hızda analiz eder |
| 📡 **Otomatik Ağ Taraması** | Wemos cihazını ağda otomatik bulur (UDP Discovery) |
| 🔌 **UDP PING/PONG Bağlantı Kontrolü** | Wemos'a gerçek zamanlı bağlantı durumu takibi |
| 💡 **Esnek LED Konfigürasyonu** | Üst, alt, sol, sağ kenar LED sayılarını ayrı ayrı ayarlama |
| 🌙 **Uyku Modu** | LED'leri uzaktan kapatma/açma (cihaz bağlı kalır) |
| 🔄 **Uzaktan Yeniden Başlatma** | Wemos'u arayüzden resetleme |
| 📊 **Performans İzleme** | FPS, gönderilen paket sayısı ve hata sayısı |
| 🔧 **Manuel IP Girişi** | Otomatik tarama çalışmazsa IP'yi elle girme |
| 💾 **Config Otomatik Kayıt** | Ayarlar JSON dosyasında saklanır |
| 🖱️ **Sistem Tepsisi** | Arka planda çalışır, tepsiden erişilir |

---

## 🛠️ Donanım Gereksinimleri

| Bileşen | Detay |
|---|---|
| **Mikrodenetleyici** | Wemos D1 Mini (ESP8266) veya uyumlu ESP8266 kartı |
| **LED Şerit** | WS2812B (NeoPixel) — Adreslenebilir RGB LED şerit |
| **Güç Kaynağı** | 5V, yeterli amper (LED sayısına göre: ~60mA/LED) |
| **Bağlantı** | Wi-Fi (2.4 GHz) |

### 📐 Varsayılan LED Düzeni
```
        ───── 34 LED (Üst) ─────
       │                         │
  20   │                         │   20
  LED  │       MONİTÖR           │   LED
(Sol)  │                         │ (Sağ)
       │                         │
        ───── 0 LED (Alt) ──────
                              Toplam: 74 LED
```

---

## 💻 Yazılım Gereksinimleri

- **İşletim Sistemi:** Windows 10/11
- **Node.js:** v18 veya üstü
- **Python:** 3.10 veya üstü
- **Python Kütüphaneleri:** `numpy`, `mss`, `Pillow`, `pystray`

---

## 🚀 Kurulum

### 1. Projeyi İndirin
```bash
git clone https://github.com/KULLANICI_ADINIZ/luxedge.git
cd luxedge
```

### 2. Node.js Bağımlılıklarını Kurun
```bash
npm install
```

### 3. Python Bağımlılıklarını Kurun
```bash
pip install -r requirements.txt
```

### 4. Wemos'u Programlayın
1. Arduino IDE'yi açın
2. `wemos_code/wemos_code.ino` dosyasını yükleyin
3. ESP8266 kart desteğini ekleyin (Araçlar → Kart → ESP8266)
4. Gerekli kütüphaneleri kurun:
   - `Adafruit NeoPixel`
   - `ESP8266WiFi` (dahili)
5. Kodu Wemos'a yükleyin

### 5. Uygulamayı Çalıştırın
```bash
npm start
```

---

## 📡 İlk Bağlantı (Wemos Kurulumu)

1. Wemos'u çalıştırın — **mavi LED** yanar ve `Wemos_Setup` adında bir Wi-Fi hotspot oluşturur
2. Telefonunuzdan veya bilgisayardan `Wemos_Setup` ağına bağlanın
3. Otomatik açılan Captive Portal'dan ev Wi-Fi ağınızı seçin ve şifresini girin
4. Wemos ev ağınıza bağlanır — **yeşil LED** yanar
5. LuxEdge uygulamasında **"Tara"** butonuna basın — Wemos otomatik olarak bulunur

---

## 🎮 Kullanım

### Kontrol Paneli Butonları

| Buton | İşlev |
|---|---|
| 🔍 **Tara** | Ağda Wemos cihazını arar ve bulursa IP'sini kaydeder |
| 🔄 **Yeniden Başlat** | Wemos'u uzaktan resetler |
| 🌙 **Uyku Modu** | LED'leri kapatır/açar (Wemos bağlı kalır) |
| ⚠️ **Wi-Fi Sıfırla** | Wemos'un Wi-Fi ayarlarını fabrika ayarlarına döndürür |
| 💾 **Manuel IP Kaydet** | Wemos IP'sini elle girerek bağlanmanızı sağlar |

### Sidebar Menüsü

| Menü | İşlev |
|---|---|
| 📊 **Genel Bakış** | Sistem durumu, FPS ve bağlantı bilgisi |
| 🔍 **Ağ Taraması** | Wemos cihazını ağda arar |
| 💾 **Ayarları Kaydet** | LED konfigürasyonunu (kenar LED sayıları) kaydeder |
| 🔄 **Yeniden Başlat** | Uygulamanın tamamını yeniden başlatır |

---

## 🏗️ Proje Yapısı

```
luxedge/
├── main.js              # Electron ana süreç (pencere, IPC, Python yönetimi)
├── preload.js           # Güvenli IPC köprüsü (contextIsolation)
├── launcher.js          # Electron başlatıcı
├── ambilight_pc.py      # Python backend (ekran yakalama, UDP, HTTP API)
├── package.json         # Node.js bağımlılıkları ve build ayarları
├── requirements.txt     # Python bağımlılıkları
├── ambilight_config.json # Kullanıcı ayarları (otomatik oluşturulur)
├── web_ui/
│   └── index.html       # Kontrol paneli arayüzü (tek dosya SPA)
├── wemos_code/
│   └── wemos_code.ino   # Wemos (ESP8266) Arduino kodu
├── LICENSE              # CC BY-NC 4.0 Lisans
└── README.md            # Bu dosya
```

---

## 🔧 Mimari

```
┌────────────────────────────────────────────────────┐
│                    ELECTRON                         │
│  ┌──────────┐  IPC  ┌──────────┐                  │
│  │ Renderer │◄─────►│   Main   │                  │
│  │(index.html)│      │ (main.js)│                  │
│  └──────────┘       └────┬─────┘                  │
│                          │ child_process           │
│                          ▼                         │
│                    ┌──────────┐                    │
│                    │  Python  │ HTTP API (:8888)   │
│                    │ Backend  │                    │
│                    └────┬─────┘                    │
│                         │ UDP (:7777)              │
│                         ▼                          │
│                    ┌──────────┐                    │
│                    │  Wemos   │                    │
│                    │(ESP8266) │ → NeoPixel LED     │
│                    └──────────┘                    │
└────────────────────────────────────────────────────┘
```

---

## 🔌 API Endpoints (Python Backend - Port 8888)

| Endpoint | Method | Açıklama |
|---|---|---|
| `/api/status` | GET | Sistem durumu (FPS, bağlantı, LED bilgileri) |
| `/api/scan` | GET | Ağda Wemos taraması başlatır |
| `/api/config` | POST | Konfigürasyon günceller |
| `/api/wemos/restart` | POST | Wemos'u yeniden başlatır |
| `/api/wemos/sleep` | POST | Wemos uyku modunu değiştirir |
| `/api/wemos/reset_wifi` | POST | Wemos Wi-Fi ayarlarını sıfırlar |

---

## 📝 Sürüm Geçmişi

### v1.2.0 (Güncel)
- ✅ Ağ taraması ile bulunan IP'nin otomatik kaydedilmesi
- ✅ Worker'ın IP değişikliklerini canlı algılaması (restart gerekmez)
- ✅ UDP PING/PONG bağlantı kontrolü (Wemos firmware uyumlu)
- ✅ Manuel IP girişi ve kaydetme
- ✅ Buton açıklamaları ve tooltip'ler
- ✅ Otomatik başlatma sadece ilk kurulumda

### v1.1.0
- Electron UI ve sistem tepsisi desteği
- Captive Portal ile Wemos Wi-Fi kurulumu

### v1.0.0
- İlk sürüm: Temel ambilight işlevselliği

---

## ⚠️ Sorun Giderme

| Sorun | Çözüm |
|---|---|
| Wemos bulunamıyor | Aynı Wi-Fi ağında olduğundan emin olun, Manuel IP deneyin |
| LED'ler yanmıyor | Güç kaynağını kontrol edin, LED_COUNT değerini doğrulayın |
| Düşük FPS | Ekran çözünürlüğünü düşürün veya EDGE_WIDTH değerini azaltın |
| Bağlantı kopuyor | Wemos'u yeniden başlatın, Wi-Fi sinyal gücünü kontrol edin |
| Python başlamıyor | `pip install -r requirements.txt` ile kütüphaneleri kurun |

---

## 📄 Lisans

Bu proje **Creative Commons Attribution-NonCommercial 4.0 International (CC BY-NC 4.0)** lisansı altındadır.

- ✅ Kişisel kullanım serbesttir
- ❌ Ticari kullanım yasaktır

Detaylar için [LICENSE](LICENSE) dosyasına bakın.

---

<p align="center">
  <sub>Made with ❤️ for DIY enthusiasts</sub>
</p>
