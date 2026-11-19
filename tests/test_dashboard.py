"""Verify data loss handling and that device controls stay narrow and same-origin."""

import copy
import http.client
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from p2pool_lab import dashboard as server


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "backups").mkdir()
        (self.root / "miner-settings.json").write_text(
            json.dumps(
                {
                    "bitcoin_payout_address": "tb1q-test-payout",
                    "share_owner_address": "tp2pool1-test",
                }
            )
        )
        root_patch = patch.object(server, "ROOT", self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        configuration = replace(
            server.CONFIG,
            bind_host="192.0.2.1",
            dashboard_host="192.0.2.1",
            public_host="192.0.2.1",
        )
        for attribute, value in (
            ("CONFIG", configuration),
            ("HOST", configuration.dashboard_host),
            ("ORIGIN", configuration.origin),
        ):
            configuration_patch = patch.object(server, attribute, value)
            configuration_patch.start()
            self.addCleanup(configuration_patch.stop)
        miners_patch = patch.object(
            server,
            "MINERS",
            {
                "100": {
                    "name": "Test miner",
                    "url": "http://192.0.2.100",
                    "worker": "bitaxe602",
                },
                "110": {
                    "name": "Second test miner",
                    "url": "http://192.0.2.110",
                    "worker": "bitaxe602-110",
                },
            },
        )
        miners_patch.start()
        self.addCleanup(miners_patch.stop)
        self.monitor = server.Monitor(self.root / "history.sqlite")
        self.addCleanup(self.monitor.executor.shutdown, wait=False, cancel_futures=True)

    def test_configuration_only_changes_pool_fields_and_does_not_restart(self):
        original = {
            "stratumURL": "old-pool",
            "stratumPort": 3334,
            "frequency": 290,
            "coreVoltage": 860,
        }
        saved = {
            "stratumURL": server.HOST,
            "stratumPort": 3333,
            "stratumUser": "tb1q-test-payout.bitaxe602",
            "fallbackStratumURL": server.HOST,
            "fallbackStratumPort": 3333,
            "fallbackStratumUser": "tb1q-test-payout.bitaxe602",
        }
        with (
            patch.object(server, "miner_status", side_effect=[original, saved]),
            patch.object(server, "request", return_value={}) as request,
        ):
            result = self.monitor.action("100", "configure-pool")
        request.assert_called_once()
        url, method, payload = request.call_args.args
        self.assertEqual(
            (url, method), (server.MINERS["100"]["url"] + "/api/system", "PATCH")
        )
        self.assertEqual(payload["stratumURL"], server.HOST)
        self.assertEqual(payload["fallbackStratumPort"], 3333)
        self.assertEqual(payload["stratumUser"], "tb1q-test-payout.bitaxe602")
        self.assertNotIn("frequency", payload)
        self.assertNotIn("coreVoltage", payload)
        self.assertIn("Restart", result["message"])
        backup = next((self.root / "backups").glob("*.json"))
        self.assertEqual(json.loads(backup.read_text())["stratumURL"], "old-pool")

    def test_array_pool_api_preserves_selected_slots_and_verifies_readback(self):
        original = {
            "pool_config_format": "array",
            "primaryPoolIndex": 2,
            "secondaryPoolIndex": 4,
        }
        saved = {
            "stratumURL": server.HOST,
            "stratumPort": 3333,
            "stratumUser": "tb1q-test-payout.bitaxe602",
            "fallbackStratumURL": server.HOST,
            "fallbackStratumPort": 3333,
            "fallbackStratumUser": "tb1q-test-payout.bitaxe602",
        }
        with (
            patch.object(server, "miner_status", side_effect=[original, saved]),
            patch.object(server, "request", return_value={}) as request,
        ):
            self.monitor.action("100", "configure-pool")
        payload = request.call_args.args[2]
        self.assertEqual(request.call_count, 2)
        self.assertEqual(
            request.call_args_list[0].args[:2],
            ("http://192.0.2.100/api/system/pools/2", "PUT"),
        )
        self.assertEqual(
            request.call_args_list[1].args[:2],
            ("http://192.0.2.100/api/system/pools/4", "PUT"),
        )
        self.assertEqual(payload["stratumUser"], "tb1q-test-payout.bitaxe602")
        self.assertNotIn("coreVoltage", payload)

    def test_ignored_pool_update_is_not_reported_as_success(self):
        with (
            patch.object(
                server, "miner_status", return_value={"stratumURL": "old-pool"}
            ),
            patch.object(server, "request", return_value={}),
        ):
            with self.assertRaisesRegex(RuntimeError, "did not save"):
                self.monitor.action("100", "configure-pool")

    def test_owner_shares_matches_share_address_and_includes_referenced_uncles(self):
        def entry(height, blockhash, owner="tp2pool1-test"):
            return {
                "height": height,
                "blockhash": blockhash,
                "miner_address": owner,
                "miner_bitcoin_address": "tb1q-test-payout",
                "timestamp": 1000 + height,
                "bits": 0x207FFFFF,
            }

        uncle = entry(99, "uncle")
        batches = [
            {
                "from_height": 7001,
                "to_height": 7100,
                "shares": [entry(7100, "tip"), entry(7001, "previous")],
            },
            {"chain_tip_height": 7100, "total_work": hex(7101 * 2)},
            {
                "from_height": 100,
                "to_height": 199,
                "shares": [
                    dict(entry(150, "confirmed"), uncles=[]),
                    dict(entry(110, "other", "tp2pool1-other"), uncles=[uncle]),
                    dict(entry(105, "other2", "tp2pool1-other"), uncles=[uncle]),
                ],
            },
            {"from_height": 0, "to_height": 99, "shares": []},
        ]
        with (
            patch.object(server, "request", side_effect=batches) as request,
            patch.object(server.time, "time", return_value=8101),
            patch.object(
                server, "bitcoin_rpc", return_value={"coinbasevalue": 6048000}
            ),
            patch.object(
                server,
                "CONFIG",
                SimpleNamespace(pool_auth_headers={"Authorization": "Basic pool-test"}),
            ),
        ):
            page = self.monitor.owner_shares(199)

        self.assertEqual((page["from_height"], page["to_height"]), (0, 199))
        self.assertEqual(page["tip_height"], 7100)
        self.assertEqual(page["seconds_per_share"], 1)
        self.assertEqual(
            [(share["blockhash"], share["kind"]) for share in page["shares"]],
            [("confirmed", "Confirmed"), ("uncle", "Uncle")],
        )
        self.assertEqual(
            [call.args[0].rsplit("/", 1)[1] for call in request.call_args_list],
            [
                "shares?num=100",
                "chain_info",
                "shares?num=100&to=199",
                "shares?num=100&to=99",
            ],
        )
        for call in request.call_args_list:
            self.assertEqual(
                call.kwargs["headers"], {"Authorization": "Basic pool-test"}
            )
        self.assertEqual(
            [share["payout_from_height"] for share in page["shares"]], [151, 106]
        )
        self.assertEqual(
            [share["estimated_block_sats"] for share in page["shares"]], [1000, 900]
        )

    def test_owner_page_index_skips_empty_ranges_and_rebuilds_after_reorg(self):
        from urllib.parse import parse_qs, urlsplit

        owner = self.monitor.settings["share_owner_address"]
        tip = 1500
        owned = {25, 1460}
        revision = 0

        def fetch(url, **kwargs):
            query = parse_qs(urlsplit(url).query)
            end = min(tip, int(query.get("to", [tip])[0]))
            start = max(0, end - int(query["num"][0]) + 1)
            return {
                "from_height": start,
                "to_height": end,
                "shares": [
                    {
                        "height": height,
                        "blockhash": f"{revision}-{height}",
                        "miner_address": "other",
                        "uncles": [{"miner_address": owner}] if height in owned else [],
                    }
                    for height in range(end, start - 1, -1)
                ],
            }

        with (
            patch.object(server, "request", side_effect=fetch) as request,
            patch.object(
                server,
                "CONFIG",
                SimpleNamespace(pool_auth_headers={"Authorization": "Basic pool-test"}),
            ),
        ):
            self.assertEqual(self.monitor.owner_share_pages(), [1500, 500])
            request.reset_mock()
            tip = 1501
            self.assertEqual(self.monitor.owner_share_pages(), [1501, 501])
            self.assertEqual(request.call_count, 4)
            revision = 1
            owned = {990}
            self.assertEqual(self.monitor.owner_share_pages(), [1001])
            revision = 2
            owned = set()
            self.assertEqual(self.monitor.owner_share_pages(), [])
            for call in request.call_args_list:
                self.assertEqual(
                    call.kwargs["headers"], {"Authorization": "Basic pool-test"}
                )

    def test_share_payout_window_and_recent_pace(self):
        self.assertEqual(server.payout_window(33_037), (39_085, 160_044))
        self.assertEqual(server.payout_window(6_047), (6_048, 133_054))
        self.assertEqual(server.payout_window(6_048), (12_096, 133_055))
        recent = [
            {"height": 105, "timestamp": 150},
            {"height": 100, "timestamp": 100},
        ]
        self.assertEqual(server.recent_share_interval(recent, 151), 10)
        self.assertIsNone(server.recent_share_interval(recent, 1100))
        self.assertIsNone(server.recent_share_interval(recent[:1], 151))

    def test_share_value_uses_work_discount_and_payout_window_size(self):
        estimate = server.estimate_share_sats
        partial = (8_953_000, 30_000, 14_999, 15_000, 0x207FFFFF)
        self.assertEqual(estimate(*partial, 10), 1000)
        self.assertEqual(estimate(*partial, 9), 900)
        self.assertEqual(estimate(*partial, 11), 1100)
        self.assertEqual(
            estimate(120_960_000, 500_000, 249_999, 250_000, 0x207FFFFF, 10), 1000
        )
        self.assertIsNone(estimate(120_960_000, 500_000, 249_999, 250_000, 0, 10))

    def test_lab_block_count_includes_finds_older_than_the_recent_table(self):
        payout = self.monitor.settings["bitcoin_payout_address"]
        lines = ["bitcoin_blocks_found_total 10"]
        for height in range(10):
            miner = payout if height in (0, 9) else "tb1q-other"
            lines.append(
                f'bitcoin_block_found_time_seconds{{blockhash="{height:064x}",'
                f'height="{height}",miner="{miner}",network="testnet5"}} {height + 100}'
            )
        metrics = server.parse_metrics("\n".join(lines), payout)
        self.assertEqual(metrics["values"]["bitcoin_blocks_found_total"], 10)
        self.assertEqual(metrics["lab_blocks_found"], 2)
        self.assertEqual(len(metrics["blocks"]), 8)
        self.assertEqual(metrics["blocks"][0]["height"], "9")
        incomplete = server.parse_metrics("\n".join(lines[:-1]), payout)
        empty = server.parse_metrics("bitcoin_blocks_found_total 0", payout)
        self.assertIsNone(incomplete["lab_blocks_found"])
        self.assertEqual(empty["lab_blocks_found"], 0)

    def test_failed_miner_poll_records_gap_and_retains_last_good_timestamp(self):
        def fetch(url, **kwargs):
            if url.endswith("chain_info"):
                return {"chain_tip_height": 20}
            if url.endswith("peers"):
                return []
            return "shares_accepted_total 0\n"

        with (
            patch.object(server, "request", side_effect=fetch),
            patch.object(server, "bitcoin_status", return_value={"blocks": 50}),
            patch.object(self.monitor.wallet, "balance", return_value={"confirmed": 0}),
            patch.object(
                server,
                "miner_status",
                return_value={"hashRate": 700, "temp": 55, "power": 12},
            ),
        ):
            self.monitor.collect()
        last_good = self.monitor.snapshot()["sources"]["miner-100"]["updated_at"]

        def next_miner(miner_id, url=None):
            if miner_id == "100":
                raise TimeoutError("offline")
            return {"hashRate": 900, "temp": 60, "power": 15}

        with (
            patch.object(server, "request", side_effect=fetch),
            patch.object(server, "bitcoin_status", return_value={"blocks": 50}),
            patch.object(server, "miner_status", side_effect=next_miner),
        ):
            self.monitor.collect()
        source = self.monitor.snapshot()["sources"]["miner-100"]
        self.assertFalse(source["ok"])
        self.assertEqual(source["updated_at"], last_good)
        self.assertEqual(source["data"]["hashRate"], 700)
        with self.monitor.connect() as connection:
            latest = connection.execute(
                "SELECT hashrate FROM miner_samples WHERE miner='100' ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
        self.assertIsNone(latest["hashrate"])
        self.assertEqual(
            self.monitor.snapshot()["sources"]["miner-110"]["data"]["hashRate"], 900
        )

    def test_collect_uses_bounded_future_wait_and_isolates_event_failures(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def pool_data(url, **kwargs):
            if url.endswith("peers"):
                return []
            if url.endswith("metrics"):
                return ""
            return {"chain_tip_height": 20}

        with (
            patch.object(self.monitor.registry, "devices", return_value={}),
            patch.object(server, "pool_request", side_effect=pool_data),
            patch.object(server, "bitcoin_status", side_effect=release.wait) as bitcoin,
            patch.object(server, "REQUEST_TIMEOUT", 0.01),
            patch.object(server, "COLLECTION_GRACE", 0.01),
        ):
            started = time.monotonic()
            self.monitor.collect()
            self.monitor.collect()
            workers_after_warmup = len(self.monitor.executor._threads)
            self.monitor.collect()
            self.monitor.collect()
            elapsed = time.monotonic() - started
            self.assertEqual(len(self.monitor.executor._threads), workers_after_warmup)
            bitcoin.assert_called_once_with()
        release.set()
        for future in self.monitor.loader_futures.values():
            future.result(timeout=1)
        self.assertLess(elapsed, 1)
        self.assertFalse(self.monitor.state["sources"]["bitcoin"]["ok"])
        self.assertIsNotNone(self.monitor.state["updated_at"])
        self.monitor.state["alerts"] = []

        with (
            patch.object(self.monitor.registry, "devices", return_value={}),
            patch.object(server, "pool_request", side_effect=pool_data),
            patch.object(server, "bitcoin_status", side_effect=RuntimeError("offline")),
            patch.object(
                self.monitor,
                "event",
                side_effect=UnicodeEncodeError("utf-8", "\ud800", 0, 1, "bad"),
            ) as event,
        ):
            with self.assertLogs(level="WARNING"):
                self.monitor.collect()
        event.assert_called()
        self.assertIsNotNone(self.monitor.state["updated_at"])

    def test_collect_pool_requests_include_configured_auth_headers(self):
        headers = {"Authorization": "Basic pool-test"}

        def fetch(url, **kwargs):
            if url.endswith("chain_info"):
                return {"chain_tip_height": 20}
            if url.endswith("peers"):
                return []
            return "shares_accepted_total 0\n"

        with (
            patch.object(self.monitor.registry, "devices", return_value={}),
            patch.object(server, "bitcoin_status", return_value={"blocks": 50}),
            patch.object(server, "CONFIG", SimpleNamespace(pool_auth_headers=headers)),
            patch.object(server, "request", side_effect=fetch) as request,
        ):
            self.monitor.collect()
        self.assertEqual(request.call_count, 3)
        for call in request.call_args_list:
            self.assertEqual(call.kwargs["headers"], headers)

    def test_badly_typed_miner_fields_do_not_stop_the_collection_cycle(self):
        def fetch(url, **kwargs):
            if url.endswith("chain_info"):
                return {"chain_tip_height": 20}
            if url.endswith("peers"):
                return []
            if url.endswith("/api/system/info"):
                if "192.0.2.100" in url:
                    return {
                        "version": 1,
                        "hashRate": "fast",
                        "temp": "hot",
                        "temp2": [],
                        "vrTemp": {},
                        "power": {"watts": 15},
                        "coreVoltageActual": "low",
                        "sharesAccepted": "10",
                        "sharesRejected": None,
                        "poolConnectionInfo": [],
                        "power_fault": {"reason": "test"},
                        "overheat_mode": {"bad": True},
                    }
                return {
                    "version": "v2.15.4",
                    "hashRate": 900,
                    "temp": 60,
                    "power": 15,
                    "sharesAccepted": 2,
                    "sharesRejected": 0,
                }
            return "shares_accepted_total 0\n"

        with (
            patch.object(server, "request", side_effect=fetch),
            patch.object(server, "bitcoin_status", return_value={"blocks": 50}),
        ):
            self.monitor.collect()

        state = self.monitor.snapshot()
        self.assertTrue(state["sources"]["miner-100"]["ok"])
        self.assertTrue(state["sources"]["miner-110"]["ok"])
        self.assertEqual(state["sources"]["miner-110"]["data"]["hashRate"], 900)
        malformed = state["sources"]["miner-100"]["data"]
        self.assertIsNone(malformed["hashRate"])
        self.assertIsNone(malformed["temp"])
        self.assertIsNone(malformed["power"])
        self.assertIsNone(malformed["coreVoltageActual"])
        self.assertIsNone(malformed["poolConnectionInfo"])
        with self.monitor.connect() as connection:
            rows = {
                row["miner"]: row["hashrate"]
                for row in connection.execute(
                    "SELECT miner, hashrate FROM miner_samples WHERE timestamp=?",
                    (state["updated_at"],),
                )
            }
        self.assertEqual(rows, {"100": None, "110": 900})

    def test_out_of_range_integer_telemetry_cannot_stop_collection(self):
        def fetch(url, **kwargs):
            if url.endswith("chain_info"):
                return {"chain_tip_height": 20}
            if url.endswith("peers"):
                return []
            if url.endswith("/api/system/info"):
                if "192.0.2.100" in url:
                    return {
                        "hashRate": 2**63,
                        "temp": 55,
                        "power": 15,
                        "sharesAccepted": 1,
                        "sharesRejected": 0,
                    }
                return {"hashRate": 900, "temp": 60, "power": 15}
            return "shares_accepted_total 0\n"

        with (
            patch.object(server, "request", side_effect=fetch),
            patch.object(server, "bitcoin_status", return_value={"blocks": 50}),
        ):
            self.monitor.collect()

        state = self.monitor.snapshot()
        self.assertTrue(state["sources"]["miner-100"]["ok"])
        self.assertTrue(state["sources"]["miner-110"]["ok"])
        self.assertIsNone(state["sources"]["miner-100"]["data"]["hashRate"])
        self.assertEqual(state["sources"]["miner-110"]["data"]["hashRate"], 900)
        with self.monitor.connect() as connection:
            rows = {
                row["miner"]: row["hashrate"]
                for row in connection.execute(
                    "SELECT miner, hashrate FROM miner_samples WHERE timestamp=?",
                    (state["updated_at"],),
                )
            }
        self.assertEqual(rows, {"100": None, "110": 900})

    def test_finite_number_rejects_values_sqlite_cannot_bind(self):
        self.assertEqual(server.finite_number(2**63 - 1), 2**63 - 1)
        self.assertEqual(server.finite_number(-(2**63)), -(2**63))
        for value in (2**63, -(2**63) - 1, 10**309):
            with self.subTest(value=value):
                self.assertIsNone(server.finite_number(value))
        self.assertIsNone(server.finite_number(float("inf")))
        self.assertIsNone(server.finite_number(float("nan")))
        self.assertIsNone(server.finite_number(True))
        self.assertEqual(server.finite_number(0), 0)

    def test_miner_schema_neutralizes_hostile_values_and_deep_containers(self):
        nested = current = []
        for _ in range(2000):
            child = []
            current.append(child)
            current = child
        with patch.object(
            server,
            "request",
            return_value={
                "hostname": [float("nan")],
                "hashRate": 1.7e308,
                "power": float("inf"),
                "temp": float("nan"),
                "power_fault": "\ud800",
                "poolConnectionInfo": 1,
                "sharesRejectedReasons": nested,
                "stratumURL": "x" * 1000,
                "miningPaused": "false",
                "overheat_mode": [1],
                "autofanspeed": 1,
                "ASICModel": 1370,
                "stratumPassword": "must not escape",
            },
        ):
            info = server.miner_status("100")

        self.assertIsNone(info["hostname"])
        self.assertIsNone(info["hashRate"])
        self.assertIsNone(info["power"])
        self.assertIsNone(info["temp"])
        self.assertIsNone(info["poolConnectionInfo"])
        self.assertIsNone(info["sharesRejectedReasons"])
        self.assertIsNone(info["miningPaused"])
        self.assertIsNone(info["overheat_mode"])
        self.assertIsNone(info["ASICModel"])
        self.assertEqual(info["autofanspeed"], 1)
        self.assertEqual(info["power_fault"], "?")
        self.assertEqual(len(info["stratumURL"]), 256)
        self.assertNotIn("stratumPassword", info)
        json.dumps(copy.deepcopy(info), allow_nan=False).encode("utf-8")

    def test_extreme_telemetry_cannot_poison_state_history_or_events(self):
        def fetch(url, **kwargs):
            if url.endswith("chain_info"):
                return {"chain_tip_height": 20}
            if url.endswith("peers"):
                return []
            if url.endswith("/api/system/info"):
                return {
                    "hashRate": 1.7e308,
                    "power": 1.7e308,
                    "temp": 55,
                    "power_fault": "\ud800",
                }
            return "shares_accepted_total 0\n"

        with (
            patch.object(server, "request", side_effect=fetch),
            patch.object(server, "bitcoin_status", return_value={"blocks": 50}),
        ):
            self.monitor.collect()

        state = self.monitor.snapshot()
        self.assertIsNone(state["sources"]["miner-100"]["data"]["hashRate"])
        self.assertIsNone(state["fleet"]["hashRate"])
        json.dumps(state, allow_nan=False).encode("utf-8")
        json.dumps(self.monitor.history(1, "fleet"), allow_nan=False).encode("utf-8")
        with self.monitor.connect() as connection:
            message = connection.execute(
                "SELECT message FROM events ORDER BY id DESC LIMIT 1"
            ).fetchone()["message"]
        message.encode("utf-8")
        self.assertIn("Bitaxe .", message)
        self.assertIn("?", message)

    def test_finite_number_rejects_float_magnitudes_beyond_telemetry_bounds(self):
        self.assertEqual(server.finite_number(1e15), 1e15)
        self.assertEqual(server.finite_number(-1e15), -1e15)
        self.assertIsNone(server.finite_number(1e15 + 1))
        self.assertIsNone(server.finite_number(-1.7e308))

    def test_outbound_hostname_is_rejected_without_calling_resolver(self):
        started = time.monotonic()
        with patch("p2pool_lab.devices.socket.getaddrinfo") as resolve:
            with self.assertRaisesRegex(OSError, "numeric IP addresses"):
                server.request("http://miner.example/", timeout=1)
            resolve.assert_not_called()
        self.assertLess(time.monotonic() - started, 0.5)

    def test_request_rejects_redirects_instead_of_following_them(self):
        target_hits = []

        class Target(BaseHTTPRequestHandler):
            def do_GET(self):
                target_hits.append(self.path)
                body = b"{}"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        target = ThreadingHTTPServer(("127.0.0.1", 0), Target)
        self.addCleanup(target.server_close)
        self.addCleanup(target.shutdown)
        threading.Thread(target=target.serve_forever, daemon=True).start()

        class Redirect(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header(
                    "Location", f"http://127.0.0.1:{target.server_port}/secret"
                )
                self.end_headers()

            def log_message(self, *args):
                pass

        redirect = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        self.addCleanup(redirect.server_close)
        self.addCleanup(redirect.shutdown)
        threading.Thread(target=redirect.serve_forever, daemon=True).start()

        with self.assertRaises(HTTPError):
            server.request(f"http://127.0.0.1:{redirect.server_port}/start")
        self.assertEqual(target_hits, [])

    def test_slow_response_body_is_cut_off_by_total_deadline(self):
        class SlowDrip(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "128")
                self.end_headers()
                for _ in range(128):
                    try:
                        self.wfile.write(b" ")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        break
                    time.sleep(0.03)

            def log_message(self, *args):
                pass

        running = ThreadingHTTPServer(("127.0.0.1", 0), SlowDrip)
        self.addCleanup(running.server_close)
        self.addCleanup(running.shutdown)
        threading.Thread(target=running.serve_forever, daemon=True).start()
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            server.request(f"http://127.0.0.1:{running.server_port}/", timeout=0.2)
        self.assertLess(time.monotonic() - started, 1)

    def test_slow_response_headers_are_cut_off_by_absolute_deadline(self):
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
        started = time.monotonic()
        with self.assertRaises((OSError, http.client.HTTPException)):
            server.request(f"http://127.0.0.1:{running.server_port}/", timeout=0.2)
        self.assertLess(time.monotonic() - started, 1)

    def test_miner_api_filters_credentials(self):
        with patch.object(
            server,
            "request",
            return_value={
                "hashRate": 0,
                "stratumPassword": "secret",
                "ssid": "private-wifi",
                "power_fault": "Fault",
            },
        ):
            info = server.miner_status("100")
        self.assertEqual(
            info,
            {
                "hashRate": 0,
                "power_fault": "Fault",
                "pool_config_format": "legacy",
                "supported_actions": ["configure-pool", "restart"],
            },
        )

    def test_negative_temperature_is_an_unavailable_sensor_not_a_real_reading(self):
        with patch.object(
            server,
            "request",
            return_value={"hashRate": 0, "temp": -1, "temp2": -1, "vrTemp": 25},
        ):
            info = server.miner_status("100")
        self.assertIsNone(info["temp"])
        self.assertIsNone(info["temp2"])
        self.assertEqual(info["vrTemp"], 25)
        alerts = server.build_alerts({"miner-100": {"ok": True, "data": info}})
        self.assertIn("sensor is unavailable", alerts[0]["message"])

    def test_huge_version_digits_cannot_disable_miner_polling(self):
        with patch.object(
            server,
            "request",
            return_value={"version": "2.14." + "9" * 5000},
        ):
            info = server.miner_status("100")
        self.assertEqual(len(info["version"]), 64)
        self.assertIn("pause", info["supported_actions"])

    def test_metrics_block_timestamps_are_sanitized_in_full_state(self):
        valid_hash = "ab" * 32
        lines = ["bitcoin_blocks_found_total 4"]
        for timestamp in ("nan", "inf", "1e9999"):
            lines.append(
                f'bitcoin_block_found_time_seconds{{blockhash="{valid_hash}",'
                f'miner="tb1q-other",network="testnet5"}} {timestamp}'
            )
        lines.append(
            f'bitcoin_block_found_time_seconds{{blockhash="{valid_hash}",'
            f'miner="tb1q-other",network="testnet5"}} 100'
        )

        def fetch(url, **kwargs):
            if url.endswith("chain_info"):
                return {"chain_tip_height": 20}
            if url.endswith("peers"):
                return []
            return "\n".join(lines)

        with (
            patch.object(self.monitor.registry, "devices", return_value={}),
            patch.object(server, "request", side_effect=fetch),
            patch.object(server, "bitcoin_status", return_value={"blocks": 50}),
        ):
            self.monitor.collect()
        state = self.monitor.snapshot()
        json.dumps(state, allow_nan=False).encode("utf-8")
        blocks = state["sources"]["metrics"]["data"]["blocks"]
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["timestamp"], 100)

    def test_history_retains_zero_and_null_samples(self):
        now = int(time.time())
        with self.monitor.connect() as connection:
            connection.execute(
                "INSERT INTO miner_samples VALUES (?,?,?,?,?,?,?,?,?)",
                ("100", now - 15, 0, 50, 5, 0, 0, 10, 20),
            )
            connection.execute(
                "INSERT INTO miner_samples VALUES (?,?,?,?,?,?,?,?,?)",
                ("100", now - 5, None, None, None, None, None, 10, 20),
            )
            connection.execute(
                "INSERT INTO miner_samples VALUES (?,?,?,?,?,?,?,?,?)",
                ("110", now - 15, 900, 60, 15, 2, 0, 10, 20),
            )
        history = self.monitor.history(1, "100")
        self.assertEqual(history[0]["hashrate"], 0)
        self.assertIsNone(history[1]["hashrate"])
        self.assertEqual(self.monitor.history(1, "110")[0]["hashrate"], 900)

    def test_old_single_miner_history_migrates_to_original_device(self):
        now = int(time.time())
        with self.monitor.connect() as connection:
            connection.execute(
                "CREATE TABLE samples (timestamp INTEGER PRIMARY KEY, hashrate REAL, temperature REAL, power REAL, accepted REAL, rejected REAL, bitcoin_height INTEGER, share_height INTEGER)"
            )
            connection.execute(
                "INSERT INTO samples VALUES (?,?,?,?,?,?,?,?)",
                (now, 700, 50, 15, 10, 0, 20, 40),
            )
        migrated = server.Monitor(self.root / "history.sqlite")
        self.assertEqual(migrated.history(1, "100")[0]["hashrate"], 700)
        self.assertEqual(migrated.history(1, "110"), [])

    def test_fleet_history_sums_simultaneous_samples_and_preserves_peak_temperature(
        self,
    ):
        now = int(time.time())
        with self.monitor.connect() as connection:
            connection.execute(
                "INSERT INTO miner_samples VALUES (?,?,?,?,?,?,?,?,?)",
                ("100", now, 800, 50, 15, 10, 0, 20, 40),
            )
            connection.execute(
                "INSERT INTO miner_samples VALUES (?,?,?,?,?,?,?,?,?)",
                ("110", now, 1200, 65, 20, 20, 1, 20, 40),
            )
        history = self.monitor.history(1, "fleet")
        self.assertEqual(history[0]["hashrate"], 2000)
        self.assertEqual(history[0]["power"], 35)
        self.assertEqual(history[0]["temperature"], 65)

    def test_baseline_extreme_history_values_are_safe_at_read_and_http_boundary(self):
        now = int(time.time())
        with self.monitor.connect() as connection:
            for miner_id in ("100", "110"):
                connection.execute(
                    "INSERT INTO miner_samples VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        miner_id,
                        now,
                        1.7e308,
                        1.7e308,
                        1.7e308,
                        1.7e308,
                        1.7e308,
                        20,
                        40,
                    ),
                )
        rows = self.monitor.history(1, "fleet")
        self.assertTrue(rows)
        for row in rows:
            for field in ("hashrate", "temperature", "power", "accepted", "rejected"):
                self.assertIsNone(row[field])
        json.dumps(rows, allow_nan=False)
        individual = self.monitor.history(1, "100")
        json.dumps(individual, allow_nan=False)
        self.assertIsNone(individual[0]["hashrate"])

        running = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        running.monitor = self.monitor
        threading.Thread(target=running.serve_forever, daemon=True).start()
        self.addCleanup(running.server_close)
        self.addCleanup(running.shutdown)
        client = http.client.HTTPConnection("127.0.0.1", running.server_port)
        self.addCleanup(client.close)
        client.request(
            "GET",
            "/api/history?hours=1&miner=fleet",
            headers={"Host": f"{server.HOST}:{server.PORT}"},
        )
        response = client.getresponse()
        body = response.read()
        self.assertEqual(response.status, 200)
        history = json.loads(body)
        json.dumps(history, allow_nan=False)
        self.assertIsNone(history[0]["hashrate"])

    def test_old_firmware_unsupported_control_never_sends_post(self):
        with (
            patch.object(
                server,
                "miner_status",
                return_value={"supported_actions": ["restart", "configure-pool"]},
            ),
            patch.object(server, "request") as request,
        ):
            with self.assertRaisesRegex(ValueError, "not supported"):
                self.monitor.action("110", "pause")
        request.assert_not_called()

    def test_restart_routes_only_to_selected_device(self):
        with (
            patch.object(
                server, "miner_status", return_value={"supported_actions": ["restart"]}
            ),
            patch.object(server, "request", return_value={}) as request,
        ):
            self.monitor.action("110", "restart")
        request.assert_called_once_with(
            "http://192.0.2.110/api/system/restart", "POST", {}
        )

    def test_log_message_handles_single_argument_log_errors(self):
        handler = object.__new__(server.Handler)
        with patch.object(server.logging, "info") as log:
            handler.log_message("%s", "socket error")
            log.assert_called_once_with("%s", "socket error")
            log.reset_mock()
            handler.log_message('"%s" %s %s', "GET / HTTP/1.1", "200", "10")
            log.assert_not_called()

    def test_api_get_rejects_cross_site_fetch_but_not_navigation_or_static(self):
        running = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        running.monitor = self.monitor
        threading.Thread(target=running.serve_forever, daemon=True).start()
        self.addCleanup(running.server_close)
        self.addCleanup(running.shutdown)
        host = f"{server.HOST}:{server.PORT}"
        client = http.client.HTTPConnection("127.0.0.1", running.server_port)
        self.addCleanup(client.close)
        with patch.object(self.monitor, "snapshot", return_value={}) as snapshot:
            client.request(
                "GET",
                "/api/state",
                headers={"Host": host, "Sec-Fetch-Site": "cross-site"},
            )
            response = client.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            snapshot.assert_not_called()
            client.request(
                "GET",
                "/api/state",
                headers={"Host": host, "Sec-Fetch-Site": "same-origin"},
            )
            response = client.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            snapshot.assert_called_once_with()
            client.request(
                "GET",
                "/api/state",
                headers={"Host": host, "Sec-Fetch-Site": "none"},
            )
            response = client.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
        for path in ("/", "/app.js"):
            client.request(
                "GET", path, headers={"Host": host, "Sec-Fetch-Site": "cross-site"}
            )
            response = client.getresponse()
            self.assertEqual(response.status, 200)
            response.read()

    def test_non_ascii_dashboard_token_is_rejected_without_server_error(self):
        running = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        running.monitor = self.monitor
        threading.Thread(target=running.serve_forever, daemon=True).start()
        self.addCleanup(running.server_close)
        self.addCleanup(running.shutdown)
        client = http.client.HTTPConnection("127.0.0.1", running.server_port)
        self.addCleanup(client.close)
        with patch.object(self.monitor, "action") as action:
            client.request(
                "POST",
                "/api/miner/100/identify",
                body="{}",
                headers={
                    "Host": f"{server.HOST}:{server.PORT}",
                    "Origin": server.ORIGIN,
                    "X-Dashboard-Token": "é",
                },
            )
            response = client.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            action.assert_not_called()

    def test_http_controls_reject_cross_origin_and_unknown_actions(self):
        running_server = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        running_server.monitor = self.monitor
        thread = threading.Thread(target=running_server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(running_server.server_close)
        self.addCleanup(running_server.shutdown)
        headers = {
            "Host": f"{server.HOST}:{server.PORT}",
            "Origin": "https://foreign.example",
            "X-Dashboard-Token": self.monitor.token,
        }
        with patch.object(
            self.monitor, "action", return_value={"message": "identified"}
        ) as action:
            client = http.client.HTTPConnection("127.0.0.1", running_server.server_port)
            client.request(
                "POST", "/api/miner/100/identify", body="{}", headers=headers
            )
            response = client.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            action.assert_not_called()
            headers["Origin"] = server.ORIGIN
            client.request(
                "POST", "/api/miner/100/firmware", body="{}", headers=headers
            )
            response = client.getresponse()
            self.assertEqual(response.status, 404)
            response.read()
            action.assert_not_called()
            client.request(
                "POST", "/api/miner/100/identify", body="{}", headers=headers
            )
            response = client.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            action.assert_called_once_with("100", "identify")
            client.close()

    def test_wallet_send_requires_same_origin_token_and_routes_only_json(self):
        running_server = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        running_server.monitor = self.monitor
        threading.Thread(target=running_server.serve_forever, daemon=True).start()
        self.addCleanup(running_server.server_close)
        self.addCleanup(running_server.shutdown)
        headers = {
            "Host": f"{server.HOST}:{server.PORT}",
            "Content-Type": "application/json",
        }
        client = http.client.HTTPConnection("127.0.0.1", running_server.server_port)
        with patch.object(
            self.monitor.wallet, "send", return_value={"txid": "ab" * 32}
        ) as send:
            client.request(
                "POST",
                "/api/wallet/send",
                body='{"draft_id":"reviewed"}',
                headers=headers,
            )
            response = client.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            send.assert_not_called()
            headers.update(
                {"Origin": server.ORIGIN, "X-Dashboard-Token": self.monitor.token}
            )
            client.request(
                "POST",
                "/api/wallet/send",
                body='{"draft_id":"reviewed"}',
                headers=headers,
            )
            response = client.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            send.assert_called_once_with({"draft_id": "reviewed"})
        client.close()


if __name__ == "__main__":
    unittest.main()
