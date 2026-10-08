# Windows optimizasyonu — 8 Ekim 2026

Dal: `enes`. Değişiklikler yerel çalışma ağacındadır; commit/push yapılmadı.

## Değişiklik ve gerekçe

- Tekli kare yolunda monitör seçimi her karede JSON okuyordu. Worker artık
  bellekte tuttuğu monitör indeksini kullanıyor; 3 saniyelik ayar yenilemesi korunuyor.
- Tam ekran kontrolü her pencere için yeniden MSS ve yapılandırma açıyordu.
  Worker'ın monitör geometrisi artık doğrudan bu kontrole aktarılıyor.
- Çoklu mod kapalıyken 3 saniyede bir gereksiz gölge kare üretiliyordu; kaldırıldı.
- Çoklu modda kullanılmayan tekli kare kaldırıldı. Aynı monitörün görüntüsü yalnız
  o döngü içinde paylaşılır; sonraki karede yeniden yakalanır.
- Kenar planları ve sabit RGB paketleri sınırlı önbellekte tutuluyor. Kenar
  renkleri toplu tamsayı toplamlarıyla hesaplanıyor; LED sırası değişmedi.
- 60 FPS hedefi korundu. Zamanlama monoton saat kullanıyor; aşırı yükte yoğun
  telafi döngüsü oluşmuyor. Zamanlayıcı kaynağı worker sonunda serbest bırakılıyor.
- Tek uygulama kilidi, yalnız sahip olunan backend PID'sine yönelik asenkron
  kapatma ve gizli arayüzde durdurulan, çakışmayan durum/log sorguları eklendi.
- Linux kod dalları, binary, derleme betiği ve paket hedefleri kaldırıldı.
  Windows paketi artık Linux binary'si veya sistem Python fallback'i taşımıyor.

Windows ağ, tarama, bağlantı kontrolü, yeniden bağlanma ve cihaz doğrulama
işleyişi korundu. Kaynak AST karşılaştırmasında Windows dalları aynı kaldı;
Wemos firmware dosyalarında değişiklik yok. Mevcut cihaz doğrulamasının worker
içindeki yerleşimi de bağlantı kapsamını değiştirmemek için korundu.

## Program içi doğrulama

- `python -m unittest discover -s tests -v`: 31/31 başarılı.
- `node --test tests/electron.test.js tests/monitors.test.js`: 13/13 başarılı.
- Yapay görüntülerde önceki renk algoritmasıyla 24 boyut/LED/ofset birleşimi
  birebir karşılaştırıldı. Boş kenarlar ve pikselden fazla LED sayısı kapsandı.
- Sahte saat/ağ/ekran ile worker paket sayısı, hedefi, renkleri, 60 FPS bekleme
  süresi, ayar yenileme, çoklu yakalama paylaşımı ve sabit mod doğrulandı.
- API testleri gerçek HTTP sunucusu açmaz; handler'ları bellek üzerinden çağırır.
  Test ayarları ve logları `build/tests` altındaki geçici klasörlerde tutulur.
- Electron testleri VM ve sahte süreçlerle çalışır; gerçek Electron veya backend
  başlatılmaz. Sözdizimi kontrolleri ve `git diff --check` başarılıdır.

## Ek optimizasyonlar

- Monitör kataloğu başlangıçta yüklenir. `/api/status` önbelleği döndürür;
  monitör taraması veya yapılandırma okuması yapmaz. Her iki monitör seçicisinin
  yanındaki **Güncelle** düğmesi `POST /api/monitors/refresh` çağrısıyla listeyi
  yeniler. Otomatik aralıkla tarama yoktur; hata durumunda önceki liste korunur.
- Liste sürümü değişmedikçe seçim kutuları yeniden oluşturulmaz. Düğmeler
  işlem boyunca pasiftir; eski bir durum yanıtı yeni listeyi geri alamaz.
- Başarılı manuel yenileme sonrası yakalama nesnesi kendi worker iş parçacığında
  yeniden oluşturulur. Yeniden oluşturma başarısızsa worker mevcut nesneyle
  devam eder; her karede tekrar denemez.
- Tekli ve çoklu ekran RGB paketleri NumPy dizisinden doğrudan üretilir.
  Tüm 0–255 kanal ve parlaklık değerlerinde önceki kesme davranışıyla byte
  eşitliği doğrulandı; UDP biçimi ve gönderim sıklığı değişmedi.
- Kenar yakalama için 18 yapay görüntü senaryosu eşleşti. Üretim yakalama
  yolu değiştirilmedi; ayrıntılar `EDGE_CAPTURE_RESEARCH.md` dosyasındadır.

## Windows paketi

- Dosya: `dist/LuxEdge Setup 1.6.4.exe` — 100.671.204 bayt.
- Kurulum SHA256: `88dbacef5bad263a612d44eefe917b1a63f3d3980bf1856ae77d2698724b6dbf`
- Backend SHA256: `85304a6e96e219b794bef8ed7f8fa3c75fc86056d5bb8a7b48ca83685fe4d061`
- Backend'in proje, derleme, unpacked paket ve kurulum arşivinden çıkartılan
  kopyaları aynı. Python bytecode'u güncel kaynaklarla karşılaştırıldı.
- Kurulumdan okunan ASAR içindeki ana süreç, preload, arayüz ve polling dosyaları
  kaynaklarla aynı. Paket kaynakları arasında Linux binary'si yok.
- Python 3.10.11, PyInstaller 6.21.0, Electron 28.3.3 ve electron-builder 24.13.3
  kullanıldı. Eski `.venv` çalışmadığından `build/windows-venv` oluşturuldu;
  mevcut kurulu Python bağımlılıkları kullanıldı, sistem paketleri değiştirilmedi.
- Tekrar derleme: `scripts/build-windows.ps1`. Paket kontrol yardımcıları:
  `scripts/verify-package.py` ve `scripts/verify-asar.js`.

Gerçek ekran yakalama, Wemos/ağ, oyun, canlı performans ve kurulum testi yapılmadı.
Windows takılmasının giderildiği veya gerçek CPU/GPU yükünün azaldığı bu aşamada
doğrulanmış değildir. Bunlar sonraki canlı test aşamasında değerlendirilecektir.
