#include <ESP8266WiFi.h>
#include <WiFiUdp.h>
#include <ESP8266WebServer.h>
#include <Adafruit_NeoPixel.h>
#include <EEPROM.h>
#include <DNSServer.h> // Captive Portal için gerekli kütüphane
#include <ArduinoOTA.h>
#include <ESP8266HTTPUpdateServer.h>

#define FIRMWARE_VERSION "1.7.0"
#define UDP_PORT  7777
#define DNS_PORT  53
#define STATUS_BRIGHTNESS 64 // Sistem animasyonları: yaklaşık %25 güç
#define MAX_LED_COUNT 170  // UDP paket tamponu en fazla 170 RGB LED destekler

// Hotspot ayarları
const char* ap_ssid = "Wemos_Setup";
const char* ap_password = "";  // Şifresiz hotspot

// Wi-Fi bilgileri (EEPROM'dan okunacak)
String saved_ssid = "";
String saved_password = "";
const uint8_t STRIP_COUNT = 4;
uint16_t stripLedCounts[STRIP_COUNT] = {0, 0, 0, 0};
uint8_t stripPins[STRIP_COUNT] = {0, 0, 0, 0};
uint16_t totalLedCount = 0;
bool ledConfigValid = false;

// ------------------------------------------------------------------
// Şerit (segment) modları
// Her fiziksel şerit bağımsız bir görev alabilir:
//   STREAM  : PC'den gelen UDP RGB akışını gösterir (ambilight / ekran kenarı)
//   OFF     : Kapalı
//   SOLID   : Sabit renk
//   BREATHE : Sabit renkte nefes alma
//   RAINBOW : Gökkuşağı kayması
//   CHASE   : Kuyruklu nokta (kovalama)
//   TWINKLE : Rastgele parıltı
// STREAM dışındaki modlar tamamen cihaz üzerinde çalışır; PC kapalıyken de sürer.
// ------------------------------------------------------------------
enum SegmentMode : uint8_t {
  SEG_STREAM = 0,
  SEG_OFF = 1,
  SEG_SOLID = 2,
  SEG_BREATHE = 3,
  SEG_RAINBOW = 4,
  SEG_CHASE = 5,
  SEG_TWINKLE = 6,
  SEG_MODE_COUNT = 7
};

struct SegmentConfig {
  uint8_t mode;
  uint8_t r;
  uint8_t g;
  uint8_t b;
  uint8_t brightness; // 0-255
  uint8_t speed;      // 1-100
};

SegmentConfig segments[STRIP_COUNT];
bool segmentsDirty = true;                 // Yerel modlar yeniden çizilmeli
unsigned long lastSegmentTick = 0;          // Son animasyon adımı
uint8_t twinkleLevel[MAX_LED_COUNT];        // TWINKLE: LED başına parlaklık (global ofsetle)
uint16_t chasePosition[STRIP_COUNT] = {0, 0, 0, 0};
unsigned long chaseLastStep[STRIP_COUNT] = {0, 0, 0, 0};
uint16_t rainbowHue[STRIP_COUNT] = {0, 0, 0, 0};
const unsigned long SEGMENT_TICK_MS = 20;   // 50 Hz animasyon

WiFiUDP Udp;
ESP8266WebServer server(80);
DNSServer dnsServer;
// Seritler, hotspot kurulumunda secilen degerler okunana kadar bos baslatilir.
Adafruit_NeoPixel strip1(0, 0, NEO_GRB + NEO_KHZ800);
Adafruit_NeoPixel strip2(0, 0, NEO_GRB + NEO_KHZ800);
Adafruit_NeoPixel strip3(0, 0, NEO_GRB + NEO_KHZ800);
Adafruit_NeoPixel strip4(0, 0, NEO_GRB + NEO_KHZ800);
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
// 0..63    SSID
// 64..127  Şifre
// 128..139 Şerit LED sayısı + pin (4 x 3 bayt)
// 140      Şerit yapılandırma imzası
// 141..164 Şerit mod ayarları (4 x 6 bayt)
// 165      Şerit mod imzası
#define EEPROM_SIZE 192
#define SSID_ADDR 0
#define PASSWORD_ADDR 64
#define STRIP_CONFIG_ADDR 128
#define STRIP_CONFIG_RECORD_SIZE 3
#define STRIP_CONFIG_MAGIC_ADDR 140
#define STRIP_CONFIG_MAGIC 0xB6
#define SEGMENT_CONFIG_ADDR 141
#define SEGMENT_CONFIG_RECORD_SIZE 6
#define SEGMENT_CONFIG_MAGIC_ADDR 165
#define SEGMENT_CONFIG_MAGIC 0xC7

// Fonksiyon prototipleri
void handleRoot();
void handleWiFiConfig();
void handleStatus();
void handleRestart();
void handleToggleSleep();
void handleResetWifi();
void handleSegmentsGet();
void handleSegmentsPost();
void connectToWiFi();
bool restartUdpListener(const char* reason);
bool isSupportedLedPin(uint8_t pin);
bool isValidLedCount(uint16_t count);
bool isValidStripConfiguration(const uint16_t counts[], const uint8_t pins[]);
bool configureStrips();
Adafruit_NeoPixel* getStrip(uint8_t index);
void clearAllStrips();
void clearStreamStrips();
void showAllStrips();
void fillAllStrips(uint32_t color);
void resetSegmentDefaults();
void loadSegmentConfig();
void saveSegmentConfig();
void renderSegments(unsigned long now, bool force);
uint16_t streamLedCount();
uint16_t stripGlobalOffset(uint8_t index);
const char* segmentModeName(uint8_t mode);
int segmentModeFromName(const String& name);
String segmentsJson();

uint32_t statusColor(uint8_t r, uint8_t g, uint8_t b) {
  uint8_t scaledR = (uint16_t)r * STATUS_BRIGHTNESS / 255;
  uint8_t scaledG = (uint16_t)g * STATUS_BRIGHTNESS / 255;
  uint8_t scaledB = (uint16_t)b * STATUS_BRIGHTNESS / 255;
  return strip1.Color(scaledR, scaledG, scaledB);
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

bool isSupportedLedPin(uint8_t pin) {
  return pin == D1 || pin == D2 || pin == D4 || pin == D5 || pin == D6 || pin == D7;
}

bool isValidLedCount(uint16_t count) {
  return count >= 1 && count <= MAX_LED_COUNT;
}

String ledPinName(uint8_t pin) {
  if (pin == D1) return "D1";
  if (pin == D2) return "D2";
  if (pin == D4) return "D4";
  if (pin == D5) return "D5";
  if (pin == D6) return "D6";
  if (pin == D7) return "D7";
  return "";
}

int pinFromName(const String& name) {
  if (name == "D1") return D1;
  if (name == "D2") return D2;
  if (name == "D4") return D4;
  if (name == "D5") return D5;
  if (name == "D6") return D6;
  if (name == "D7") return D7;
  return -1;
}

Adafruit_NeoPixel* getStrip(uint8_t index) {
  switch (index) {
    case 0: return &strip1;
    case 1: return &strip2;
    case 2: return &strip3;
    case 3: return &strip4;
    default: return NULL;
  }
}

bool isValidStripConfiguration(const uint16_t counts[], const uint8_t pins[]) {
  uint16_t total = 0;
  bool hasActiveStrip = false;

  for (uint8_t i = 0; i < STRIP_COUNT; i++) {
    bool hasCount = counts[i] > 0;
    bool hasPin = pins[i] > 0;
    if (hasCount != hasPin) return false;
    if (!hasCount) continue;
    if (!isValidLedCount(counts[i]) || !isSupportedLedPin(pins[i])) return false;

    for (uint8_t previous = 0; previous < i; previous++) {
      if (counts[previous] > 0 && pins[previous] == pins[i]) return false;
    }
    total += counts[i];
    if (total > MAX_LED_COUNT) return false;
    hasActiveStrip = true;
  }
  return hasActiveStrip;
}

// ------------------------------------------------------------------
// Şerit yardımcıları
// ------------------------------------------------------------------
bool isStreamStrip(uint8_t index) {
  return stripLedCounts[index] > 0 && segments[index].mode == SEG_STREAM;
}

uint16_t streamLedCount() {
  uint16_t total = 0;
  for (uint8_t i = 0; i < STRIP_COUNT; i++) {
    if (isStreamStrip(i)) total += stripLedCounts[i];
  }
  return total;
}

// Şeridin tüm zincir içindeki başlangıç ofseti (TWINKLE tamponu için).
uint16_t stripGlobalOffset(uint8_t index) {
  uint16_t offset = 0;
  for (uint8_t i = 0; i < index; i++) offset += stripLedCounts[i];
  return offset;
}

const char* segmentModeName(uint8_t mode) {
  switch (mode) {
    case SEG_STREAM: return "stream";
    case SEG_OFF: return "off";
    case SEG_SOLID: return "solid";
    case SEG_BREATHE: return "breathe";
    case SEG_RAINBOW: return "rainbow";
    case SEG_CHASE: return "chase";
    case SEG_TWINKLE: return "twinkle";
    default: return "stream";
  }
}

int segmentModeFromName(const String& name) {
  for (uint8_t mode = 0; mode < SEG_MODE_COUNT; mode++) {
    if (name == segmentModeName(mode)) return mode;
  }
  return -1;
}

void resetSegmentDefaults() {
  for (uint8_t i = 0; i < STRIP_COUNT; i++) {
    segments[i].mode = SEG_STREAM;
    segments[i].r = 255;
    segments[i].g = 120;
    segments[i].b = 0;
    segments[i].brightness = 128;
    segments[i].speed = 50;
  }
  segmentsDirty = true;
}

void clearAllStrips() {
  for (uint8_t i = 0; i < STRIP_COUNT; i++) {
    if (stripLedCounts[i] == 0) continue;
    Adafruit_NeoPixel* strip = getStrip(i);
    strip->clear();
    strip->show();
  }
  segmentsDirty = true;
}

// Yalnız STREAM modundaki şeritleri karartır; yerel animasyonlara dokunmaz.
void clearStreamStrips() {
  for (uint8_t i = 0; i < STRIP_COUNT; i++) {
    if (!isStreamStrip(i)) continue;
    Adafruit_NeoPixel* strip = getStrip(i);
    strip->clear();
    strip->show();
  }
}

void showAllStrips() {
  for (uint8_t i = 0; i < STRIP_COUNT; i++) {
    if (stripLedCounts[i] > 0) getStrip(i)->show();
  }
}

void fillAllStrips(uint32_t color) {
  for (uint8_t i = 0; i < STRIP_COUNT; i++) {
    if (stripLedCounts[i] == 0) continue;
    Adafruit_NeoPixel* strip = getStrip(i);
    for (uint16_t led = 0; led < stripLedCounts[i]; led++) {
      strip->setPixelColor(led, color);
    }
  }
  showAllStrips();
  segmentsDirty = true;
}

void showOtaProgress(unsigned int progress, unsigned int total) {
  for (uint8_t i = 0; i < STRIP_COUNT; i++) {
    if (stripLedCounts[i] == 0) continue;
    Adafruit_NeoPixel* strip = getStrip(i);
    uint16_t ledProgress = ((uint32_t)progress * stripLedCounts[i]) / total;
    for (uint16_t led = 0; led < stripLedCounts[i]; led++) {
      strip->setPixelColor(led, led <= ledProgress ? statusColor(128, 0, 255) : 0);
    }
  }
  showAllStrips();
  segmentsDirty = true;
}

bool configureStrips() {
  if (!ledConfigValid) return false;

  for (uint8_t i = 0; i < STRIP_COUNT; i++) {
    Adafruit_NeoPixel* strip = getStrip(i);
    strip->clear();
    strip->show();
    strip->updateLength(stripLedCounts[i]);
    if (stripLedCounts[i] == 0) continue;
    strip->setPin(stripPins[i]);
    strip->begin();
    strip->clear();
    strip->show();
  }
  for (uint16_t i = 0; i < MAX_LED_COUNT; i++) twinkleLevel[i] = 0;
  segmentsDirty = true;
  return true;
}

// ------------------------------------------------------------------
// Yerel animasyonlar (STREAM dışı modlar)
// ------------------------------------------------------------------
static inline uint8_t scale8(uint8_t value, uint8_t scale) {
  return (uint16_t)value * scale / 255;
}

uint32_t scaledColor(Adafruit_NeoPixel* strip, const SegmentConfig& seg, uint8_t extra) {
  uint8_t level = scale8(seg.brightness, extra);
  return strip->Color(scale8(seg.r, level), scale8(seg.g, level), scale8(seg.b, level));
}

void renderSolid(uint8_t index, Adafruit_NeoPixel* strip) {
  uint32_t color = scaledColor(strip, segments[index], 255);
  for (uint16_t led = 0; led < stripLedCounts[index]; led++) strip->setPixelColor(led, color);
}

void renderBreathe(uint8_t index, Adafruit_NeoPixel* strip, unsigned long now) {
  // speed 1 -> ~8.4 sn, speed 100 -> ~1.5 sn periyot
  unsigned long period = 1500UL + (100UL - segments[index].speed) * 70UL;
  float phase = (float)(now % period) / (float)period;           // 0..1
  float wave = 0.5f - 0.5f * cosf(phase * 6.2831853f);            // 0..1 (yumuşak)
  uint8_t level = 20 + (uint8_t)(wave * 235.0f);                  // tamamen sönmesin
  uint32_t color = scaledColor(strip, segments[index], level);
  for (uint16_t led = 0; led < stripLedCounts[index]; led++) strip->setPixelColor(led, color);
}

void renderRainbow(uint8_t index, Adafruit_NeoPixel* strip) {
  uint16_t count = stripLedCounts[index];
  uint16_t step = 65535 / count;
  for (uint16_t led = 0; led < count; led++) {
    uint16_t hue = rainbowHue[index] + led * step;
    strip->setPixelColor(led, strip->gamma32(strip->ColorHSV(hue, 255, segments[index].brightness)));
  }
  // speed 1 -> yavaş, speed 100 -> hızlı kayma
  rainbowHue[index] += (uint16_t)segments[index].speed * 6;
}

void renderChase(uint8_t index, Adafruit_NeoPixel* strip, unsigned long now) {
  uint16_t count = stripLedCounts[index];
  unsigned long interval = 130UL - segments[index].speed;          // 30..129 ms
  if (now - chaseLastStep[index] >= interval) {
    chaseLastStep[index] = now;
    chasePosition[index] = (chasePosition[index] + 1) % count;
  }
  strip->clear();
  const uint8_t tail[] = {255, 150, 90, 50, 25, 10};
  uint8_t tailLength = count < 6 ? count : 6;
  for (uint8_t t = 0; t < tailLength; t++) {
    uint16_t pos = (chasePosition[index] + count - t) % count;
    strip->setPixelColor(pos, scaledColor(strip, segments[index], tail[t]));
  }
}

void renderTwinkle(uint8_t index, Adafruit_NeoPixel* strip) {
  uint16_t count = stripLedCounts[index];
  uint16_t base = stripGlobalOffset(index);
  // speed: doğma olasılığı ve sönme hızı
  uint8_t decay = 4 + segments[index].speed / 8;                   // 4..16 / tick
  uint8_t spawnChance = 2 + segments[index].speed / 5;             // yüzde / tick
  for (uint16_t led = 0; led < count; led++) {
    uint8_t& level = twinkleLevel[base + led];
    if (level > decay) level -= decay; else level = 0;
    if (level == 0 && (uint8_t)random(100) < spawnChance) level = 255;
    strip->setPixelColor(led, scaledColor(strip, segments[index], level));
  }
}

// Yerel modlardaki şeritleri gerektiğinde çizer. STREAM şeritlerine dokunmaz.
void renderSegments(unsigned long now, bool force) {
  bool tick = (now - lastSegmentTick) >= SEGMENT_TICK_MS;
  if (!tick && !force && !segmentsDirty) return;
  if (tick) lastSegmentTick = now;

  for (uint8_t i = 0; i < STRIP_COUNT; i++) {
    if (stripLedCounts[i] == 0 || segments[i].mode == SEG_STREAM) continue;
    Adafruit_NeoPixel* strip = getStrip(i);
    bool animated = segments[i].mode == SEG_BREATHE || segments[i].mode == SEG_RAINBOW ||
                    segments[i].mode == SEG_CHASE || segments[i].mode == SEG_TWINKLE;
    if (!animated && !segmentsDirty && !force) continue;
    if (animated && !tick && !segmentsDirty && !force) continue;

    switch (segments[i].mode) {
      case SEG_OFF:     strip->clear(); break;
      case SEG_SOLID:   renderSolid(i, strip); break;
      case SEG_BREATHE: renderBreathe(i, strip, now); break;
      case SEG_RAINBOW: renderRainbow(i, strip); break;
      case SEG_CHASE:   renderChase(i, strip, now); break;
      case SEG_TWINKLE: renderTwinkle(i, strip); break;
      default: break;
    }
    strip->show();
  }
  segmentsDirty = false;
}

// ------------------------------------------------------------------
// EEPROM
// ------------------------------------------------------------------
void saveWiFiCredentials(String ssid, String password, const uint16_t selectedCounts[], const uint8_t selectedPins[]) {
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

  for (uint8_t i = 0; i < STRIP_COUNT; i++) {
    int address = STRIP_CONFIG_ADDR + (i * STRIP_CONFIG_RECORD_SIZE);
    EEPROM.write(address, selectedCounts[i] & 0xFF);
    EEPROM.write(address + 1, (selectedCounts[i] >> 8) & 0xFF);
    EEPROM.write(address + 2, selectedPins[i]);
  }
  EEPROM.write(STRIP_CONFIG_MAGIC_ADDR, STRIP_CONFIG_MAGIC);

  EEPROM.commit();
  EEPROM.end();
}

void saveSegmentConfig() {
  EEPROM.begin(EEPROM_SIZE);
  for (uint8_t i = 0; i < STRIP_COUNT; i++) {
    int address = SEGMENT_CONFIG_ADDR + (i * SEGMENT_CONFIG_RECORD_SIZE);
    EEPROM.write(address, segments[i].mode);
    EEPROM.write(address + 1, segments[i].r);
    EEPROM.write(address + 2, segments[i].g);
    EEPROM.write(address + 3, segments[i].b);
    EEPROM.write(address + 4, segments[i].brightness);
    EEPROM.write(address + 5, segments[i].speed);
  }
  EEPROM.write(SEGMENT_CONFIG_MAGIC_ADDR, SEGMENT_CONFIG_MAGIC);
  EEPROM.commit();
  EEPROM.end();
}

// EEPROM zaten açıkken çağrılır (loadWiFiCredentials içinden).
void loadSegmentConfig() {
  resetSegmentDefaults();
  if (EEPROM.read(SEGMENT_CONFIG_MAGIC_ADDR) != SEGMENT_CONFIG_MAGIC) return;
  for (uint8_t i = 0; i < STRIP_COUNT; i++) {
    int address = SEGMENT_CONFIG_ADDR + (i * SEGMENT_CONFIG_RECORD_SIZE);
    uint8_t mode = EEPROM.read(address);
    segments[i].mode = mode < SEG_MODE_COUNT ? mode : SEG_STREAM;
    segments[i].r = EEPROM.read(address + 1);
    segments[i].g = EEPROM.read(address + 2);
    segments[i].b = EEPROM.read(address + 3);
    segments[i].brightness = EEPROM.read(address + 4);
    uint8_t speed = EEPROM.read(address + 5);
    segments[i].speed = (speed >= 1 && speed <= 100) ? speed : 50;
  }
  segmentsDirty = true;
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

  uint16_t storedCounts[STRIP_COUNT] = {0, 0, 0, 0};
  uint8_t storedPins[STRIP_COUNT] = {0, 0, 0, 0};
  for (uint8_t i = 0; i < STRIP_COUNT; i++) {
    int address = STRIP_CONFIG_ADDR + (i * STRIP_CONFIG_RECORD_SIZE);
    storedCounts[i] = EEPROM.read(address);
    storedCounts[i] |= (uint16_t)EEPROM.read(address + 1) << 8;
    storedPins[i] = EEPROM.read(address + 2);
  }
  uint8_t storedMagic = EEPROM.read(STRIP_CONFIG_MAGIC_ADDR);
  ledConfigValid = storedMagic == STRIP_CONFIG_MAGIC &&
                   isValidStripConfiguration(storedCounts, storedPins);
  if (ledConfigValid) {
    totalLedCount = 0;
    for (uint8_t i = 0; i < STRIP_COUNT; i++) {
      stripLedCounts[i] = storedCounts[i];
      stripPins[i] = storedPins[i];
      totalLedCount += stripLedCounts[i];
    }
  } else {
    totalLedCount = 0;
    for (uint8_t i = 0; i < STRIP_COUNT; i++) {
      stripLedCounts[i] = 0;
      stripPins[i] = 0;
    }
  }

  loadSegmentConfig();

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

  // IP çakışmasını önlemek ve sabit IP vermek için
  WiFi.mode(WIFI_AP);
  WiFi.softAPConfig(IPAddress(192,168,4,1), IPAddress(192,168,4,1), IPAddress(255,255,255,0));
  WiFi.softAP(ap_ssid, ap_password);

  IPAddress IP = WiFi.softAPIP();
  Serial.print("Hotspot IP: ");
  Serial.println(IP);

  // DNS Sunucusunu başlat (Tüm (*) istekleri kendine yönlendir)
  dnsServer.setErrorReplyCode(DNSReplyCode::NoError);
  dnsServer.start(DNS_PORT, "*", IP);

  // HTTP server endpoint'leri
  server.on("/", handleRoot);
  server.on("/wifi_config", handleWiFiConfig);
  server.on("/status", handleStatus);
  // Android/iOS gibi cihazlar rastgele URL dener, onları da ana sayfaya atalım
  server.onNotFound(handleRoot);

  server.begin();
  Udp.begin(UDP_PORT);
  udpRunning = true;
  networkServicesNeedRestart = false;

  isHotspotMode = true;

  // Daha once geceri bir LED ayari varsa hotspot modunu mavi goster.
  if (ledConfigValid) fillAllStrips(statusColor(0, 0, 255));
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
  html += "<input type='password' name='password' placeholder='Sifre' required><br>";
  html += "<p style='color:#666;font-size:13px'>Kullanmayacaginiz seritleri bos birakin. Aktif seritlerin toplami en fazla " + String(MAX_LED_COUNT) + " LED olabilir. Her seridin gorevi (ambilight, sabit renk, animasyon) daha sonra LuxEdge uygulamasindan ayarlanir.</p>";
  for (uint8_t i = 0; i < STRIP_COUNT; i++) {
    String number = String(i + 1);
    html += "<h3 style='margin:18px 0 4px'>LED Seridi " + number + "</h3>";
    html += "<label>LED Sayisi:</label><br>";
    html += "<input type='number' name='strip_count_" + number + "' min='1' max='" + String(MAX_LED_COUNT) + "' step='1' placeholder='Kullanilmiyorsa bos birakin'><br>";
    html += "<label>Veri Pini:</label><br>";
    html += "<select name='strip_pin_" + number + "'>";
    html += "<option value='' selected>Kullanilmiyor</option>";
    html += "<option value='D1'>D1 (GPIO5)</option>";
    html += "<option value='D2'>D2 (GPIO4)</option>";
    html += "<option value='D4'>D4 (GPIO2)</option>";
    html += "<option value='D5'>D5 (GPIO14)</option>";
    html += "<option value='D6'>D6 (GPIO12)</option>";
    html += "<option value='D7'>D7 (GPIO13)</option>";
    html += "</select>";
  }
  html += "<br>";

  html += "<button type='submit'>Kaydet ve Baglan</button>";
  html += "</form>";
  html += "<p style='color:#666;font-size:12px;margin-top:20px'>LuxEdge Wemos v" FIRMWARE_VERSION "</p>";
  html += "</body></html>";

  server.send(200, "text/html", html);
}

void handleWiFiConfig() {
  if (server.method() == HTTP_POST) {
    String ssid = server.arg("ssid");
    String password = server.arg("password");
    uint16_t selectedCounts[STRIP_COUNT] = {0, 0, 0, 0};
    uint8_t selectedPins[STRIP_COUNT] = {0, 0, 0, 0};
    bool formValid = ssid.length() > 0;

    for (uint8_t i = 0; i < STRIP_COUNT; i++) {
      String number = String(i + 1);
      String countText = server.arg("strip_count_" + number);
      String pinText = server.arg("strip_pin_" + number);
      bool hasCount = countText.length() > 0;
      bool hasPin = pinText.length() > 0;

      if (hasCount != hasPin) {
        formValid = false;
        continue;
      }
      if (!hasCount) continue;

      long parsedCount = countText.toInt();
      int parsedPin = pinFromName(pinText);
      if (String(parsedCount) != countText || parsedCount < 1 ||
          parsedCount > MAX_LED_COUNT || parsedPin < 0) {
        formValid = false;
        continue;
      }
      selectedCounts[i] = (uint16_t)parsedCount;
      selectedPins[i] = (uint8_t)parsedPin;
    }

    formValid = formValid && isValidStripConfiguration(selectedCounts, selectedPins);
    if (formValid) {
      Serial.print("Wi-Fi bilgileri alındı - SSID: ");
      Serial.println(ssid);
      for (uint8_t i = 0; i < STRIP_COUNT; i++) {
        if (selectedCounts[i] == 0) continue;
        Serial.print("Serit ");
        Serial.print(i + 1);
        Serial.print(" - LED: ");
        Serial.print(selectedCounts[i]);
        Serial.print(", Pin: ");
        Serial.println(ledPinName(selectedPins[i]));
      }

      // Wi-Fi ve dort serit ayarini birlikte kaydet.
      saveWiFiCredentials(ssid, password, selectedCounts, selectedPins);
      saved_ssid = ssid;
      saved_password = password;
      totalLedCount = 0;
      for (uint8_t i = 0; i < STRIP_COUNT; i++) {
        stripLedCounts[i] = selectedCounts[i];
        stripPins[i] = selectedPins[i];
        totalLedCount += stripLedCounts[i];
      }
      ledConfigValid = true;
      configureStrips();

      // Yanıt gönder
      server.send(200, "text/plain", "OK:WiFi ve LED seridi ayarlari kaydedildi. Baglaniliyor...");

      delay(1000);

      // Wi-Fi'ye bağlan (temizlik connectToWiFi icinde yapiliyor)
      connectToWiFi();
    } else {
      server.send(400, "text/plain", "ERROR:WiFi adi ve en az bir tam LED seridi zorunludur. Her aktif seritte LED sayisi ile pin birlikte secilmeli, pinler benzersiz olmali ve toplam 170'i asmamalidir.");
    }
  } else {
    server.send(405, "text/plain", "ERROR:Method not allowed");
  }
}

String hexColor(uint8_t r, uint8_t g, uint8_t b) {
  char buffer[8];
  snprintf(buffer, sizeof(buffer), "#%02x%02x%02x", r, g, b);
  return String(buffer);
}

// Şerit listesini JSON dizisi olarak üretir ("strips" alanı).
String segmentsJson() {
  String json;
  json.reserve(STRIP_COUNT * 120);
  json = "[";
  for (uint8_t i = 0; i < STRIP_COUNT; i++) {
    if (i > 0) json += ",";
    json += "{\"index\":";
    json += String(i + 1);
    json += ",\"enabled\":";
    json += stripLedCounts[i] > 0 ? "true" : "false";
    json += ",\"led_count\":";
    json += String(stripLedCounts[i]);
    json += ",\"led_pin\":\"";
    if (stripLedCounts[i] > 0) json += ledPinName(stripPins[i]);
    json += "\",\"mode\":\"";
    json += segmentModeName(segments[i].mode);
    json += "\",\"color\":\"";
    json += hexColor(segments[i].r, segments[i].g, segments[i].b);
    json += "\",\"brightness\":";
    json += String(segments[i].brightness);
    json += ",\"speed\":";
    json += String(segments[i].speed);
    json += "}";
  }
  json += "]";
  return json;
}

void handleStatus() {
  lastHttpActivityTime = millis();
  long lastDataAgeMs = hasReceivedLedData ? (long)(millis() - lastDataTime) : -1;
  long lastUdpAgeMs = lastUdpActivityTime > 0 ? (long)(millis() - lastUdpActivityTime) : -1;

  String status;
  status.reserve(1280);
  status = "{\"mode\":\"";
  status += isHotspotMode ? "hotspot" : "wifi";
  status += "\",\"ip\":\"";
  status += isHotspotMode ? WiFi.softAPIP().toString() : WiFi.localIP().toString();
  status += "\",\"led_count\":";
  status += String(totalLedCount);
  status += ",\"stream_led_count\":";
  status += String(streamLedCount());
  status += ",\"segments_supported\":true";
  status += ",\"strips\":";
  status += segmentsJson();
  status += ",\"led_configured\":";
  status += ledConfigValid ? "true" : "false";
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

// GET /segments -> mevcut şerit ayarları
void handleSegmentsGet() {
  String json = "{\"led_count\":" + String(totalLedCount) +
                ",\"stream_led_count\":" + String(streamLedCount()) +
                ",\"strips\":" + segmentsJson() + "}";
  server.send(200, "application/json", json);
}

// 6 haneli hex rengi çözer; "#" isteğe bağlıdır.
bool parseHexColor(String text, uint8_t& r, uint8_t& g, uint8_t& b) {
  if (text.startsWith("#")) text = text.substring(1);
  if (text.length() != 6) return false;
  for (uint8_t i = 0; i < 6; i++) {
    if (!isHexadecimalDigit(text[i])) return false;
  }
  long value = strtol(text.c_str(), NULL, 16);
  r = (value >> 16) & 0xFF;
  g = (value >> 8) & 0xFF;
  b = value & 0xFF;
  return true;
}

// POST /segments
// Alanlar (her şerit için isteğe bağlı, N = 1..4):
//   sN_mode=stream|off|solid|breathe|rainbow|chase|twinkle
//   sN_color=rrggbb   sN_brightness=0-255   sN_speed=1-100
//   save=1 -> EEPROM'a kalıcı yaz (varsayılan: yalnız bellekte uygula)
void handleSegmentsPost() {
  if (server.method() != HTTP_POST) {
    server.send(405, "text/plain", "ERROR:Method not allowed");
    return;
  }

  SegmentConfig updated[STRIP_COUNT];
  for (uint8_t i = 0; i < STRIP_COUNT; i++) updated[i] = segments[i];
  bool changed = false;

  for (uint8_t i = 0; i < STRIP_COUNT; i++) {
    String prefix = "s" + String(i + 1) + "_";
    if (server.hasArg(prefix + "mode")) {
      int mode = segmentModeFromName(server.arg(prefix + "mode"));
      if (mode < 0) {
        server.send(400, "text/plain", "ERROR:Gecersiz mod (serit " + String(i + 1) + ")");
        return;
      }
      updated[i].mode = (uint8_t)mode;
      changed = true;
    }
    if (server.hasArg(prefix + "color")) {
      if (!parseHexColor(server.arg(prefix + "color"), updated[i].r, updated[i].g, updated[i].b)) {
        server.send(400, "text/plain", "ERROR:Gecersiz renk (serit " + String(i + 1) + ")");
        return;
      }
      changed = true;
    }
    if (server.hasArg(prefix + "brightness")) {
      long value = server.arg(prefix + "brightness").toInt();
      if (value < 0 || value > 255) {
        server.send(400, "text/plain", "ERROR:Parlaklik 0-255 olmali (serit " + String(i + 1) + ")");
        return;
      }
      updated[i].brightness = (uint8_t)value;
      changed = true;
    }
    if (server.hasArg(prefix + "speed")) {
      long value = server.arg(prefix + "speed").toInt();
      if (value < 1 || value > 100) {
        server.send(400, "text/plain", "ERROR:Hiz 1-100 olmali (serit " + String(i + 1) + ")");
        return;
      }
      updated[i].speed = (uint8_t)value;
      changed = true;
    }
  }

  if (changed) {
    for (uint8_t i = 0; i < STRIP_COUNT; i++) {
      bool modeChanged = updated[i].mode != segments[i].mode;
      segments[i] = updated[i];
      if (modeChanged && stripLedCounts[i] > 0) {
        // Moddan çıkan şeridi temizle ki eski desen asılı kalmasın.
        Adafruit_NeoPixel* strip = getStrip(i);
        strip->clear();
        strip->show();
        chasePosition[i] = 0;
        uint16_t base = stripGlobalOffset(i);
        for (uint16_t led = 0; led < stripLedCounts[i]; led++) twinkleLevel[base + led] = 0;
      }
    }
    segmentsDirty = true;
    if (server.arg("save") == "1") {
      saveSegmentConfig();
      Serial.println("Serit ayarlari EEPROM'a kaydedildi.");
    }
  }

  handleSegmentsGet();
}

void handleRestart() {
  server.send(200, "text/plain", "OK: Yeniden baslatiliyor...");
  delay(500);
  ESP.restart();
}

void handleToggleSleep() {
  isSleepMode = !isSleepMode;
  if (isSleepMode) {
    clearAllStrips();
    Serial.println("Uyku modu AKTIF: LED'ler kapatildi.");
    server.send(200, "text/plain", "SLEEP_ON");
  } else {
    segmentsDirty = true;
    Serial.println("Uyku modu KAPALI: Normal calisma.");
    server.send(200, "text/plain", "SLEEP_OFF");
  }
}

void handleResetWifi() {
  server.send(200, "text/plain", "OK: Wi-Fi ve LED ayarlari siliniyor ve reset atiliyor...");
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

  Serial.println("Wi-Fi ve LED ayarlari silindi. Cihaz yeniden baslatiliyor...");
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

  // Tum aktif seritleri sari yap (baglaniyor).
  fillAllStrips(statusColor(255, 255, 0));

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

    // Tum aktif seritleri yesil yap (baglandi).
    fillAllStrips(statusColor(0, 255, 0));
    delay(1000);

    // LED'leri kapat; yerel modlar loop() icinde yeniden cizilir.
    clearAllStrips();

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
    server.on("/segments", HTTP_GET, handleSegmentsGet);
    server.on("/segments", HTTP_POST, handleSegmentsPost);
    // OTA HTTP güncelleyici - /firmware adresinden erişilir
    httpUpdater.setup(&server, "/firmware");

    // ArduinoOTA ayarları
    ArduinoOTA.setHostname("luxedge-wemos");
    ArduinoOTA.onStart([]() {
      // OTA başladığında mor renk
      fillAllStrips(statusColor(128, 0, 255));
    });
    ArduinoOTA.onProgress([](unsigned int progress, unsigned int total) {
      // İlerleme göstergesi - mor dolum
      showOtaProgress(progress, total);
    });
    ArduinoOTA.onEnd([]() {
      // OTA tamamlandığında yeşil flaş
      fillAllStrips(statusColor(0, 255, 0));
    });
    ArduinoOTA.onError([](ota_error_t error) {
      // Hata durumunda kırmızı
      fillAllStrips(statusColor(255, 0, 0));
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
  Serial.println("=== LuxEdge Wemos v" FIRMWARE_VERSION " Başlatılıyor ===");
  randomSeed(ESP.getCycleCount());
  // EEPROM'dan Wi-Fi, LED ve şerit mod bilgilerini yükle
  loadWiFiCredentials();
  if (ledConfigValid) {
    configureStrips();
    Serial.print("Toplam LED sayisi: ");
    Serial.println(totalLedCount);
    for (uint8_t i = 0; i < STRIP_COUNT; i++) {
      if (stripLedCounts[i] == 0) continue;
      Serial.print("Serit ");
      Serial.print(i + 1);
      Serial.print(" - LED: ");
      Serial.print(stripLedCounts[i]);
      Serial.print(", Pin: ");
      Serial.print(ledPinName(stripPins[i]));
      Serial.print(", Mod: ");
      Serial.println(segmentModeName(segments[i].mode));
    }
  } else {
    Serial.println("LED yapilandirmasi bulunamadi. Hotspot kurulumu bekleniyor...");
  }

  // Wi-Fi ve LED ayarlari birlikte geçerliyse bağlan.
  if (saved_ssid.length() > 0 && ledConfigValid) {
    Serial.print("Kayıtlı Wi-Fi bulundu: ");
    Serial.println(saved_ssid);
    connectToWiFi();
  } else {
    // Eksik LED ayari veya Wi-Fi bilgisi için hotspot moduna geç.
    Serial.println("Kurulum bilgisi eksik. Hotspot modu baslatiliyor...");
    startHotspot();
  }
}

void loop() {
  if (isHotspotMode) {
    // DNS isteklerini işle (Captive Portal için kritik)
    dnsServer.processNextRequest();

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
        // 3. LED Verisi - Yalnız STREAM modundaki seritlere sirayla dagitilir
        else if (!isSleepMode && len >= 3 && len % 3 == 0) {
          int receivedLedCount = len / 3;
          uint16_t streamTotal = streamLedCount();
          if (receivedLedCount > streamTotal) receivedLedCount = streamTotal;
          int inputOffset = 0;

          for (uint8_t stripIndex = 0; stripIndex < STRIP_COUNT; stripIndex++) {
            if (!isStreamStrip(stripIndex) || inputOffset >= receivedLedCount) continue;
            Adafruit_NeoPixel* strip = getStrip(stripIndex);
            uint16_t ledsForStrip = stripLedCounts[stripIndex];
            if (ledsForStrip > receivedLedCount - inputOffset) {
              ledsForStrip = receivedLedCount - inputOffset;
            }

            for (uint16_t led = 0; led < ledsForStrip; led++) {
              int byteOffset = (inputOffset + led) * 3;
              uint8_t r = packetBuffer[byteOffset + 0];
              uint8_t g = packetBuffer[byteOffset + 1];
              uint8_t b = packetBuffer[byteOffset + 2];
              strip->setPixelColor(led, strip->Color(r, g, b));
            }
            inputOffset += ledsForStrip;
            strip->show();
          }
          lastDataTime = millis();
          receivingData = true;
          hasReceivedLedData = true;
          dataRecoveryStage = 0;
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

    unsigned long now = millis();

    // Yerel modlardaki seritleri cihaz uzerinde canlandir.
    renderSegments(now, false);

    // STREAM seridi yoksa veri akisi beklenmez; kurtarma ve bekleme animasyonu gereksiz.
    bool expectsStream = streamLedCount() > 0;

    // Daha önce veri akışı varken bağlantı sessizce takılırsa kademeli kurtarma uygula.
    // İlk açılışta veya PC uygulaması hiç bağlanmamışsa bu blok çalışmaz.
    if (hasReceivedLedData && expectsStream) {
      unsigned long dataAge = now - lastDataTime;

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

    // Bekleme animasyonu: yalniz STREAM seritlerinde ve veri yokken.
    if (expectsStream && now - lastDataTime > 500) {
      receivingData = false;
      if (now - lastAnimUpdate > 50) {
        lastAnimUpdate = now;
        uint8_t brightness[] = {64, 45, 30, 18, 9, 3};

        for (uint8_t stripIndex = 0; stripIndex < STRIP_COUNT; stripIndex++) {
          if (!isStreamStrip(stripIndex)) continue;
          Adafruit_NeoPixel* strip = getStrip(stripIndex);
          strip->clear();
          for (int t = 0; t < 6; t++) {
            int pos = ((idleAnimPos - t) % stripLedCounts[stripIndex] + stripLedCounts[stripIndex]) % stripLedCounts[stripIndex];
            uint8_t r = brightness[t];
            uint8_t g = (uint8_t)(brightness[t] * 0.65);
            strip->setPixelColor(pos, strip->Color(r, g, 0));
          }
          strip->show();
        }
        idleAnimPos = (idleAnimPos + 1) % MAX_LED_COUNT;
      }
    }
  }
  yield(); // ESP8266 Wi-Fi stack'ine işlem zamanı ver
}
