"""Regression checks for the pre-multi-device single-Wemos behavior.

These tests deliberately exercise a temporary config location.  They must never
read or overwrite the user's active LuxEdge config while the multi-device work
is being introduced in later steps.
"""

import importlib
import importlib.util
import inspect
import json
import sys
import tempfile
import threading
import types
import urllib.request
import unittest
from pathlib import Path
from unittest import mock


def _install_optional_dependency_stubs():
    """Allow config-level checks to import the backend without capture libraries."""
    if importlib.util.find_spec("mss") is None:
        mss_module = types.ModuleType("mss")
        mss_module.mss = object
        sys.modules["mss"] = mss_module

    if importlib.util.find_spec("PIL") is None:
        pil_module = types.ModuleType("PIL")
        image_module = types.ModuleType("PIL.Image")
        image_module.Image = object
        pil_module.Image = image_module
        sys.modules["PIL"] = pil_module
        sys.modules["PIL.Image"] = image_module


_install_optional_dependency_stubs()


class SingleWemosRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temp_dir.name) / "ambilight_config.json"
        self.backend = importlib.import_module("ambilight_pc")
        self.config_path_patch = mock.patch.object(
            self.backend, "get_config_path", return_value=str(self.config_path)
        )
        self.config_path_patch.start()

    def tearDown(self):
        self.config_path_patch.stop()
        self.temp_dir.cleanup()

    def test_current_single_wemos_config_round_trips_unchanged(self):
        source_config = json.loads(Path("ambilight_config.json").read_text(encoding="utf-8"))

        self.assertIn("wemos_ip", source_config)
        self.assertNotIn("wemos_devices", source_config)
        self.assertTrue(self.backend.save_config(source_config))
        self.assertEqual(self.backend.load_config(), source_config)

    def test_save_config_keeps_a_backup_of_the_previous_single_device_config(self):
        previous = {"wemos_ip": "192.168.1.50", "wemos_port": 7777}
        replacement = {"wemos_ip": "192.168.1.51", "wemos_port": 7777}
        self.config_path.write_text(json.dumps(previous), encoding="utf-8")

        self.assertTrue(self.backend.save_config(replacement))

        backup_path = Path(f"{self.config_path}.bak")
        self.assertEqual(json.loads(backup_path.read_text(encoding="utf-8")), previous)
        self.assertEqual(self.backend.load_config(), replacement)

    def test_legacy_worker_path_still_has_one_udp_target(self):
        worker_source = inspect.getsource(self.backend.ambilight_worker)

        self.assertIn('config.get("wemos_ip", "")', worker_source)
        self.assertIn("sock.sendto(data, (WEMOS_IP, WEMOS_PORT))", worker_source)

    def test_legacy_config_has_a_non_persistent_primary_device_view(self):
        legacy_config = {
            "wemos_ip": "192.168.1.50",
            "wemos_port": 7778,
            "led_monitor_index": 2,
            "top_leds": 12,
            "bottom_leds": 11,
            "left_leds": 7,
            "right_leds": 8,
        }

        normalized = self.backend.normalize_config_for_multi_device(legacy_config)

        self.assertNotIn("wemos_devices", legacy_config)
        self.assertEqual(normalized["schema_version"], 2)
        self.assertFalse(normalized["multi_device_enabled"])
        self.assertEqual(normalized["wemos_devices"], [{
            "id": "legacy-primary",
            "name": "Ana Wemos",
            "ip": "192.168.1.50",
            "port": 7778,
            "role": "ambilight",
            "monitor_index": 2,
            "leds": {"top": 12, "bottom": 11, "left": 7, "right": 8},
            "enabled": True,
        }])

    def test_existing_device_list_is_preserved(self):
        configured_device = {"id": "screen-two", "ip": "192.168.1.60"}
        config = {"schema_version": 3, "wemos_devices": [configured_device]}

        normalized = self.backend.normalize_config_for_multi_device(config)

        self.assertEqual(normalized["schema_version"], 3)
        self.assertEqual(normalized["wemos_devices"], [configured_device])
        self.assertFalse(normalized["multi_device_enabled"])

    def test_blank_legacy_ip_does_not_create_a_device(self):
        normalized = self.backend.normalize_config_for_multi_device({"wemos_ip": "  "})

        self.assertEqual(normalized["wemos_devices"], [])

    def test_device_crud_api_stays_passive_while_multi_mode_is_disabled(self):
        legacy_config = {"wemos_ip": "192.168.1.50", "wemos_port": 7777}
        self.config_path.write_text(json.dumps(legacy_config), encoding="utf-8")
        server = self.backend.HTTPServer(("127.0.0.1", 0), self.backend.WebUIHandler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()

        def request(path, method="GET", body=None):
            data = json.dumps(body).encode("utf-8") if body is not None else None
            http_request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}{path}",
                data=data,
                headers={"Content-Type": "application/json"},
                method=method,
            )
            with urllib.request.urlopen(http_request, timeout=2) as response:
                return json.loads(response.read().decode("utf-8"))

        try:
            listed = request("/api/devices")
            self.assertFalse(listed["multi_device_enabled"])
            self.assertEqual(listed["devices"][0]["id"], "legacy-primary")

            created = request("/api/devices", "POST", {
                "name": "İkinci Ekran",
                "ip": "192.168.1.60",
                "port": 7777,
                "monitor_index": 2,
                "leds": {"top": 10, "bottom": 10, "left": 5, "right": 5},
            })["device"]
            self.assertFalse(created["enabled"])

            updated = request(f"/api/devices/{created['id']}", "PUT", {
                "name": "İkinci Ekran Güncel",
                "leds": {"top": 12, "bottom": 10, "left": 5, "right": 5},
            })
            self.assertTrue(updated["success"])
            self.assertEqual(updated["device"]["name"], "İkinci Ekran Güncel")

            deleted = request(f"/api/devices/{created['id']}", "DELETE")
            self.assertTrue(deleted["success"])
            saved_config = json.loads(self.config_path.read_text(encoding="utf-8"))
            self.assertFalse(saved_config["multi_device_enabled"])
            self.assertEqual(saved_config["wemos_ip"], "192.168.1.50")
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)

    def test_renderer_uses_backend_device_api_without_local_storage(self):
        renderer_source = Path("web_ui/index.html").read_text(encoding="utf-8")

        self.assertIn("api.getDevices()", renderer_source)
        self.assertIn("api.createDevice(getDevicePayload())", renderer_source)
        self.assertIn("api.updateDevice(id, getDevicePayload())", renderer_source)
        self.assertIn("api.deleteDevice(id)", renderer_source)
        self.assertNotIn("localStorage", renderer_source)

    def test_device_validation_compares_firmware_total_without_sending_udp(self):
        device = {
            "id": "screen-two",
            "ip": "192.168.1.60",
            "leds": {"top": 12, "bottom": 10, "left": 5, "right": 5},
        }
        with mock.patch.object(self.backend, "get_wemos_status", return_value={"ip": device["ip"], "led_count": 32}):
            matching = self.backend.get_device_validation(device)
        with mock.patch.object(self.backend, "get_wemos_status", return_value={"ip": device["ip"], "led_count": 31}):
            mismatching = self.backend.get_device_validation(device)

        self.assertTrue(matching["reachable"])
        self.assertTrue(matching["compatible"])
        self.assertFalse(mismatching["compatible"])
        self.assertEqual(mismatching["expected_led_count"], 32)
        self.assertEqual(mismatching["firmware_led_count"], 31)

    def test_shadow_device_frame_is_built_without_a_udp_send(self):
        device = {
            "id": "screen-two",
            "monitor_index": 2,
            "leds": {"top": 1, "bottom": 0, "left": 1, "right": 0},
        }
        with mock.patch.object(self.backend, "grab_edge_colors", return_value=[(1, 2, 3), (4, 5, 6)]) as grab:
            frame = self.backend.build_device_frame(device, 20, 0, capture=object())

        self.assertEqual(frame, bytes([1, 2, 3, 4, 5, 6]))
        self.assertEqual(grab.call_args.kwargs["monitor_index"], 2)
        worker_source = inspect.getsource(self.backend.ambilight_worker)
        self.assertNotIn("sendto(shadow_frame", worker_source)

    def test_multi_mode_requires_compatible_active_devices(self):
        config = {
            "wemos_devices": [{
                "id": "screen-two", "name": "Ekran 2", "ip": "192.168.1.60", "port": 7777,
                "role": "ambilight", "monitor_index": 2,
                "leds": {"top": 12, "bottom": 10, "left": 5, "right": 5}, "enabled": True,
            }],
        }
        with mock.patch.object(self.backend, "get_device_validation", return_value={"compatible": True}):
            enabled = self.backend.set_multi_device_mode(config, True)
        with mock.patch.object(self.backend, "get_device_validation", return_value={"compatible": False}):
            with self.assertRaises(ValueError):
                self.backend.set_multi_device_mode(config, True)

        self.assertTrue(enabled["multi_device_enabled"])
        self.assertFalse(self.backend.set_multi_device_mode(enabled, False)["multi_device_enabled"])


if __name__ == "__main__":
    unittest.main()
