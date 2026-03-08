#!/bin/bash

echo "==============================================="
echo " LuxEdge - Linux Derleme Betiği"
echo "==============================================="

# Paketleri kontrol et
PYINSTALLER=""
if [ -f ".venv/bin/pyinstaller" ]; then
    PYINSTALLER=".venv/bin/pyinstaller"
elif command -v pyinstaller &> /dev/null; then
    PYINSTALLER="pyinstaller"
else
    echo "[!] PyInstaller bulunamadı."
    echo "Sisteminizde sanal ortam (.venv) içerisine pyinstaller kurunuz:"
    echo "Örn: .venv/bin/python -m pip install pyinstaller"
    exit 1
fi

echo "[1/3] Python backend ('lush_backend') derleniyor..."
$PYINSTALLER --onefile --noconsole --name lush_backend ambilight_pc.py

if [ ! -f "dist/lush_backend" ]; then
    echo "[!] Backend derlenirken hata oluştu!"
    exit 1
fi

echo "[2/3] Derlenen backend ana dizine kopyalanıyor..."
cp dist/lush_backend .

echo "[3/3] Electron uygulaması paketleniyor..."
npm run dist

echo "==============================================="
echo "✅ Derleme tamamlandı!"
echo "Çıktılar 'dist/' klasöründe bulunabilir."
echo "==============================================="
