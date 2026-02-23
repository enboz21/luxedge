#include <ESP8266WiFi.h>
#include <WiFiUdp.h>
#include <ESP8266WebServer.h>
#include <Adafruit_NeoPixel.h>
#include <EEPROM.h>
#include <DNSServer.h> // --- EKLENDİ: Captive Portal için gerekli kütüphane ---

#define LED_PIN   4        // ESP-01 GPIO2
#define LED_COUNT 74       // 34 + 0 + 20 + 20 (config ile eşleşmeli)
#define UDP_PORT  7777
#define DNS_PORT  53       // --- EKLENDİ: DNS Portu ---

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

bool isHotspotMode = false;

// Bekleme animasyonu değişkenleri
unsigned long lastDataTime = 0;      // Son LED verisi zamanı
unsigned long lastAnimUpdate = 0;    // Son animasyon güncellemesi
int idleAnimPos = 0;                 // Halka pozisyonu
bool receivingData = false;          // PC'den veri geliyor mu?
bool isSleepMode = false;            // Uyku modu (LED'ler kapalı)

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
  
  isHotspotMode = true;
  
  // LED'i mavi yap (hotspot modu)
  for (int i = 0; i < LED_COUNT; i++) {
    strip.setPixelColor(i, strip.Color(0, 0, 255));
  }
  strip.show();
}

void handleRoot() {
  // Ağları tara
  int n = WiFi.scanNetworks();
  
  String html = "<!DOCTYPE html><html><head><meta charset='UTF-8'><meta name='viewport' content='width=device-width, initial-scale=1'>";
  html += "<style>body{font-family:sans-serif;padding:20px;text-align:center;background:#f0f2f5}form{background:white;padding:20px;border-radius:8px;box-shadow:0 2px 4px rgba(0,0,0,0.1);max-width:400px;margin:0 auto}h1{color:#333}select,input{padding:12px;margin:10px 0;width:100%;box-sizing:border-box;border:1px solid #ddd;border-radius:4px}button{padding:12px 20px;background:#007bff;color:white;border:none;border-radius:4px;cursor:pointer;width:100%;font-size:16px}button:hover{background:#0056b3}</style>";
  html += "<title>Wemos Kurulum</title></head><body>";
  html += "<br><h1>🌐 Wi-Fi Agini Sec</h1>";
  html += "<form action='/wifi_config' method='POST'>";
  
  html += "<label>Mevcut Aglar (" + String(n) + " bulundu):</label><br>";
  html += "<select name='ssid'>";
  
  if (n == 0) {
    html += "<option>Ag bulunamadi</option>";
  } else {
    for (int i = 0; i < n; ++i) {
      String ssid = WiFi.SSID(i);
      int rssi = WiFi.RSSI(i);
      String enc = (WiFi.encryptionType(i) == ENC_TYPE_NONE) ? " " : "🔒";
      html += "<option value='" + ssid + "'>" + ssid + " (" + rssi + "dBm) " + enc + "</option>";
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
      server.send(200, "text/plain", "OK:WiFi bilgileri kaydedildi. Yeniden başlatılıyor...");
      
      delay(1000);
      
      // Wi-Fi'ye bağlan (Önce hotspot'ı ve DNS'i durdurmak iyi fikirdir ama reset atacağız genelde)
      connectToWiFi();
    } else {
      server.send(400, "text/plain", "ERROR:SSID boş olamaz");
    }
  } else {
    server.send(405, "text/plain", "ERROR:Method not allowed");
  }
}

void handleStatus() {
  String status = "{\"mode\":\"";
  status += isHotspotMode ? "hotspot" : "wifi";
  status += "\",\"ip\":\"";
  status += isHotspotMode ? WiFi.softAPIP().toString() : WiFi.localIP().toString();
  status += "\",\"led_count\":";
  status += String(LED_COUNT);
  status += ",\"sleep_mode\":";
  status += isSleepMode ? "true" : "false";
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
  delay(100);
  
  // 1. Wi-Fi bağlantısını kes ve kayıtlı ağları UNUT (Flash'tan siler)
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
  Serial.print("Wi-Fi'ye bağlanılıyor: ");
  Serial.println(saved_ssid);
  
  // --- EKLENDİ: Eski hotspot ve DNS ayarlarını temizle ---
  WiFi.softAPdisconnect(true);
  WiFi.enableAP(false);
  // -----------------------------------------------------

  WiFi.mode(WIFI_STA);
  WiFi.begin(saved_ssid.c_str(), saved_password.c_str());
  
  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 30) {
    delay(500);
    Serial.print(".");
    attempts++;
    
    // LED'i sarı yap (bağlanıyor)
    for (int i = 0; i < LED_COUNT; i++) {
      strip.setPixelColor(i, strip.Color(255, 255, 0));
    }
    strip.show();
  }
  
  if (WiFi.status() == WL_CONNECTED) {
    Serial.println();
    Serial.println("WiFi bağlandı!");
    Serial.print("IP adresi: ");
    Serial.println(WiFi.localIP());
    
    isHotspotMode = false;
    
    // LED'i yeşil yap (bağlandı)
    for (int i = 0; i < LED_COUNT; i++) {
      strip.setPixelColor(i, strip.Color(0, 255, 0));
    }
    strip.show();
    delay(1000);
    
    // LED'leri kapat
    strip.clear();
    strip.show();
    
    // UDP dinlemeye başla
    Udp.begin(UDP_PORT);
    
    // HTTP sunucusunu normal modda da başlat
    server.on("/", handleRoot);
    server.on("/wifi_config", handleWiFiConfig);
    server.on("/status", handleStatus);
    server.on("/restart", handleRestart);
    server.on("/toggle_sleep", handleToggleSleep);
    server.on("/reset_wifi", handleResetWifi);
    server.begin();
    
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
    
    // UDP paketlerini de dinle (hotspot modunda)
    int packetSize = Udp.parsePacket();
    if (packetSize > 0) {
      char packet[255];
      int len = Udp.read(packet, 255);
      if (len > 0) {
        packet[len] = 0;
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
    // Normal mod: HTTP isteklerini de işle
    server.handleClient();
    
    // UDP paketlerini dinle
    int packetSize = Udp.parsePacket();
    if (packetSize > 0) {
      char packetBuffer[255];
      int len = Udp.read(packetBuffer, 254);
      
      if (len > 0) {
        packetBuffer[len] = 0;
      }

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
      // 2. PING Kontrolü
      else if (strcmp(packetBuffer, "PING") == 0) {
        Udp.beginPacket(Udp.remoteIP(), Udp.remotePort());
        Udp.write("PONG");
        Udp.endPacket();
      }
      // 3. LED Verisi
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
      }
    }

    if (isSleepMode) {
      delay(10);
      return;
    }
    
    if (millis() - lastDataTime > 500) {
      receivingData = false;
      if (millis() - lastAnimUpdate > 80) {
        lastAnimUpdate = millis();
        strip.clear();
        int trailLengths[] = {6, 5, 4, 3, 2, 1};
        uint8_t brightness[] = {255, 180, 120, 70, 35, 10};
        
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
    
    if (WiFi.status() != WL_CONNECTED) {
      Serial.println("Wi-Fi bağlantısı kesildi! Hotspot moduna dönülüyor...");
      delay(1000);
      startHotspot();
    }
  }
}