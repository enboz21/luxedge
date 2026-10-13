"""Şerit bazlı (strips) cihaz desteği: doğrulama, kare üretimi ve Wemos eşitleme."""
import io
import json
import tempfile
import types
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

import numpy as np

from backend_fixture import backend, TEST_ROOT
from frame_processing import edge_rgb, pack_rgb


class SyntheticCapture:
    def __init__(self, w=64, h=36):
        self.monitors = [{}, {'left': 0, 'top': 0, 'width': w, 'height': h},
                         {'left': w, 'top': 0, 'width': w, 'height': h}]
        self.raw = np.random.default_rng(7).integers(0, 256, (h, w, 4), dtype=np.uint8)
        self.image = types.SimpleNamespace(raw=self.raw.tobytes(), width=w, height=h)
        self.calls = []

    def grab(self, monitor):
        self.calls.append(monitor)
        return self.image


def strips_device(**overrides):
    device = {
        "id": "desk", "name": "Masa", "ip": "192.0.2.9", "port": 7777, "role": "strips",
        "monitor_index": 1, "leds": {"top": 0, "bottom": 0, "left": 0, "right": 0}, "enabled": True,
        "strips": [
            {"index": 1, "led_count": 5, "pin": "D1", "mode": "stream", "color": "#ff0000",
             "brightness": 255, "speed": 50, "monitor_index": 1, "edge": "top", "reverse": False},
            {"index": 2, "led_count": 4, "pin": "D2", "mode": "solid", "color": "#00ff00",
             "brightness": 100, "speed": 50, "monitor_index": 1, "edge": "top", "reverse": False},
            {"index": 3, "led_count": 3, "pin": "D5", "mode": "stream", "color": "#0000ff",
             "brightness": 255, "speed": 50, "monitor_index": 2, "edge": "left", "reverse": True},
        ],
    }
    device.update(overrides)
    return device


class StripValidationTests(unittest.TestCase):
    def test_strips_role_round_trips_through_device_validation(self):
        device = backend.validate_wemos_device(strips_device(), device_id="desk")
        self.assertEqual(device["role"], "strips")
        self.assertEqual([s["index"] for s in device["strips"]], [1, 2, 3])
        self.assertEqual(device["strips"][2]["edge"], "left")
        self.assertTrue(device["strips"][2]["reverse"])
        self.assertEqual(backend.expected_stream_led_count(device), 8)

    def test_invalid_strip_settings_are_rejected(self):
        for field, value in (("mode", "disco"), ("edge", "middle"), ("color", "#12345"),
                             ("brightness", 300), ("speed", 0), ("index", 5)):
            payload = strips_device()
            payload["strips"][0][field] = value
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    backend.validate_wemos_device(payload)
        with self.assertRaises(ValueError):
            backend.validate_wemos_device(strips_device(role="disco"))
        duplicate = strips_device()
        duplicate["strips"][1]["index"] = 1
        with self.assertRaises(ValueError):
            backend.validate_wemos_device(duplicate)

    def test_ambilight_devices_still_validate_without_strips(self):
        device = backend.validate_wemos_device({"name": "Ekran", "ip": "192.0.2.5", "leds": {"top": 3}})
        self.assertEqual(device["role"], "ambilight")
        self.assertEqual(device["strips"], [])
        self.assertEqual(backend.expected_stream_led_count(device), 3)

    def test_validation_uses_firmware_stream_count_when_available(self):
        device = strips_device()
        new_firmware = {"ip": device["ip"], "led_count": 12, "stream_led_count": 8, "segments_supported": True,
                        "firmware_version": "1.7.0", "strips": []}
        result = backend.get_device_validation(device, status=new_firmware)
        self.assertTrue(result["compatible"])
        self.assertEqual(result["expected_led_count"], 8)
        self.assertEqual(result["firmware_total_led_count"], 12)
        self.assertTrue(result["segments_supported"])

        old_firmware = {"ip": device["ip"], "led_count": 12, "firmware_version": "1.6.4"}
        result = backend.get_device_validation(device, status=old_firmware)
        self.assertFalse(result["compatible"])
        self.assertIn("1.7.0", result["message"])

        ambilight = {"id": "a", "ip": "192.0.2.6", "leds": {"top": 12, "bottom": 0, "left": 0, "right": 0}}
        self.assertTrue(backend.get_device_validation(ambilight, status=old_firmware)["compatible"])
        self.assertFalse(backend.get_device_validation(ambilight, status=new_firmware)["compatible"])

    def test_firmware_strips_merge_keeps_user_settings_and_adopts_new_strips(self):
        device = strips_device()
        status = {"strips": [
            {"index": 1, "enabled": True, "led_count": 30, "led_pin": "D1"},
            {"index": 2, "enabled": False, "led_count": 0, "led_pin": ""},
            {"index": 4, "enabled": True, "led_count": 7, "led_pin": "D7", "mode": "rainbow",
             "color": "#123456", "brightness": 10, "speed": 90},
        ]}
        merged = backend.merge_firmware_strips(device, status)
        self.assertEqual([s["index"] for s in merged["strips"]], [1, 4])
        self.assertEqual(merged["strips"][0]["led_count"], 30)
        self.assertEqual(merged["strips"][0]["mode"], "stream")
        self.assertEqual(merged["strips"][1]["mode"], "rainbow")
        self.assertEqual(merged["strips"][1]["color"], "#123456")
        self.assertEqual(device["strips"][0]["led_count"], 5)  # girdi değişmedi


class StripFrameTests(unittest.TestCase):
    def test_strips_frame_concatenates_stream_strips_in_firmware_order(self):
        capture = SyntheticCapture()
        device = strips_device()
        frame = backend.build_device_frame(device, 4, 1, capture)
        top = pack_rgb(edge_rgb(capture.image, 5, 0, 0, 0, 4, 1))
        left = pack_rgb(edge_rgb(capture.image, 0, 0, 3, 0, 4, 1))
        self.assertEqual(frame, top + backend.reverse_rgb_frame(left))
        self.assertEqual(len(frame), 8 * 3)
        self.assertEqual(capture.calls, [capture.monitors[1], capture.monitors[2]])

    def test_reverse_frame_flips_rgb_triplets(self):
        self.assertEqual(backend.reverse_rgb_frame(bytes([1, 2, 3, 4, 5, 6, 7, 8, 9])), bytes([7, 8, 9, 4, 5, 6, 1, 2, 3]))
        self.assertEqual(backend.reverse_rgb_frame(b""), b"")

    def test_device_without_stream_strips_produces_no_frame(self):
        device = strips_device()
        for strip in device["strips"]:
            strip["mode"] = "solid"
        self.assertEqual(backend.build_device_frame(device, 4, 1, SyntheticCapture()), b"")
        self.assertEqual(backend.expected_stream_led_count(device), 0)


class StripSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.config_path = Path(self.temp_dir.name) / "ambilight_config.json"
        patcher = mock.patch.object(backend, "get_config_path", return_value=str(self.config_path))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.temp_dir.cleanup)
        backend.invalidate_device_validation()

    def test_push_encodes_each_strip_and_forces_stream_for_ambilight(self):
        sent = []

        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self): return b'{"strips": []}'

        def urlopen(request, timeout=None):
            sent.append((request.full_url, urllib.parse.parse_qs(request.data.decode())))
            return Response()

        with mock.patch.object(backend.urllib.request, "urlopen", side_effect=urlopen):
            ok, _, reply = backend.push_device_segments(strips_device())
            backend.push_device_segments({"ip": "192.0.2.1", "role": "ambilight"}, save=False)
        self.assertTrue(ok)
        self.assertEqual(reply, {"strips": []})
        url, fields = sent[0]
        self.assertEqual(url, "http://192.0.2.9/segments")
        self.assertEqual(fields["s2_mode"], ["solid"])
        self.assertEqual(fields["s2_color"], ["00ff00"])
        self.assertEqual(fields["s2_brightness"], ["100"])
        self.assertEqual(fields["save"], ["1"])
        _, fields = sent[1]
        self.assertEqual({fields[f"s{i}_mode"][0] for i in range(1, 5)}, {"stream"})
        self.assertEqual(fields["save"], ["0"])

    def test_sync_endpoint_merges_firmware_strips_pushes_and_saves(self):
        self.config_path.write_text(json.dumps({"wemos_ip": "192.0.2.1", "wemos_devices": [strips_device()]}), encoding="utf-8")
        status = {"ip": "192.0.2.9", "led_count": 40, "stream_led_count": 40, "segments_supported": True,
                  "strips": [{"index": 1, "enabled": True, "led_count": 30, "led_pin": "D1"},
                             {"index": 2, "enabled": True, "led_count": 10, "led_pin": "D2"}]}
        reply = {"led_count": 40, "stream_led_count": 30, "strips": status["strips"]}
        handler = object.__new__(backend.WebUIHandler)
        handler.path = "/api/devices/desk/sync"
        body = b'{"push": true}'
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler._safe_send_json = mock.Mock()
        handler.send_error = mock.Mock(side_effect=AssertionError("Unexpected HTTP error"))
        with mock.patch.object(backend, "get_wemos_status", return_value=status), \
             mock.patch.object(backend, "push_device_segments", return_value=(True, "ok", reply)) as push:
            handler.do_POST()
        result = handler._safe_send_json.call_args.args[0]
        self.assertTrue(result["success"], result)
        self.assertTrue(result["pushed"])
        self.assertEqual([s["led_count"] for s in result["device"]["strips"]], [30, 10])
        self.assertEqual(result["device"]["strips"][1]["mode"], "solid")  # kullanıcı seçimi korundu
        self.assertTrue(result["validation"]["compatible"])  # 30 akış LED'i firmware yanıtıyla eşleşti
        push.assert_called_once()
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["wemos_devices"][0]["strips"][0]["led_count"], 30)
        self.assertEqual(saved["wemos_ip"], "192.0.2.1")

    def test_sync_unreachable_device_reports_error_without_saving(self):
        self.config_path.write_text(json.dumps({"wemos_devices": [strips_device()]}), encoding="utf-8")
        with mock.patch.object(backend, "get_wemos_status", return_value=None), \
             mock.patch.object(backend, "save_config", side_effect=AssertionError("must not save")):
            with self.assertRaises(ValueError):
                backend.sync_wemos_device(backend.load_config(), "desk")

    def test_worker_validation_cache_avoids_repeated_http_for_same_settings(self):
        device = strips_device()
        results = iter([{"compatible": False, "message": "no"}, {"compatible": True, "message": "ok"}])
        with mock.patch.object(backend, "get_device_validation", side_effect=lambda d, **k: next(results)) as probe:
            self.assertFalse(backend.cached_device_validation(device, now=1000)["compatible"])
            self.assertFalse(backend.cached_device_validation(device, now=1005)["compatible"])  # kısa aralık: tekrar sorma
            self.assertTrue(backend.cached_device_validation(device, now=1020)["compatible"])   # yeniden dene
            self.assertTrue(backend.cached_device_validation(device, now=5000)["compatible"])   # uyumlu: tekrar sorma
            self.assertEqual(probe.call_count, 2)
            device["strips"][1]["mode"] = "stream"  # ayar değişti: imza farklı
            with self.assertRaises(StopIteration):
                backend.cached_device_validation(device, now=5001)

    def test_legacy_primary_device_follows_single_panel_settings(self):
        config = {"wemos_ip": "192.0.2.50", "wemos_port": 7777, "top_leds": 9, "bottom_leds": 0,
                  "left_leds": 4, "right_leds": 4, "led_monitor_index": 2,
                  "wemos_devices": [{"id": "legacy-primary", "name": "Benim", "ip": "10.0.0.1", "port": 1,
                                     "role": "strips", "monitor_index": 1, "leds": {"top": 1}, "enabled": True}]}
        primary = backend.normalize_config_for_multi_device(config)["wemos_devices"][0]
        self.assertEqual(primary["ip"], "192.0.2.50")
        self.assertEqual(primary["leds"]["top"], 9)
        self.assertEqual(primary["monitor_index"], 2)
        self.assertEqual(primary["name"], "Benim")
        self.assertEqual(primary["role"], "strips")


if __name__ == "__main__":
    unittest.main()
