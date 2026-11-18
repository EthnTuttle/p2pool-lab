from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from p2pool_lab.devices import DeviceRegistry, fleet_summary, probe, scan_network


class DeviceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "devices.sqlite"
        self.registry = DeviceRegistry(self.connect, {})

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

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

    def test_bonanza_beta_model_is_discovered_and_still_requires_device_identity(self):
        info = {
            "ASICModel": "BZM",
            "hostname": "bitaxeBonanza-1004x",
            "macAddr": "12:34:56:78:9A:BC",
            "hashRate": 850,
        }
        response = MagicMock()
        response.__enter__.return_value = response
        with patch("p2pool_lab.devices.build_opener") as opener:
            opener.return_value.open.return_value = response
            response.read.return_value = json.dumps(info).encode()
            self.assertEqual(probe("192.168.50.2")["model"], "BZM")
            del info["macAddr"]
            response.read.return_value = json.dumps(info).encode()
            self.assertIsNone(probe("192.168.50.2"))

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
