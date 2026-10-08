# Yalnız kenar yakalama araştırması

8 Ekim 2026. Gerçek ekran, Wemos veya canlı performans testi yapılmadı.
Üretim yakalama yolu değiştirilmedi.

## İncelenen uygulama

Yerel ortamda kullanılan MSS 10.2.0 Windows GDI uygulaması incelendi:
`mss/windows/gdi.py`, `grab` içindeki 350–390. satırlar.
Her yakalama `BitBlt`, `GdiFlush` ve BGRA tamponundan `bytearray` oluşturmayı
içeriyor. Genişlik/yükseklik değiştiğinde `DeleteObject` ve `CreateDIBSection`
ile görüntü tamponu yeniden oluşturuluyor. Bu çıkarım doğrudan kurulu sürümün
kaynak koduna dayanır; Windows üzerinde ölçülen bir süre değildir.

## Potansiyel ve maliyet

Örnek: 3840×2160 ekran, 10 piksel kalınlık, üst + sol + sağ kenar (alt kapalı).
Tam ekran BGRA boyutu kare başına 33.177.600 bayttır. Üç ayrı kenar için toplam
326.400 bayt yeterlidir: geometrik veri hacmi yaklaşık %99 azalır.
Bu, CPU/GPU kullanımının veya gecikmenin %99 azalacağı anlamına gelmez.

- Tek çağrı yerine kare başına üç GDI yakalama ve flush gerekir.
- Aynı MSS nesnesinde dikey/yatay bölge boyutları arasında geçiş yapmak
  tekrarlanan tampon oluşturma/silme maliyeti getirir.
- Ayrı kenarlara ayrı, kalıcı MSS nesneleri bu boyut değişimini önleyebilir;
  karşılığında daha fazla GDI kaynağı tutulur ve yaşam döngüsü yönetilir.
- Ayrı yakalamalar farklı anlara denk gelebilir. Hızlı hareketlerde kenarlar
  aynı ekran karesini temsil etmeyebilir.
- Bir monitörü farklı kenar/ofset düzenleriyle kullanan cihazlar için ortak
  bölge planı gerekir; cihaz başına bağımsız yakalama tekrar yaratabilir.

## Yapay görüntü doğrulaması ve karar

`tests/test_edge_capture_research.py`: iki boyut, üç LED düzeni ve üç
kalınlık/ofset birleşiminde (18 senaryo) ayrı kenar bölgelerinden üretilen
RGB paketleri tam görüntü yöntemiyle birebir eşleşti. Bu, sabit kaynak görüntüde
geometri ve renk hesabını doğrular; gerçek GDI hızı veya zaman uyumunu doğrulamaz.

Şimdilik tam ekran yakalama korunur. Sonraki canlı test aşamasında aynı 60 FPS,
ekran ve içerikle tam ekran ile kalıcı kenar yakalayıcıları karşılaştırılmalıdır:
kare süresi p95/p99, süreç CPU/belleği, GDI nesne sayısı ve hareketli içerikte
LED tutarlılığı. Avantaj ölçülmeden yeni yöntem varsayılan yapılmamalıdır.
