from contextlib import contextmanager
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from p2pool_lab.devices import (
    DeadlineHTTPConnection,
    DeviceRegistry,
    fleet_summary,
    probe,
    scan_network,
)


class DeviceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "devices.sqlite"
        self.registry = DeviceRegistry(self.connect, {})
        self.addCleanup(
            self.registry.executor.shutdown, wait=False, cancel_futures=True
        )

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def test_numeric_ip_uses_numeric_only_address_resolution(self):
        connection = DeadlineHTTPConnection("192.0.2.1")
        with patch("p2pool_lab.devices.socket.getaddrinfo", return_value=[]) as resolve:
            with self.assertRaises(OSError):
                connection._deadline_create_connection(("192.0.2.1", 80), 1, None)
        resolve.assert_called_once_with(
            "192.0.2.1",
            80,
            0,
            socket.SOCK_STREAM,
            flags=socket.AI_NUMERICHOST,
        )

    def test_add_is_idempotent_and_inventory_survives_restart(self):
        device = {
            "address": "192.168.50.2",
            "mac": "123456789abc",
            "name": "Bitaxe",
            "model": "BM1370",
            "version": "v2.15.4",
        }
        with patch("p2pool_lab.devices.probe", return_value=device):
            first = self.registry.add(device["address"])
            second = self.registry.add(device["address"])
        self.assertEqual(first["id"], second["id"])
        restarted = DeviceRegistry(self.connect, {})
        self.assertEqual(len(restarted.devices()), 1)
        self.assertEqual(restarted.devices()[first["id"]]["url"], "http://192.168.50.2")

    def test_new_address_for_same_mac_preserves_device_id_and_worker(self):
        device = {"mac": "123456789abc", "name": "Bitaxe"}
        with patch("p2pool_lab.devices.probe", return_value=device):
            first = self.registry.add("192.168.50.2", "Office")
            moved = self.registry.add("192.168.50.3")
        self.assertEqual(moved["id"], first["id"])
        self.assertEqual(moved["worker"], first["worker"])
        self.assertEqual(moved["name"], "Office")
        self.assertEqual(len(self.registry.devices()), 1)

    def test_remove_stops_monitoring_and_frees_address_for_new_hardware(self):
        with patch(
            "p2pool_lab.devices.probe",
            return_value={"mac": "123456789abc", "name": "First"},
        ):
            first = self.registry.add("192.168.50.2")
        self.registry.remove(first["id"])
        self.assertEqual(self.registry.devices(), {})
        with patch(
            "p2pool_lab.devices.probe",
            return_value={"mac": "223456789abc", "name": "Second"},
        ):
            second = self.registry.add("192.168.50.2")
        self.assertNotEqual(first["id"], second["id"])

    def test_identify_rejects_missing_or_malformed_mac_when_entry_is_pinned(self):
        with patch(
            "p2pool_lab.devices.probe",
            return_value={"mac": "123456789abc", "name": "Bitaxe"},
        ):
            device = self.registry.add("192.168.50.2")
        for mac in (None, "", "not-a-mac", "123456789ab"):
            with self.subTest(mac=mac):
                self.assertFalse(self.registry.identify(device["id"], mac))
        self.assertTrue(self.registry.identify(device["id"], "12:34:56:78:9A:BC"))

    def test_identify_adopts_mac_once_for_unpinned_configured_device(self):
        registry = DeviceRegistry(
            self.connect,
            {
                "100": {
                    "name": "Configured",
                    "url": "http://192.168.50.2",
                    "worker": "bitaxe602",
                }
            },
        )
        self.assertTrue(registry.identify("100", None))
        self.assertTrue(registry.identify("100", "123456789abc"))
        self.assertFalse(registry.identify("100", None))
        self.assertTrue(registry.identify("100", "123456789abc"))
        self.assertFalse(registry.identify("100", "223456789abc"))

    def test_scan_rejects_public_network_and_oversized_lan(self):
        with self.assertRaises(ValueError):
            scan_network("8.8.8.0/24")
        with self.assertRaises(ValueError):
            scan_network("192.168.0.0/16")
        self.assertEqual(str(scan_network("192.168.50.7/24")), "192.168.50.0/24")

    def test_non_axe_device_cannot_be_registered(self):
        with patch("p2pool_lab.devices.probe", return_value=None):
            with self.assertRaisesRegex(ValueError, "No responding AxeOS"):
                self.registry.add("192.168.50.1")
        self.assertEqual(self.registry.devices(), {})

    def test_discovery_accepts_model_names_and_requires_valid_device_fields(self):
        info = {
            "ASICModel": "BZM",
            "hostname": "bitaxeBonanza-1004x",
            "macAddr": "12:34:56:78:9A:BC",
            "hashRate": 850,
        }
        response = MagicMock()
        response.__enter__.return_value = response

        def read_chunk(size):
            body = response.read(size)
            response.read.return_value = b""
            return body

        response.read1.side_effect = read_chunk
        with patch("p2pool_lab.devices.build_opener") as opener:
            opener.return_value.open.return_value = response
            for model in ("BM1370", "BZM", "Custom ASIC"):
                with self.subTest(model=model):
                    info["ASICModel"] = model
                    response.read.return_value = json.dumps(info).encode()
                    self.assertEqual(probe("192.168.50.2")["model"], model)
            for model in ("", "   ", None, 1370):
                with self.subTest(invalid_model=model):
                    response.read.return_value = json.dumps(
                        {**info, "ASICModel": model}
                    ).encode()
                    self.assertIsNone(probe("192.168.50.2"))
            for field in ("ASICModel", "macAddr", "hashRate"):
                with self.subTest(missing=field):
                    incomplete = {
                        key: value for key, value in info.items() if key != field
                    }
                    response.read.return_value = json.dumps(incomplete).encode()
                    self.assertIsNone(probe("192.168.50.2"))

    def test_header_trickle_does_not_hold_discovery_open(self):
        class SlowHeaders(BaseHTTPRequestHandler):
            def do_GET(self):
                response = (
                    b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"
                    b"Content-Type: application/json\r\nConnection: close\r\n\r\n{}"
                )
                for byte in response:
                    try:
                        self.wfile.write(bytes((byte,)))
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        break
                    time.sleep(0.03)

            def log_message(self, *args):
                pass

        running = ThreadingHTTPServer(("127.0.0.1", 0), SlowHeaders)
        self.addCleanup(running.server_close)
        self.addCleanup(running.shutdown)
        threading.Thread(target=running.serve_forever, daemon=True).start()
        with (
            patch(
                "p2pool_lab.devices.private_address",
                return_value=f"127.0.0.1:{running.server_port}",
            ),
            patch("p2pool_lab.devices.PROBE_TIMEOUT", 0.2),
            patch("p2pool_lab.devices.DISCOVERY_GRACE", 0.5),
        ):
            started = time.monotonic()
            self.registry._scan(["192.168.50.2"])
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1)
        self.assertTrue(all(future.done() for future in self.registry._probe_futures))
        self.assertEqual(self.registry.scan["checked"], 1)
        self.assertIsNone(self.registry.scan["error"])

    def test_probe_rejects_bad_http_status_and_pathological_json(self):
        with patch("p2pool_lab.devices.build_opener") as opener:
            opener.return_value.open.side_effect = http.client.BadStatusLine("bad")
            self.assertIsNone(probe("192.168.50.2"))

        response = MagicMock()
        response.__enter__.return_value = response
        response.read1.side_effect = [b"{}", b""]
        with (
            patch("p2pool_lab.devices.build_opener") as opener,
            patch("p2pool_lab.devices.json.loads", side_effect=RecursionError),
        ):
            opener.return_value.open.return_value = response
            self.assertIsNone(probe("192.168.50.2"))

    def test_probe_bounds_discovery_strings(self):
        body = json.dumps(
            {
                "ASICModel": "M" * 100,
                "hostname": "H" * 100,
                "version": "V" * 100,
                "macAddr": "12:34:56:78:9A:BC",
                "hashRate": 850,
            }
        ).encode()
        response = MagicMock()
        response.__enter__.return_value = response
        response.read1.side_effect = [body, b""]
        with patch("p2pool_lab.devices.build_opener") as opener:
            opener.return_value.open.return_value = response
            found = probe("192.168.50.2")
        self.assertEqual(len(found["name"]), 64)
        self.assertEqual(len(found["model"]), 64)
        self.assertEqual(len(found["version"]), 32)

    def test_one_bad_future_does_not_abort_remaining_discovery(self):
        found = {
            "address": "192.168.50.2",
            "mac": "123456789abc",
            "name": "Bitaxe",
            "model": "BM1370",
            "version": "v2.15.4",
        }

        def probing(address):
            if address.endswith(".1"):
                raise http.client.BadStatusLine("not HTTP")
            return found

        with patch("p2pool_lab.devices.probe", side_effect=probing):
            self.registry._scan(["192.168.50.1", "192.168.50.2"])
        result = self.registry.discovery()
        self.assertEqual(result["checked"], 2)
        self.assertEqual(result["results"], [{**found, "registered": False}])
        self.assertIsNone(result["error"])
        self.assertFalse(result["running"])

    def test_config_url_collision_preserves_inventory_and_other_rows(self):
        existing = {
            "mac": "123456789abc",
            "name": "Dashboard device",
        }
        with patch("p2pool_lab.devices.probe", return_value=existing):
            added = self.registry.add("192.168.50.2")
        with self.assertLogs(level="WARNING"):
            restarted = DeviceRegistry(
                self.connect,
                {
                    "100": {
                        "name": "Conflicting config",
                        "url": "http://192.168.50.2",
                        "worker": "conflict",
                    },
                    "110": {
                        "name": "Other config",
                        "url": "http://192.168.50.3",
                        "worker": "other",
                    },
                },
            )
        devices = restarted.devices()
        self.assertIn(added["id"], devices)
        self.assertIn("110", devices)
        self.assertNotIn("100", devices)
        self.assertEqual(len(devices), 2)

    def test_extreme_fleet_values_never_create_non_finite_aggregates(self):
        sources = {
            "miner-one": {
                "ok": True,
                "updated_at": 99,
                "data": {
                    "hashRate": 1e15,
                    "power": 1.7e308,
                    "sharesAccepted": 2**63 - 1,
                },
            },
            "miner-two": {
                "ok": True,
                "updated_at": 99,
                "data": {
                    "hashRate": 1e15,
                    "power": float("inf"),
                    "sharesAccepted": 2**63 - 1,
                },
            },
        }
        summary = fleet_summary(sources, {"one": {}, "two": {}}, 100)
        json.dumps(summary, allow_nan=False)
        self.assertIsNone(summary["hashRate"])
        self.assertIsNone(summary["power"])
        self.assertIsNone(summary["sharesAccepted"])
        self.assertIsNone(summary["temp"])

    def test_fleet_sums_hashrate_and_power_but_uses_hottest_temperature(self):
        sources = {
            "miner-one": {
                "ok": True,
                "updated_at": 99,
                "data": {
                    "hashRate": 800,
                    "power": 15,
                    "temp": 50,
                    "sharesAccepted": 10,
                    "sharesRejected": 0,
                },
            },
            "miner-two": {
                "ok": True,
                "updated_at": 99,
                "data": {
                    "hashRate": 1200,
                    "power": 20,
                    "temp": 65,
                    "sharesAccepted": 20,
                    "sharesRejected": 1,
                },
            },
        }
        summary = fleet_summary(sources, {"one": {}, "two": {}}, 100)
        self.assertEqual(summary["hashRate"], 2000)
        self.assertEqual(summary["power"], 35)
        self.assertEqual(summary["temp"], 65)
        self.assertEqual(summary["sharesAccepted"], 30)
        self.assertEqual(summary["reporting"], 2)

    def test_stale_miner_is_excluded_and_missing_is_not_zero(self):
        sources = {
            "miner-one": {
                "ok": True,
                "updated_at": 1,
                "data": {"hashRate": 800, "power": 15, "temp": 50},
            }
        }
        summary = fleet_summary(sources, {"one": {}}, 100)
        self.assertEqual(summary["reporting"], 0)
        self.assertIsNone(summary["hashRate"])
        self.assertIsNone(summary["power"])


if __name__ == "__main__":
    unittest.main()
