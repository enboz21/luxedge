"""Original color algorithm retained only as a synthetic-test oracle."""
import numpy as np

def grab_edge_colors(top_leds, bottom_leds, left_leds, right_leds, edge_width, edge_offset, sct, monitor_index=None):
    """
    Ekran kenarlarından renkleri toplar.
    v1.6.1: Tamamen numpy vektörizasyonu ile yeniden yazıldı.
    PIL.Image.crop() döngüsü kaldırıldı → ~3-4x daha hızlı.
    """
    if sct is None or not hasattr(sct, 'monitors'):
        sct = mss()
    led_idx = monitor_index if monitor_index is not None else get_led_monitor_index(sct)
    if not hasattr(sct, 'monitors') or led_idx >= len(sct.monitors) or led_idx <= 0:
        led_idx = get_primary_monitor_index(sct)
    monitor = sct.monitors[led_idx]
    screenshot = sct.grab(monitor)
    # mss'den direkt numpy array (BGRA) → RGB'ye çevir (kopyasız dönüşüm)
    arr = np.frombuffer(screenshot.raw, dtype=np.uint8).reshape(screenshot.height, screenshot.width, 4)[:, :, 2::-1]
    # arr shape: (H, W, 3) — RGB

    h, w = arr.shape[:2]
    final_colors = []

    # 1) RIGHT side (bottom → top)
    if right_leds > 0:
        strip = arr[:, max(0, w - edge_width - edge_offset):max(0, w - edge_offset) or w, :]  # (H, edge_width, 3)
        for i in range(right_leds):
            y1 = int((right_leds - 1 - i) * h / right_leds)
            y2 = int((right_leds - i) * h / right_leds)
            if y2 > y1:
                final_colors.append(tuple(strip[y1:y2].reshape(-1, 3).mean(axis=0).astype(int)))
            else:
                final_colors.append((0, 0, 0))

    # 2) TOP side (right → left)
    if top_leds > 0:
        strip = arr[edge_offset:edge_width + edge_offset, :, :]  # (edge_width, W, 3)
        for i in range(top_leds):
            x1 = int((top_leds - 1 - i) * w / top_leds)
            x2 = int((top_leds - i) * w / top_leds)
            if x2 > x1:
                final_colors.append(tuple(strip[:, x1:x2].reshape(-1, 3).mean(axis=0).astype(int)))
            else:
                final_colors.append((0, 0, 0))

    # 3) LEFT side (top → bottom)
    if left_leds > 0:
        strip = arr[:, edge_offset:edge_width + edge_offset, :]  # (H, edge_width, 3)
        for i in range(left_leds):
            y1 = int(i * h / left_leds)
            y2 = int((i + 1) * h / left_leds)
            if y2 > y1:
                final_colors.append(tuple(strip[y1:y2].reshape(-1, 3).mean(axis=0).astype(int)))
            else:
                final_colors.append((0, 0, 0))

    # 4) BOTTOM side (left → right)
    if bottom_leds > 0:
        strip = arr[max(0, h - edge_width - edge_offset):max(0, h - edge_offset) or h, :, :]  # (edge_width, W, 3)
        for i in range(bottom_leds):
            x1 = int(i * w / bottom_leds)
            x2 = int((i + 1) * w / bottom_leds)
            if x2 > x1:
                final_colors.append(tuple(strip[:, x1:x2].reshape(-1, 3).mean(axis=0).astype(int)))
            else:
                final_colors.append((0, 0, 0))

    return final_colors
