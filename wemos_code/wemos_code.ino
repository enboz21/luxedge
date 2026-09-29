#include <ESP8266WiFi.h>
#include <WiFiUdp.h>
#include <ESP8266WebServer.h>
#include <Adafruit_NeoPixel.h>
#include <EEPROM.h>
#include <DNSServer.h> // --- EKLENDİ: Captive Portal için gerekli kütüphane ---
#include <ArduinoOTA.h>
#include <ESP8266HTTPUpdateServer.h>

#define LED_PIN   D2       // D2 pini (GPIO4) - Wemos D1 Mini
#define LED_COUNT 74       // 34 + 0 + 20 + 20 (config ile eşleşmeli)
#define FIRMWARE_VERSION "1.6.4"
#define UDP_PORT  7777
#define DNS_PORT  53       // --- EKLENDİ: DNS Portu ---
#define STATUS_BRIGHTNESS 64 // Sistem animasyonları: yaklaşık %25 güç

// Hotspot ayarları
const char* ap_ssid = "Wemos_Setup";
const char* ap_password = "";  // Şifresiz hotspot

// Wi-Fi bilgileri (EEPROM'dan okunacak)
String saved_ssid = "";
String saved_password = "";

WiFiUDP Udp;
ESP8266WebServer server(80);
DNSServer dnsServer; // --- EKLENDİ: DNS Sunucu nesnesi ---
Adafruit_NeoPixel strip(LED_COUNT, LED_PIN, NEO_GRB + NEO_KHZ800);
ESP8266HTTPUpdateServer httpUpdater; // OTA HTTP güncelleyici nesnesi

bool isHotspotMode = false;
bool udpRunning = false;
bool networkServicesNeedRestart = false;

// WiFi reconnect yönetimi (global, connectToWiFi() içinden sıfırlanır)
int reconnectCount = 0;
unsigned long lastReconnectAttempt = 0;

// Bekleme animasyonu değişkenleri
unsigned long lastDataTime = 0;      // Son LED verisi zamanı
unsigned long lastAnimUpdate = 0;    // Son animasyon güncellemesi
int idleAnimPos = 0;                 // Halka pozisyonu
bool receivingData = false;          // PC'den veri geliyor mu?
bool isSleepMode = false;            // Uyku modu (LED'ler kapalı)
bool hasReceivedLedData = false;      // Bu açılışta en az bir geçerli LED paketi alındı mı?
unsigned long lastUdpActivityTime = 0;// Son geçerli UDP paketi zamanı (PING/discovery/LED)
unsigned long lastHttpActivityTime = 0;// Son HTTP /status isteği zamanı
unsigned int udpRebindCount = 0;      // Tanılama: UDP soketi kaç kez yenilendi
unsigned int wifiRecoveryCount = 0;   // Tanılama: Wi-Fi kaç kez kurtarılmaya çalışıldı
String bootResetReason = "unknown";  // ESP'nin bildirdiği son reset sebebi
uint8_t dataRecoveryStage = 0;        // 0=normal, 1=UDP yenilendi, 2=Wi-Fi yenilendi

const unsigned long UDP_RECOVERY_DELAY_MS = 5000UL;
const unsigned long WIFI_RECOVERY_DELAY_MS = 20000UL;
const unsigned long RESTART_RECOVERY_DELAY_MS = 60000UL;

// EEPROM adresleri
#define EEPROM_SIZE 128
#define SSID_ADDR 0
#define PASSWORD_ADDR 64

// Fonksiyon prototipleri (Hata almamak için)
void handleRoot();
void handleWiFiConfig();
void handleStatus();
void handleRestart();
void handleToggleSleep();
void handleResetWifi();
void connectToWiFi();
bool restartUdpListener(const char* reason);

uint32_t statusColor(uint8_t r, uint8_t g, uint8_t b) {
  uint8_t scaledR = (uint16_t)r * STATUS_BRIGHTNESS / 255;
  uint8_t scaledG = (uint16_t)g * STATUS_BRIGHTNESS / 255;
  uint8_t scaledB = (uint16_t)b * STATUS_BRIGHTNESS / 255;
  return strip.Color(scaledR, scaledG, scaledB);
}

bool restartUdpListener(const char* reason) {
  Udp.stop();
  udpRunning = false;
  delay(10);

  if (WiFi.status() != WL_CONNECTED) {
    Serial.print("UDP yenilenemedi, Wi-Fi bagli degil. Sebep: ");
    Serial.println(reason);
    return false;
  }

  if (Udp.begin(UDP_PORT)) {
    udpRunning = true;
    udpRebindCount++;
    Serial.print("UDP soketi yenilendi. Sebep: ");
    Serial.println(reason);
    return true;
  }

  Serial.print("UDP soketi yenilenemedi. Sebep: ");
  Serial.println(reason);
  return false;
}

// XSS koruması için HTML escape fonksiyonu
String htmlEscape(String input) {
  input.replace("&", "&amp;");
  input.replace("<", "&lt;");
  input.replace(">", "&gt;");
  input.replace("\"", "&quot;");
  input.replace("'", "&#39;");
  return input;
}

void saveWiFiCredentials(String ssid, String password) {
  EEPROM.begin(EEPROM_SIZE);
  // SSID kaydet
  for (int i = 0; i < 64; i++) {
    if (i < ssid.length()) {
      EEPROM.write(SSID_ADDR + i, ssid[i]);
    } else {
      EEPROM.write(SSID_ADDR + i, 0);
    }
  }
  
  // Password kaydet
  for (int i = 0; i < 64; i++) {
    if (i < password.length()) {
      EEPROM.write(PASSWORD_ADDR + i, password[i]);
    } else {
      EEPROM.write(PASSWORD_ADDR + i, 0);
    }
  }
  
  EEPROM.commit();
  EEPROM.end();
}

void loadWiFiCredentials() {
  EEPROM.begin(EEPROM_SIZE);
  
  // SSID oku
  saved_ssid = "";
  for (int i = 0; i < 64; i++) {
    char c = EEPROM.read(SSID_ADDR + i);
    if (c == 0) break;
    saved_ssid += c;
  }
  
  // Password oku
  saved_password = "";
  for (int i = 0; i < 64; i++) {
    char c = EEPROM.read(PASSWORD_ADDR + i);
    if (c == 0) break;
    saved_password += c;
  }
  
  EEPROM.end();
}

void startHotspot() {
  Serial.println("Hotspot modu başlatılıyor...");

  // Önce eski bağlantıları ve soketleri temizle
  Udp.stop();
  udpRunning = false;
  server.stop();
  dnsServer.stop();
  WiFi.disconnect(true);
  WiFi.softAPdisconnect(true);
  delay(100);

  // --- EKLENDİ: IP çakışmasını önlemek ve sabit IP vermek için ---
  WiFi.mode(WIFI_AP);
  WiFi.softAPConfig(IPAddress(192,168,4,1), IPAddress(192,168,4,1), IPAddress(255,255,255,0));
  WiFi.softAP(ap_ssid, ap_password);
  
  IPAddress IP = WiFi.softAPIP();
  Serial.print("Hotspot IP: ");
  Serial.println(IP);
  
  // --- EKLENDİ: DNS Sunucusunu başlat (Tüm (*) istekleri kendine yönlendir) ---
  dnsServer.setErrorReplyCode(DNSReplyCode::NoError);
  dnsServer.start(DNS_PORT, "*", IP);

  // HTTP server endpoint'leri
  server.on("/", handleRoot);
  server.on("/wifi_config", handleWiFiConfig);
  server.on("/status", handleStatus);
  // --- EKLENDİ: Android/iOS gibi cihazlar rastgele URL dener, onları da ana sayfaya atalım ---
  server.onNotFound(handleRoot); 
  
  server.begin();
  Udp.begin(UDP_PORT);
  udpRunning = true;
  networkServicesNeedRestart = false;
  
  isHotspotMode = true;
  
  // LED'i mavi yap (hotspot modu)
  for (int i = 0; i < LED_COUNT; i++) {
    strip.setPixelColor(i, statusColor(0, 0, 255));
  }
  strip.show();
}

// WiFi scan cache (handleRoot her çağrıldığında taranmaz, 30sn'de bir taranır)
static unsigned long lastScanTime = 0;
static int cachedScanCount = 0;

void handleRoot() {
  // 30 saniyede bir tara, arasında cache kullan
  unsigned long now = millis();
  if (now - lastScanTime > 30000 || cachedScanCount <= 0) {
    cachedScanCount = WiFi.scanNetworks();
    lastScanTime = now;
  }
  int n = cachedScanCount;

  // HTML'i parça parça oluştur (heap fragmentation önleme)
  String html = "<!DOCTYPE html><html><head><meta charset='UTF-8'><meta name='viewport' content='width=device-width,initial-scale=1'>";
  html += "<style>body{font-family:sans-serif;padding:20px;text-align:center;background:#f0f2f5}";
  html += "form{background:white;padding:20px;border-radius:8px;box-shadow:0 2px 4px rgba(0,0,0,.1);max-width:400px;margin:0 auto}";
  html += "h1{color:#333}select,input{padding:12px;margin:10px 0;width:100%;box-sizing:border-box;border:1px solid #ddd;border-radius:4px}";
  html += "button{padding:12px 20px;background:#007bff;color:#fff;border:none;border-radius:4px;cursor:pointer;width:100%;font-size:16px}";
  html += "button:hover{background:#0056b3}</style>";
  html += "<title>Wemos Kurulum</title></head><body>";
  html += "<br><h1>Wi-Fi Agini Sec</h1>";
  html += "<form action='/wifi_config' method='POST'>";

  html += "<label>Mevcut Aglar (" + String(n) + " bulundu):</label><br>";
  html += "<select name='ssid'>";

  if (n <= 0) {
    html += "<option>Ag bulunamadi</option>";
  } else {
    for (int i = 0; i < n; ++i) {
      String ssid = WiFi.SSID(i);
      int rssi = WiFi.RSSI(i);
      String enc = (WiFi.encryptionType(i) == ENC_TYPE_NONE) ? "" : " *";
      html += "<option value='" + htmlEscape(ssid) + "'>" + htmlEscape(ssid) + " (" + rssi + "dBm)" + enc + "</option>";
    }
  }
  html += "</select><br>";
  
  html += "<label>Wi-Fi Sifresi:</label><br>";
  html += "<input type='password' name='password' placeholder='Şifre' required><br><br>";
  
  html += "<button type='submit'>💾 Kaydet ve Baglan</button>";
  html += "</form>";
  html += "<p style='color:#666;font-size:12px;margin-top:20px'>Ambilight Projesi v2.0</p>";
  html += "</body></html>";
  
  server.send(200, "text/html", html);
}

void handleWiFiConfig() {
  if (server.method() == HTTP_POST) {
    String ssid = server.arg("ssid");
    String password = server.arg("password");
    
    if (ssid.length() > 0) {
      Serial.print("Wi-Fi bilgileri alındı - SSID: ");
      Serial.println(ssid);
      
      // Bilgileri kaydet
      saveWiFiCredentials(ssid, password);
      saved_ssid = ssid;
      saved_password = password;
      
      // Yanıt gönder
      server.send(200, "text/plain", "OK:WiFi bilgileri kaydedildi. Yeniden baslatiliyor...");

      delay(1000);

      // Wi-Fi'ye bağlan (temizlik connectToWiFi icinde yapiliyor)
      connectToWiFi();
    } else {
      server.send(400, "text/plain", "ERROR:SSID boş olamaz");
    }
  } else {
    server.send(405, "text/plain", "ERROR:Method not allowed");
  }
}

void handleStatus() {
  lastHttpActivityTime = millis();
  long lastDataAgeMs = hasReceivedLedData ? (long)(millis() - lastDataTime) : -1;
  long lastUdpAgeMs = lastUdpActivityTime > 0 ? (long)(millis() - lastUdpActivityTime) : -1;

  String status;
  status.reserve(512);
  status = "{\"mode\":\"";
  status += isHotspotMode ? "hotspot" : "wifi";
  status += "\",\"ip\":\"";
  status += isHotspotMode ? WiFi.softAPIP().toString() : WiFi.localIP().toString();
  status += "\",\"led_count\":";
  status += String(LED_COUNT);
  status += ",\"firmware_version\":\"";
  status += FIRMWARE_VERSION;
  status += "\"";
  status += ",\"sleep_mode\":";
  status += isSleepMode ? "true" : "false";
  status += ",\"wifi_connected\":";
  status += WiFi.status() == WL_CONNECTED ? "true" : "false";
  status += ",\"rssi\":";
  status += WiFi.status() == WL_CONNECTED ? String(WiFi.RSSI()) : "0";
  status += ",\"udp_running\":";
  status += udpRunning ? "true" : "false";
  status += ",\"receiving_data\":";
  status += receivingData ? "true" : "false";
  status += ",\"has_received_led_data\":";
  status += hasReceivedLedData ? "true" : "false";
  status += ",\"last_data_age_ms\":";
  status += String(lastDataAgeMs);
  status += ",\"last_udp_age_ms\":";
  status += String(lastUdpAgeMs);
  status += ",\"uptime_seconds\":";
  status += String(millis() / 1000UL);
  status += ",\"udp_rebind_count\":";
  status += String(udpRebindCount);
  status += ",\"wifi_recovery_count\":";
  status += String(wifiRecoveryCount);
  status += ",\"reset_reason\":\"";
  status += bootResetReason;
  status += "\"";
  status += "}";
  server.send(200, "application/json", status);
}

void handleRestart() {
  server.send(200, "text/plain", "OK: Yeniden baslatiliyor...");
  delay(500);
  ESP.restart();
}

void handleToggleSleep() {
  isSleepMode = !isSleepMode;
  if (isSleepMode) {
    strip.clear();
    strip.show();
    Serial.println("Uyku modu AKTIF: LED'ler kapatildi.");
    server.send(200, "text/plain", "SLEEP_ON");
  } else {
    Serial.println("Uyku modu KAPALI: Normal calisma.");
    server.send(200, "text/plain", "SLEEP_OFF");
  }
}

void handleResetWifi() {
  server.send(200, "text/plain", "OK: Wi-Fi ayarlari siliniyor ve reset atiliyor...");
  delay(500);

  // 1. Wi-Fi baglantısını kes ve kayitlilari UNUT (Flash'tan siler)
  WiFi.disconnect(true);
  delay(500);

  // 2. EEPROM'u da temizle
  EEPROM.begin(EEPROM_SIZE);
  for (int i = 0; i < EEPROM_SIZE; i++) {
    EEPROM.write(i, 0);
  }
  EEPROM.commit();
  EEPROM.end();

  Serial.println("Wi-Fi ayarlari silindi. Cihaz yeniden baslatiliyor...");
  delay(500);
  ESP.restart();
}

void connectToWiFi() {
  Serial.println();
  Serial.print("Wi-Fi'ye baglaniyor: ");
  Serial.println(saved_ssid);

  // Eski baglantilari ve soketleri temizle
  Udp.stop();
  server.stop();
  dnsServer.stop();
  WiFi.softAPdisconnect(true);
  WiFi.enableAP(false);
  delay(100);

  WiFi.mode(WIFI_STA);
  WiFi.begin(saved_ssid.c_str(), saved_password.c_str());
  
  // LED'i sarı yap (bağlanıyor) - döngüden önce 1 kere ayarla
  for (int i = 0; i < LED_COUNT; i++) {
    strip.setPixelColor(i, statusColor(255, 255, 0));
  }
  strip.show();

  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 60) {
    delay(500);
    Serial.print(".");
    attempts++;
  }
  
  if (WiFi.status() == WL_CONNECTED) {
    Serial.println();
    Serial.println("WiFi bağlandı!");
    Serial.print("IP adresi: ");
    Serial.println(WiFi.localIP());
    
    isHotspotMode = false;
    reconnectCount = 0;
    lastReconnectAttempt = 0;
    WiFi.setAutoReconnect(true);
    WiFi.persistent(true);
    
    // LED'i yeşil yap (bağlandı)
    for (int i = 0; i < LED_COUNT; i++) {
      strip.setPixelColor(i, statusColor(0, 255, 0));
    }
    strip.show();
    delay(1000);

    // LED'leri kapat
    strip.clear();
    strip.show();
    
    // UDP dinlemeye başla
    Udp.begin(UDP_PORT);
    udpRunning = true;
    
    // HTTP sunucusunu normal modda da başlat
    server.on("/", handleRoot);
    server.on("/wifi_config", handleWiFiConfig);
    server.on("/status", handleStatus);
    server.on("/restart", handleRestart);
    server.on("/toggle_sleep", handleToggleSleep);
    server.on("/reset_wifi", handleResetWifi);
    // OTA HTTP güncelleyici - /firmware adresinden erişilir
    httpUpdater.setup(&server, "/firmware");

    // ArduinoOTA ayarları
    ArduinoOTA.setHostname("luxedge-wemos");
    ArduinoOTA.onStart([]() {
      // OTA başladığında mor renk
      for (int i = 0; i < LED_COUNT; i++) {
        strip.setPixelColor(i, statusColor(128, 0, 255));
      }
      strip.show();
    });
    ArduinoOTA.onProgress([](unsigned int progress, unsigned int total) {
      // İlerleme göstergesi - mor dolum
      int ledProgress = (progress * LED_COUNT) / total;
      for (int i = 0; i < LED_COUNT; i++) {
        if (i <= ledProgress) {
          strip.setPixelColor(i, statusColor(128, 0, 255));
        } else {
          strip.setPixelColor(i, strip.Color(0, 0, 0));
        }
      }
      strip.show();
    });
    ArduinoOTA.onEnd([]() {
      // OTA tamamlandığında yeşil flaş
      for (int i = 0; i < LED_COUNT; i++) {
        strip.setPixelColor(i, statusColor(0, 255, 0));
      }
      strip.show();
    });
    ArduinoOTA.onError([](ota_error_t error) {
      // Hata durumunda kırmızı
      for (int i = 0; i < LED_COUNT; i++) {
        strip.setPixelColor(i, statusColor(255, 0, 0));
      }
      strip.show();
    });
    ArduinoOTA.begin();

    server.begin();
    networkServicesNeedRestart = false;
    
    Serial.println("UDP dinleme ve HTTP sunucu başlatıldı (Port 7777 + 80)");
  } else {
    Serial.println();
    Serial.println("WiFi bağlantısı başarısız! Hotspot moduna dönülüyor...");
    delay(2000);
    startHotspot();
  }
}

void setup() {
  Serial.begin(115200);
  delay(100);

  bootResetReason = ESP.getResetReason();
  bootResetReason.replace("\\", "/");
  bootResetReason.replace("\"", "'");
  
  Serial.println();
  Serial.println("=== Wemos Ambilight Başlatılıyor ===");
  Serial.print("LED sayısı: ");
  Serial.println(LED_COUNT);
  
  // LED başlat
  strip.begin();
  strip.clear();
  strip.show();
  
  // EEPROM'dan Wi-Fi bilgilerini yükle
  loadWiFiCredentials();
  
  // Kayıtlı Wi-Fi bilgisi varsa bağlan
  if (saved_ssid.length() > 0) {
    Serial.print("Kayıtlı Wi-Fi bulundu: ");
    Serial.println(saved_ssid);
    connectToWiFi();
  } else {
    // Hotspot moduna geç
    Serial.println("Kayıtlı Wi-Fi bulunamadı. Hotspot modu başlatılıyor...");
    startHotspot();
  }
}

void loop() {
  if (isHotspotMode) {
    // --- EKLENDİ: DNS isteklerini işle (Captive Portal için kritik) ---
    dnsServer.processNextRequest();
    // ----------------------------------------------------------------
    
    // Hotspot modunda HTTP server'ı yönet
    server.handleClient();

    // UDP paketlerini dinle (birden fazla paket olabilir, hepsini işle)
    while (true) {
      int packetSize = Udp.parsePacket();
      if (packetSize <= 0) break;

      int len = packetSize;
      if (len > 254) len = 254;
      char packet[255];
      int read = Udp.read(packet, len);
      if (read > 0) {
        packet[read] = 0;
        if (strstr(packet, "AMBLIGHT_DISCOVERY") != NULL) {
          IPAddress remoteIP = Udp.remoteIP();
          Udp.beginPacket(remoteIP, Udp.remotePort());
          Udp.write("AMBLIGHT_RESPONSE:");
          Udp.write(WiFi.softAPIP().toString().c_str());
          Udp.endPacket();
        }
      }
    }
  } else {
    // OTA güncellemelerini kontrol et
    ArduinoOTA.handle();

    // Normal modda WiFi reconnect yonetimi
    // reconnectCount ve lastReconnectAttempt artık global (connectToWiFi'tan sıfırlanır)

    if (WiFi.status() != WL_CONNECTED) {
      unsigned long now = millis();
      if (now - lastReconnectAttempt > 10000) {
        lastReconnectAttempt = now;
        reconnectCount++;
        Serial.print("Wi-Fi baglantisi kesildi! Yeniden baglaniyor... (deneme ");
        Serial.print(reconnectCount);
        Serial.println("/12)");
        server.stop();
        networkServicesNeedRestart = true;
        WiFi.disconnect();
        delay(100);
        WiFi.begin(saved_ssid.c_str(), saved_password.c_str());
        // WiFi kesildikten sonra Udp soketini de sıfırla
        Udp.stop();
        udpRunning = false;
      }
      if (reconnectCount >= 12) {
        Serial.println("12 deneme basarisiz. Hotspot moduna geciliyor...");
        reconnectCount = 0;
        startHotspot();
        return;
      }
    } else {
      reconnectCount = 0;
      // Bağlantı varsa ve Udp henüz çalışmıyorsa başlat
      if (!udpRunning) {
        if (Udp.begin(UDP_PORT)) {
          udpRunning = true;
          Serial.println("UDP soketi başlatıldı (Port 7777)");
        } else {
          Serial.println("Udp.begin() basarisiz! Hotspot moduna geciliyor...");
          startHotspot();
          return;
        }
      }
      if (networkServicesNeedRestart) {
        server.begin();
        ArduinoOTA.begin();
        networkServicesNeedRestart = false;
        Serial.println("HTTP ve OTA servisleri yeniden baslatildi");
      }
    }

    // Normal mod: HTTP isteklerini de işle
    server.handleClient();

    // UDP paketlerini dinle (birden fazla paket olabilir, hepsini işle)
    while (true) {
      int packetSize = Udp.parsePacket();
      if (packetSize <= 0) break;

      int len = packetSize;
      if (len > 512) len = 512;

      char packetBuffer[513];
      int read = Udp.read(packetBuffer, len);

      if (read > 0) {
        lastUdpActivityTime = millis();
        packetBuffer[read] = 0;

        // 1. Discovery Kontrolü
        if (strstr(packetBuffer, "AMBLIGHT_DISCOVERY") != NULL) {
          IPAddress remoteIP = Udp.remoteIP();
          int remotePort = Udp.remotePort();

          Udp.beginPacket(remoteIP, remotePort);
          Udp.write("AMBLIGHT_RESPONSE:");
          Udp.write(WiFi.localIP().toString().c_str());
          Udp.endPacket();

          Serial.print("Discovery isteği geldi: ");
          Serial.print(remoteIP);
          Serial.print(" Port: ");
          Serial.println(remotePort);
        }
        // 2. PING Kontrolü - Öncelikli işleme
        else if (strcmp(packetBuffer, "PING") == 0) {
          Udp.beginPacket(Udp.remoteIP(), Udp.remotePort());
          Udp.write("PONG");
          Udp.endPacket();
          Serial.println("PING yanıtlandı");
        }
        // 3. LED Verisi - Sadece PING değilse
        else if (!isSleepMode && len >= 3 && len % 3 == 0) {
          int numLeds = len / 3;
          if(numLeds > LED_COUNT) numLeds = LED_COUNT;

          for (int i = 0; i < numLeds; i++) {
            uint8_t r = packetBuffer[i * 3 + 0];
            uint8_t g = packetBuffer[i * 3 + 1];
            uint8_t b = packetBuffer[i * 3 + 2];
            strip.setPixelColor(i, strip.Color(r, g, b));
          }
          strip.show();
          lastDataTime = millis();
          receivingData = true;
          hasReceivedLedData = true;
          dataRecoveryStage = 0;
          // Serial.println("LED verisi alındı"); // Performans için kaldırıldı (60fps bloklama önleme)
        } else {
          Serial.print("Tanınmayan paket, len:");
          Serial.println(len);
        }
      }
    }

    if (isSleepMode) {
      delay(10);
      return;
    }

    // Daha önce veri akışı varken bağlantı sessizce takılırsa kademeli kurtarma uygula.
    // İlk açılışta veya PC uygulaması hiç bağlanmamışsa bu blok çalışmaz.
    if (hasReceivedLedData) {
      unsigned long dataAge = millis() - lastDataTime;

      if (dataAge >= RESTART_RECOVERY_DELAY_MS && dataRecoveryStage == 2) {
        Serial.println("LED verisi 60 saniyedir yok. Wemos bir kez yeniden baslatiliyor...");
        delay(100);
        ESP.restart();
      } else if (dataAge >= WIFI_RECOVERY_DELAY_MS && dataRecoveryStage == 1) {
        Serial.println("LED verisi 20 saniyedir yok. Wi-Fi baglantisi yenileniyor...");
        Udp.stop();
        udpRunning = false;
        server.stop();
        networkServicesNeedRestart = true;
        WiFi.disconnect();
        delay(100);
        WiFi.begin(saved_ssid.c_str(), saved_password.c_str());
        wifiRecoveryCount++;
        lastReconnectAttempt = millis();
        dataRecoveryStage = 2;
      } else if (dataAge >= UDP_RECOVERY_DELAY_MS && dataRecoveryStage == 0) {
        restartUdpListener("5 saniyedir LED verisi yok");
        dataRecoveryStage = 1;
      }
    }

    if (millis() - lastDataTime > 500) {
      receivingData = false;
      if (millis() - lastAnimUpdate > 50) {  // 80ms yerine 50ms - daha duyarlı animasyon
        lastAnimUpdate = millis();
        strip.clear();
        uint8_t brightness[] = {64, 45, 30, 18, 9, 3};

        for (int t = 0; t < 6; t++) {
          int pos = (idleAnimPos - t + LED_COUNT) % LED_COUNT;
          uint8_t r = brightness[t];
          uint8_t g = (uint8_t)(brightness[t] * 0.65);
          strip.setPixelColor(pos, strip.Color(r, g, 0));
        }
        strip.show();
        idleAnimPos = (idleAnimPos + 1) % LED_COUNT;
      }
    }
  }
  yield(); // ESP8266 Wi-Fi stack'ine işlem zamanı ver
}
