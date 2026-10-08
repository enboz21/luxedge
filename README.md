<p align="center">
  <h1 align="center">✨ LuxEdge — DIY Ambilight Sistemi</h1>
  <p align="center">
    <strong>Monitör arkası LED aydınlatmasını otomatik olarak yöneten, Electron + Python tabanlı masaüstü uygulaması</strong>
  </p>
  <p align="center">
    <img src="https://img.shields.io/badge/version-1.6.4-blue?style=flat-square" alt="Version">
    <img src="https://img.shields.io/badge/platform-Windows-0078D6?style=flat-square&logo=windows&logoColor=white" alt="Platform">
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

1. **Python backend** monitör görüntüsünü `mss` ile yakalar; kenar renklerini `NumPy` ile hesaplar
2. Renk verileri **UDP paketleri** olarak Wemos'a gönderilir
3. **Wemos** gelen verilere göre **NeoPixel LED'leri** kontrol eder
4. **Electron arayüzü** sistemi yönetmenizi sağlar

---

## ✨ Özellikler

| Özellik | Açıklama |
|---|---|
| 🖥️ **Gerçek Zamanlı Ekran Yakalama** | Ekran kenarlarını ayarlanabilir kare hızında analiz eder; varsayılan hedef 60 FPS'tir |
| 🖥️ **Çoklu Monitör & Ekran Seçimi** | Çift/üçlü monitörlü sistemlerde LED'lerin takip edeceği ekranı arayüzden seçme imkanı |
| 🔄 **Elle Monitör Yenileme** | Liste açılışta yüklenir; sonraki taramalar yalnız monitör seçicisinin yanındaki Güncelle düğmesiyle yapılır |
| 📡 **Otomatik Ağ Taraması** | Wemos cihazını ağda otomatik bulur (UDP Discovery) |
| 🔌 **UDP PING/PONG Bağlantı Kontrolü** | Wemos'a gerçek zamanlı bağlantı durumu takibi |
| 💡 **Esnek LED Konfigürasyonu** | Üst, alt, sol, sağ kenar LED sayılarını ayrı ayrı ayarlama |
| 🌙 **Uyku Modu** | LED'leri uzaktan kapatma/açma (cihaz bağlı kalır) |
| 🎨 **Bekleme (Idle) Modu** | Tam ekranda uygulama olmadığında sabit renk (Windows Teması uyumlu) yakma özelliği |
| 🔄 **Uzaktan Yeniden Başlatma** | Wemos'u arayüzden resetleme |
| 📊 **Performans İzleme** | FPS (renk kodlu), gönderilen paket ve hata sayısı; FPS 45+ yeşil, 25-45 sarı, 25 altı kırmızı |
| 📋 **Gerçek Zamanlı Log Paneli** | Tüm sistem olayları (bağlantı, hata, FPS uyarıları) renkli log panelinde görüntülenir; log dosyası `%APPDATA%/LuxEdge/luxedge.log` |
| 🔧 **Manuel IP Girişi** | Otomatik tarama çalışmazsa IP'yi elle girme |
| 💾 **Config Otomatik Kayıt** | Ayarlar JSON dosyasında saklanır |
| 🖱️ **Sistem Tepsisi** | Arka planda çalışır, tepsiden erişilir |
| ⚙️ **Windows Optimizasyonları** | Paylaşılan kare yakalama, toplu RGB paketleme, tek uygulama kilidi ve gizli arayüzde durdurulan sorgular |

### Çoklu Wemos geçişi (güvenli mod)

- Mevcut tek-Wemos ayarı korunur; çoklu mod kapalıyken uygulama eski `wemos_ip` UDP yolunu kullanır.
- Yeni cihazlar önce pasif kaydedilir. Her cihaz bir monitöre ve kendi dört kenar LED toplamına atanır.
- Çoklu mod yalnız etkin cihazların Wemos `/status` içindeki `led_count` değeri uygulamadaki LED toplamıyla eşleştiğinde açılabilir.
- Çoklu mod kapatıldığında worker'ın sonraki ayar kontrolünde tekli moda dönülür. LED toplamı doğrulanmayan cihazlar gönderim listesinden çıkarılır; mevcut ağ doğrulaması worker içinde beklemeye neden olabilir.
- Wemos pin/şerit ayarı firmware'in kendi kurulum sayfasında kalır; PC uygulaması yalnız toplam LED sayısını doğrular.

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

- **Hazır kurulum paketi:** Windows 10/11 x64. Python backend ve Electron pakete dahildir; ayrıca Python veya Node.js kurmanız gerekmez.
- **Kaynak koddan çalıştırma:** Node.js 18+ ve Python 3.10+; Python bağımlılıkları `requirements.txt` içindedir.
- **Paket üretme:** Bunlara ek olarak PyInstaller gerekir.

Linux desteği, derleme betiği ve paket hedefleri kaldırılmıştır. Mevcut hedef yalnız Windows x64'tür.

---

## 🚀 Kurulum

Hazır paket için `LuxEdge Setup 1.6.4.exe` dosyasını kullanın. Aşağıdaki adımlar geliştiriciler içindir.

### 1. Projeyi İndirin
```powershell
git clone https://github.com/enboz21/luxedge.git
cd luxedge
```

### 2. Node.js Bağımlılıklarını Kurun
```powershell
npm ci
```

### 3. Python Bağımlılıklarını Kurun
Proje klasöründe bir sanal ortam oluşturun; Electron geliştirme modunda bu ortamı kullanır:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 4. Wemos'u Programlayın

Bu adım yeni donanım kurulumu içindir. Bu Windows optimizasyonları firmware'i değiştirmediğinden çalışan Wemos'u yeniden programlamanız gerekmez.

1. Arduino IDE'yi açın
2. `wemos_code/wemos_code.ino` dosyasını yükleyin
3. ESP8266 kart desteğini ekleyin (Araçlar → Kart → ESP8266)
4. Gerekli kütüphaneleri kurun:
   - `Adafruit NeoPixel`
   - `ESP8266WiFi` (dahili)
5. Kodu Wemos'a yükleyin

> ⚠️ **ÖNEMLİ:** `wemos_code/wemos_code.ino` dosyasında herhangi bir değişiklik yaptığınızda, Arduino IDE'den **`firmware_D2.bin`** dosyasını yeniden derleyip Wemos'a yüklemek zorundasınız. Aksi halde yeni özellikler veya değişiklikler Wemos'ta çalışmaz.

### 5. Uygulamayı Çalıştırın
```bash
npm start
```

---

## 🔨 Derleme & Paketleme (Build)

Kaynak kurulum adımlarından sonra proje kökünde çalıştırın:

```powershell
.\.venv\Scripts\python.exe -m pip install pyinstaller
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build-windows.ps1 -Python .\.venv\Scripts\python.exe
```

Betik önce backend'i `dist/backend/lush_backend.exe` olarak derler, proje kökündeki
`lush_backend.exe` dosyasını günceller ve Windows x64 NSIS paketini üretir.
Electron için `node_modules/electron/dist` kullanılır; derleme önbellekleri `build/`
altında tutulur. Gerekli NSIS araçları önbellekte yoksa ilk derlemede indirilir.
Paketlenen backend'in SHA256 özeti kaynak binary ile karşılaştırılır.

Çıktı: **`dist/LuxEdge Setup 1.6.4.exe`**. Bu işlem kurulum veya canlı uygulama testi yapmaz.
`npm run dist` yalnız Electron paketini üretir; Python kaynakları değiştiğinde güncel
backend'in pakete girmesi için yukarıdaki tam derleme betiğini kullanın.

Paket dosyalarını uygulamayı başlatmadan kontrol etmek için:

```powershell
.\.venv\Scripts\python.exe scripts/verify-package.py
node scripts/verify-asar.js
```

Python bytecode karşılaştırması için backend'i derlerken kullandığınız Python ortamını kullanın.

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
| **Güncelle** (monitör seçicisi yanında) | Monitör listesini yeniden tarar; iki monitör seçicisini birlikte yeniler |
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

### Monitör listesini yenileme

Liste uygulama açılışında yüklenir; durum sorguları veya pencereyi yeniden göstermek
yeni tarama başlatmaz. Ekran eklediğinizde, çıkardığınızda ya da ekran düzenini
değiştirdiğinizde **Güncelle** düğmesine basın. Yenileme sırasında düğmeler geçici
olarak pasif olur. Başarısız yenilemede önceki liste korunur; işlem cihaz veya LED
ayarlarını otomatik kaydetmez.

Kullanıcı ayarları `%APPDATA%\LuxEdge\ambilight_config.json` içinde saklanır.
Projedeki JSON dosyası başlangıç ayarları içindir. Worker ayarları bellekte kullanır
ve dosya değişikliklerini 3 saniyelik kontrol aralığında alır.

---

## 🏗️ Proje Yapısı

```
luxedge/
├── main.js              # Electron ana süreç (pencere, IPC, Python yönetimi)
├── preload.js           # Güvenli IPC köprüsü (contextIsolation)
├── launcher.js          # Electron başlatıcı
├── ambilight_pc.py      # Python backend kaynak kodu
├── frame_processing.py  # Kenar renkleri, önbellek ve toplu RGB paketleme
├── monitor_catalog.py   # Açılışta ve elle yenilenen monitör listesi
├── lush_backend.exe     # Python backend (PyInstaller ile derlenmiş)
├── package.json         # Node.js bağımlılıkları ve build ayarları
├── package-lock.json    # Bağımlılık versiyon kilitleme
├── requirements.txt     # Python bağımlılıkları
├── ambilight_config.json # Başlangıç ayarları; aktif ayarlar APPDATA altında
├── web_ui/
│   ├── index.html       # Kontrol paneli arayüzü
│   ├── polling.js       # Görünürlük ve çakışmayan sorgu yönetimi
│   └── monitors.js      # Monitör seçenekleri ve Güncelle düğmesi
├── scripts/             # Windows derleme ve paket doğrulama araçları
├── tests/               # Yapay görüntü, taklit ağ ve süreç testleri
├── docs/                # Optimizasyon raporu ve kenar yakalama araştırması
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
| `/api/status` | GET | Sistem durumu ve önbellekteki monitör listesi; yeniden tarama yapmaz |
| `/api/monitors/refresh` | POST | Monitör listesini elle yeniler; `success`, `monitors`, `monitor_list_version` döndürür |
| `/api/scan` | GET | Ağda Wemos taraması başlatır |
| `/api/config` | POST | Konfigürasyon günceller |
| `/api/logs` | GET | Son log kayıtlarını döndürür |
| `/api/devices` | GET / POST | Cihazları listeler / yeni pasif cihaz kaydeder |
| `/api/devices/{id}` | PUT / DELETE | Cihazı günceller / siler |
| `/api/devices/mode` | POST | Çoklu cihaz modunu değiştirir |
| `/api/devices/scan` | GET | Ağdaki Wemos cihazlarını arar |
| `/api/devices/validation` | GET | Cihazların firmware LED toplamlarını doğrular |
| `/api/wemos/restart` | POST | Wemos'u yeniden başlatır |
| `/api/wemos/sleep` | POST | Wemos uyku modunu değiştirir |
| `/api/wemos/reset_wifi` | POST | Wemos Wi-Fi ayarlarını sıfırlar |

---

## 📝 Sürüm Geçmişi

### Geliştirme değişiklikleri — Windows optimizasyonu (8 Ekim 2026)

Paket sürümü `1.6.4` olarak korunmuştur; aşağıdaki değişiklikler yeni bir sürümün yayımlandığı anlamına gelmez.

- **Monitör listesi:** Açılışta yükleme, elle Güncelle düğmesi ve durum sorgularında önbellek kullanımı.
- **Görüntü işleme:** Kare başına ayar okuması ve gereksiz gölge yakalamalar kaldırıldı; aynı monitörün görüntüsü kare içinde paylaşılıyor.
- **RGB paketleme:** NumPy dizisinden toplu byte üretimi; LED sırası, paket biçimi ve parlaklıkta kesme davranışı korundu.
- **Zamanlama:** 60 FPS hedefi korundu; monoton saat ve worker sonunda serbest bırakılan zamanlayıcı kaynağı kullanılıyor.
- **Arayüz ve süreçler:** Tek uygulama kilidi, yalnız uygulamanın kendi backend'ini hedefleyen asenkron kapatma ve gizli pencerede durdurulan durum/log sorguları.
- **Platform:** Linux kodu, binary'si ve paket hedefleri kaldırıldı. Windows paketinde sistem Python fallback'i bulunmuyor.
- **Uyumluluk:** Mevcut Wemos bağlantı/tarama davranışı, firmware ve kullanıcı ayarları korundu.

### v1.6.4 (Güncel)
- 🛟 **Kademeli Wemos Kurtarma:** LED veri akışı kesildiğinde UDP soketi, Wi-Fi bağlantısı ve son çare olarak Wemos yeniden başlatması kontrollü aşamalarla uygulanır; yeniden başlatma döngüsü oluşmaz.
- 📊 **Gelişmiş Cihaz Tanılama:** Wemos `/status` yanıtına RSSI, çalışma süresi, UDP durumu, son veri yaşı, kurtarma sayaçları ve reset sebebi eklendi.
- 🔍 **Gerçek Bağlantı Doğrulaması:** PC uygulaması artık yalnız paket göndermeyi bağlantı kanıtı saymaz; Wemos'tan gelen UDP `PONG` veya HTTP `/status` yanıtını doğrular.
- 🔋 **Düşük Güçlü Sistem Animasyonları:** Wi-Fi, hotspot, OTA ve bekleme göstergeleri normal ambilight parlaklığını değiştirmeden yaklaşık %25 güçle çalışır.
- 🪟 **Windows Paketleme Düzeltmesi:** Konsolsuz `lush_backend.exe` başlatılırken `stdout/stderr` bulunmamasından kaynaklanan sessiz açılış hatası giderildi.
- 🖥️ **Çoklu Monitör & Hedef Ekran Desteği:** Kontrol paneline "🖥️ Hedef Monitör / Ekran Seçimi" açılır kutusu eklendi. Sisteminizdeki tüm fiziksel monitörler (çözünürlük ve birincil etiketleriyle) otomatik tespit edilir. Çift monitörlü kurulumlarda sanal masaüstünün (`monitors[0]`) taranması engellendi; tam ekranda sol/son LED'lerin sönük kalması veya yanmaması sorunu kökten çözüldü.
- 🔴 **Wemos UDP Kararlılığı & Bellek Sızıntısı Çözümü:** `Udp.begin(UDP_PORT)` çağrısının `loop()` içinde her döngüde kontrolsüz tekrarlanması engellendi (`udpRunning` durumu eklendi). Bellek tükenmesi (heap fragmentation), paket kaybı ve Watchdog Timer (WDT) resetleri durduruldu.
- 📡 **Hotspot Modu UDP İyileştirmesi:** Wemos Hotspot (`Wemos_Setup`) modundayken de UDP dinleyicisi başlatılarak ilk kurulumda otomatik cihaz bulma (Discovery) yanıtlarının her iki modda da kusursuz çalışması sağlandı.
- ⚡ **60 FPS UART Bloklaması Kaldırıldı:** Her LED paketi alındığında çağrılan `Serial.println("LED verisi alındı")` kaldırıldı. Saniyede 60 seri port yazımının Wi-Fi stack'ini dondurması ve bağlantı koparması önlendi.
- 🛡️ **Akıllı Bağlantı Kontrolü & Boş IP Koruması:** Ekrandan gerçek veri akışı varken (FPS > 1) Wemos'a her 3 saniyede gereksiz UDP PING ve HTTP GET istekleri atılması engellendi. Arayüzde ayar değişikliği sırasında boş IP gönderilerek kayıtlı Wemos IP'sinin silinmesi önlendi.
- 🚀 **Port & Süreç Yönetimi (önceki davranış):** Açılışta ada göre backend temizliği kullanılıyordu. Güncel geliştirme değişiklikleri bu yöntemi tek uygulama kilidi ve sahip olunan süreç ağacının kapatılmasıyla değiştirdi.

### v1.6.2
- 🎚️ **Hedef FPS Ayarı:** Web arayüzüne canlı "Hedef FPS" slider'ı eklendi (10-60 FPS). Artık Wemos'un Wi-Fi yükünü azaltmak ve bağlantıyı rahatlatmak için kare hızını düşürebilirsiniz.
- ⏱️ **Windows Zamanlayıcı Hassasiyeti:** Windows platformunda milisaniyelik uykuların (`time.sleep`) kararlı çalışması için Windows Multimedia Timer API (`timeBeginPeriod(1)`) entegre edildi. Hedef FPS değerine milisaniyelik tam doğrulukla kilitlenir.
- 🎨 **Dinamik Performans Çemberi:** Arayüzdeki FPS çemberinin rengi ve doluluk yüzdesi artık sabit 60 FPS'e göre değil, senin ayarladığın hedef FPS'e göre dinamik olarak güncellenir.

### v1.6.0
- ✨ **Yeni Özellik (OTA Güncelleme):** Wemos cihazını bilgisayara kabloyla bağlamaya gerek kalmadan, doğrudan Wi-Fi üzerinden (uygulama arayüzünden) güncelleyebilme imkanı eklendi.
- 🐛 **Kritik Stabilite Çözümleri:** Wemos'un çalışırken aniden kapanıp açılmasına (watchdog timer reset) sebep olan donanım kesme (`noInterrupts`) çakışmaları tamamen giderildi. 
- 📡 **Geliştirilmiş Yeniden Bağlanma:** Anlık Wi-Fi kopmalarında cihazın pes edip hemen Hotspot moduna geçmesi engellendi; artık 120 saniye (2 dakika) boyunca ağa sürekli yeniden bağlanmaya çalışıyor.
- 🔒 **Güvenlik İyileştirmeleri:** Wemos'taki güvenlik açıkları (SSID bazlı XSS vb.) kapatıldı ve bilgisayar tarafındaki Python uygulamasının dış ağa kapanarak yalnızca yerel cihazla (127.0.0.1) konuşması sağlandı.

### v1.5.5
- ✨ **Yeni Özellik:** Arayüze "Tam Ekran Maksimum Parlaklık" ayarı eklendi. Sistem sadece tam ekran modundayken LED'lerin çıkabileceği maksimum parlaklık sınırlandırılabilir.
- ⚡ **Yeni Özellik:** Maksimum parlaklık ayarının hemen yanına, tüm LED'lerin tam beyaz yanması durumunda donanımın çekeceği tahmini **Maksimum Akım (Amper)** bilgisini gösteren dinamik bir gösterge eklendi.

### v1.6.1
- 🚀 **FPS Optimizasyonu:** Ekran yakalama motoru tamamen yeniden yazıldı. PIL `Image.crop()` döngüsü kaldırılarak tek bir numpy dizi üzerinde vektörize işlem yapılmaya başlandı. Ek olarak frame işlem süresi ölçülerek `sleep_time`'dan düşülüyor (adaptive sleep). Sonuç: ~3-4× daha hızlı renk hesaplama, hedef 60 FPS'e çok daha yakın gerçek FPS.
- 🔌 **Bağlantı Kararlılığı:** `MAX_FAILS` 2'den 3'e çıkarıldı; bağlı→bağlı değil geçişinde 2 saniyelik debounce eklendi; yeniden bağlanmak için 2 ardışık başarı gerekiyor. Böylece geçici Wi-Fi paket kayıplarında arayüzde titreme yaşanmıyor.
- 📋 **Log Sistemi:** Tüm sistem olayları (bağlantı değişimleri, hata, FPS uyarıları, worker başlatma) hem `%APPDATA%/LuxEdge/luxedge.log` dosyasına (500 KB rotating, 3 yedek) hem de arayüzdeki canlı Log Paneli'ne yazılıyor. Log seviyelerine göre renk kodlama (INFO=mavi, WARNING=sarı, ERROR=kırmızı).
- 🔄 **Hata Sayacı Sıfırlama:** `ambilight_worker` her başladığında hata sayacı, paket sayacı ve FPS değeri otomatik olarak sıfırlanıyor. Uygulamayı kapatıp açsanız bile sayaçlar 0'dan başlıyor.
- 🎨 **FPS Renk Göstergesi:** Performans kartındaki FPS çemberi gerçek FPS'e göre renk değiştiriyor (45+ FPS = yeşil, 25-45 = sarı, 25 altı = kırmızı).
- 📋 **Sidebar Log Butonu:** Sidebar'a "📋 Sistem Logları" navigasyon öğesi eklendi; log paneli sağ kenardan açılıyor, loglar 2 saniyede bir yenileniyor ve TXT olarak indirilebiliyor.

### v1.6.0
- 🐛 **Wemos Kararlılık İyileştirmeleri:** ESP8266 NeoPixel sinyal kesilmelerini önlemek için `strip.show()` çağrılarına interrupt (kesme) koruması eklendi.
- 📡 **Gelişmiş Wi-Fi Yeniden Bağlanma:** Bağlantı koptuğunda hemen hotspot moduna geçmek yerine 5 kez yeniden bağlanma denemesi (10 saniye aralıklarla) eklendi.
- 🚀 **Performans Optimizasyonları:** Wemos arayüzünde heap fragmentation (bellek parçalanması) sorununu önlemek için Wi-Fi tarama sonuçları önbelleğe alındı (30 saniye) ve HTML oluşturma iyileştirildi.
- 🌐 **Karakter Düzeltmeleri:** Wemos kurulum sayfasında sorun çıkaran Türkçe karakterler İngilizce karşılıklarıyla değiştirildi.

### v1.5.3
- 🐛 **Çoklu Monitör Fullscreen Algılama Düzeltmesi (Windows):** LED monitöründe tam ekran bir uygulama (oyun, YouTube vb.) açıkken ikinci ekrana tıklanması durumunda LED'lerin sabit idle rengine geçmesi sorunu giderildi. Artık focus hangi ekranda olursa olsun, LED monitöründe tam ekran pencere varsa ekran renkleri takip edilmeye devam eder.
- 🔧 **DWM Cloaked Filtresi:** Windows'un arka planda tuttuğu görünmez sistem pencerelerinin (ör: "Windows Giriş Deneyimi") yanlışlıkla tam ekran olarak algılanması engellendi.

### v1.5.2
- ✨ **Arayüz ve Wemos Senkronizasyon İyileştirmeleri:** Sadece belirli LED'leri kapattığınızda yaşanan hayalet LED sorunu giderildi; toplam LED sayısı değiştiğinde otomatik "Blackout (karartma)" paketi gönderilerek eski renklerin cihazda asılı kalması engellendi.

### v1.5.1
- ✨ **UDP Unicast Taraması:** Güvenlik duvarı/modem yüzünden Cihaz Bulma'yı engelleyen Broadcast kısıtlamalarına karşı yedek olarak *Unicast subnet sweep* eklendi.

### v1.4.2
- ✅ **Bekleme (Idle) Modu:** Monitörde oyun/video gibi gerçek bir tam ekran uygulama çalışmadığında LED'lerin sabit bir renkte (kullanıcının seçtiği) yanmasını sağlayan özellik eklendi.
- ✅ **Windows Tema Rengi Senkronizasyonu:** Bekleme modundayken, eğer istenirse rengin otomatik olarak o anki "Windows Ana Tema Rengi" ile eşzamanlı olması sağlandı.
- ✅ **Gecikmeli Otomatik Kayıt:** Her ayar değiştiğinde kullanıcıyı rahatsız etmeden arka planda debounce timer ile (0.6 - 1.5 saniye gecikmeli) ayarları kaydeden sessiz "Auto-Save" mekanizması eklendi.

### v1.3.0
- ✅ **Canlı Bildirimler:** Arayüz üzerinden değiştirilen LED sayıları, Tarama Kalınlığı (Edge Width) ve İçeri Kaydırma (Edge Offset) verilerinin **uygulama yeniden başlatılmadan** canlı olarak backend'e yansıtılması sağlandı.
- ✅ **Gelişmiş Görüntü Analizi:** Ekrandan alınan bölgesel renk oranlarında renk ortalaması hesaplaması gerçekçi tonları alacak şekilde optimize edildi.
- ✅ **İçeri Kaydırma (Offset):** Tam olarak ekran kenarı yerine, kenardan biraz daha içerideki piksellerin izlenebilmesine olanak tanındı.
- ✅ **Zombi İşlem Fix'i:** Uygulama sistem çekmecesinden (Tray Menu) veya Görev Çubuğundan kapatıldığında, arka planda çalışan Python sürecinin (`lush_backend.exe`) açık kalma sorunu çözüldü (Senkron Taskkill yordamı eklendi).

### v1.2.1
- ✅ Paketlenmiş uygulamada Python backend başlatma düzeltmesi
- ✅ lush_backend.exe yeniden derlendi (güncel UDP PING/PONG)
- ✅ Detaylı backend başlatma logları
- ✅ Fallback: exe yoksa sistem Python'ı ile çalıştırma

### v1.2.0
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
| Düşük FPS / Windows takılması | Hedef ve gerçekleşen FPS'yi, logları ve sistem yükünü karşılaştırın; son optimizasyonların canlı performans sonucu henüz ölçülmedi |
| Monitör listesi eski | Monitör seçicisinin yanındaki Güncelle düğmesine basın |
| Bağlantı kopuyor | Wemos'u yeniden başlatın, Wi-Fi sinyal gücünü kontrol edin |
| Python başlamıyor (kaynak kod) | `.venv` ortamını ve bu ortamdaki `requirements.txt` bağımlılıklarını kontrol edin |
| Backend bulunamıyor (hazır paket) | Windows paketini yeniden kurun; paketlenmiş uygulama sistem Python'ına geçmez |

---

## 🧪 Program İçi Testler ve Doğrulama

Kaynak kurulum adımlarından sonra:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
node --test tests/electron.test.js tests/monitors.test.js
```

8 Ekim 2026 doğrulamasında **31 Python + 13 JavaScript testi (44 toplam)** geçti.
Testler yapay ekran görüntüleri, taklit ağ/süreç nesneleri ve geçici ayarlar kullanır;
gerçek Wemos'a bağlanmaz, ekran yakalamaz veya kurulum çalıştırmaz.
Renk ve LED sırası, tüm 0–255 parlaklık değerleri, monitör yenileme, gizli arayüz,
süreç yönetimi ve paket içeriği kontrol edilmiştir.

**Gerçek cihaz, ekran, oyun, canlı performans ve kurulum testleri henüz yapılmadı.**
Windows takılmalarının giderildiği veya gerçek CPU/GPU yükünün azaldığı doğrulanmış değildir.

- [Windows optimizasyonu ve doğrulama raporu](docs/WINDOWS_OPTIMIZATION.md)
- [Yalnız kenar yakalama araştırması](docs/EDGE_CAPTURE_RESEARCH.md): 18 yapay görüntü senaryosunda renk eşitliği doğrulandı; mevcut uygulama hâlâ tam monitör görüntüsü yakalar. Kenar yakalama üretimde etkin değildir.

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
