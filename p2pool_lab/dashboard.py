"""LAN-only monitoring and fixed Bitaxe controls for this testnet5 installation."""

import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import ipaddress
import logging
import math
import mimetypes
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import threading
import time
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

from .wallet_api import WalletError, WalletService
from .config import GENESIS, load
from .devices import DeviceRegistry, fleet_summary

CONFIG = load()
ROOT = CONFIG.state_dir
STATIC = Path(__file__).resolve().parent / "static"
HOST = CONFIG.bind_host
PORT = CONFIG.dashboard_port
ORIGIN = CONFIG.origin
MINERS = CONFIG.miners
POOL = CONFIG.pool_url
DATABASE = ROOT / "data/dashboard.sqlite"
INTERVAL = 5
MINER_FIELDS = (
    "hostname",
    "version",
    "axeOSVersion",
    "ASICModel",
    "boardVersion",
    "hashRate",
    "hashRate_1m",
    "hashRate_10m",
    "hashRate_1h",
    "expectedHashrate",
    "power",
    "voltage",
    "current",
    "temp",
    "temp2",
    "vrTemp",
    "coreVoltageActual",
    "frequency",
    "actualFrequency",
    "coreVoltage",
    "fanspeed",
    "fanrpm",
    "fan2rpm",
    "sharesAccepted",
    "sharesRejected",
    "sharesRejectedReasons",
    "bestDiff",
    "bestSessionDiff",
    "poolDifficulty",
    "responseTime",
    "uptimeSeconds",
    "wifiRSSI",
    "wifiStatus",
    "power_fault",
    "overheat_mode",
    "miningPaused",
    "poolConnectionInfo",
    "isUsingFallbackStratum",
    "stratumURL",
    "stratumPort",
    "stratumUser",
    "fallbackStratumURL",
    "fallbackStratumPort",
    "fallbackStratumUser",
    "stratumSuggestedDifficulty",
    "stratumTLS",
    "stratumProtocol",
    "autofanspeed",
    "manualFanSpeed",
    "temptarget",
    "resetReason",
    "pool_config_format",
    "primaryPoolIndex",
    "secondaryPoolIndex",
    "supported_actions",
    "workReceived",
    "macAddr",
)
ACTIONS = {"pause", "resume", "restart", "identify", "configure-pool"}


def request(url, method="GET", data=None, headers=None, text=False):
    headers = dict(headers or {})
    if data is not None:
        headers["Content-Type"] = "application/json"
    payload = None if data is None else json.dumps(data).encode()
    with urlopen(
        Request(url, data=payload, headers=headers, method=method), timeout=4
    ) as response:
        body = response.read(4 * 1024 * 1024).decode()
    if text:
        return body
    return json.loads(body) if body.strip() else {}


def bitcoin_rpc(method, params):
    auth = base64.b64encode((ROOT / "data/rpc.cookie").read_bytes()).decode()
    response = request(
        CONFIG.rpc_url,
        "POST",
        {"jsonrpc": "1.0", "id": "dashboard", "method": method, "params": params},
        {"Authorization": "Basic " + auth},
    )
    if response.get("error"):
        raise RuntimeError(response["error"])
    return response["result"]


def bitcoin_status():
    if bitcoin_rpc("getblockhash", [0]) != GENESIS:
        raise RuntimeError("Bitcoin backend has the wrong genesis")
    info = bitcoin_rpc("getblockchaininfo", [])
    info["connections"] = bitcoin_rpc("getconnectioncount", [])
    return info


def miner_status(miner_id, url=None):
    info = request((url or MINERS[miner_id]["url"]) + "/api/system/info")
    for key, value in list(info.items()):
        if isinstance(value, float) and not math.isfinite(value):
            info[key] = None
    info["pool_config_format"] = (
        "array" if isinstance(info.get("pools"), list) else "legacy"
    )
    version = re.search(r"(\d+)\.(\d+)\.(\d+)", info.get("version", ""))
    modern = version and tuple(map(int, version.groups())) >= (2, 14, 0)
    info["supported_actions"] = sorted(
        ACTIONS if modern else {"restart", "configure-pool"}
    )
    for field in ("temp", "temp2", "vrTemp"):
        value = info.get(field)
        if value is not None and (
            not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0
        ):
            info[field] = None
    return {key: info[key] for key in MINER_FIELDS if key in info}


def parse_metrics(text):
    """Keep pool scalars and the latest blocks, without returning the full exposition."""
    values = {}
    blocks = []
    for line in text.splitlines():
        match = re.fullmatch(r"([a-zA-Z_:][a-zA-Z0-9_:]*) ([^ ]+)", line)
        if match:
            value = float(match[2])
            if math.isfinite(value):
                values[match[1]] = value
        if line.startswith("bitcoin_block_found_time_seconds{"):
            labels = dict(re.findall(r'(\w+)="([^"\\]*)"', line))
            labels["timestamp"] = float(line.rsplit(" ", 1)[1])
            blocks.append(labels)
    return {
        "values": values,
        "blocks": sorted(blocks, key=lambda block: block["timestamp"], reverse=True)[
            :8
        ],
    }


def build_alerts(sources, miners=None):
    alerts = []
    for name, source in sources.items():
        if not source["ok"]:
            alerts.append(
                {
                    "level": "warning",
                    "message": f"{name.title()} unavailable: {source['error']}",
                }
            )
    for miner_id in MINERS if miners is None else miners:
        miner = sources.get("miner-" + miner_id, {})
        if not miner.get("ok"):
            continue
        label = f"Bitaxe .{miner_id}"
        info = miner["data"]
        if info.get("power_fault"):
            alerts.append(
                {"level": "danger", "message": f"{label}: {info['power_fault']}"}
            )
        if info.get("temp") is None:
            if info.get("coreVoltageActual", 100) < 100:
                alerts.append(
                    {
                        "level": "warning",
                        "message": f"{label} ASIC is powered down; temperature is unavailable.",
                    }
                )
            else:
                alerts.append(
                    {
                        "level": "warning",
                        "message": f"{label} ASIC temperature sensor is unavailable.",
                    }
                )
        elif info["temp"] >= 80:
            alerts.append(
                {
                    "level": "danger",
                    "message": f"{label} reports {info['temp']:.1f} °C ASIC temperature.",
                }
            )
        if info.get("overheat_mode"):
            alerts.append(
                {"level": "danger", "message": f"{label} overheat mode is active."}
            )
        if "not connected" in info.get("poolConnectionInfo", "").lower():
            alerts.append(
                {
                    "level": "warning",
                    "message": f"{label} is not connected to a Stratum pool.",
                }
            )
    bitcoin = sources.get("bitcoin", {})
    if bitcoin.get("ok") and bitcoin["data"].get("initialblockdownload"):
        alerts.append(
            {"level": "warning", "message": "rbitcoin is still synchronizing."}
        )
    return alerts


class Monitor:
    def __init__(self, database=DATABASE):
        self.database = database
        self.lock = threading.Lock()
        self.action_lock = threading.Lock()
        self.token = secrets.token_urlsafe(32)
        self.settings = json.loads((ROOT / "miner-settings.json").read_text())
        self.state = {"updated_at": None, "sources": {}, "alerts": []}
        with self.connect() as connection:
            connection.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS miner_samples (
                    miner TEXT, timestamp INTEGER, hashrate REAL, temperature REAL, power REAL,
                    accepted REAL, rejected REAL, bitcoin_height INTEGER, share_height INTEGER,
                    PRIMARY KEY (miner, timestamp)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, timestamp INTEGER, level TEXT, message TEXT
                );
            """)
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='samples'"
            ).fetchone():
                connection.execute(
                    "INSERT OR IGNORE INTO miner_samples SELECT '100', * FROM samples"
                )
                connection.execute("DROP TABLE samples")
        self.registry = DeviceRegistry(self.connect, MINERS)
        self.wallet = WalletService(
            ROOT, self.connect, bitcoin_rpc, GENESIS, self.event
        )

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def event(self, level, message):
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO events(timestamp,level,message) VALUES (?,?,?)",
                (int(time.time()), level, message),
            )
            connection.execute(
                "DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 500)"
            )

    def snapshot(self):
        with self.lock:
            state = copy.deepcopy(self.state)
        wallet = self.wallet.cache
        if wallet["balance"] is not None:
            state["sources"]["wallet"] = {
                "ok": self.wallet.error is None,
                "data": wallet["balance"],
                "updated_at": wallet["updated_at"],
                "error": self.wallet.error,
            }
        else:
            state["sources"]["wallet"] = {
                "ok": False,
                "data": None,
                "updated_at": None,
                "error": self.wallet.error or "Waiting for the first wallet sync",
            }
        with self.connect() as connection:
            state["events"] = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM events ORDER BY id DESC LIMIT 20"
                )
            ]
        settings = {
            **self.settings,
            "stratum_host": CONFIG.public_host,
            "stratum_port": CONFIG.stratum_port,
            "explorer_url": f"http://{CONFIG.public_host}:{CONFIG.api_port}/dashboard",
        }
        miners = self.registry.devices()
        now = time.time()
        state.update(
            {
                "token": self.token,
                "settings": settings,
                "miners": miners,
                "fleet": fleet_summary(state["sources"], miners, now),
                "interval": INTERVAL,
                "now": now,
            }
        )
        return state

    def read_miner(self, identifier, url):
        data = miner_status(identifier, url)
        if not self.registry.identify(identifier, data.get("macAddr")):
            raise ValueError(
                "A different or duplicate miner answered at this address; update the device inventory"
            )
        return data

    def collect(self):
        miners = self.registry.devices()
        loaders = {
            "bitcoin": bitcoin_status,
            "chain": lambda: request(POOL + "/chain_info"),
            "peers": lambda: request(POOL + "/peers"),
            "metrics": lambda: parse_metrics(request(POOL + "/metrics", text=True)),
        }
        for miner_id, device in miners.items():
            loaders["miner-" + miner_id] = lambda miner_id=miner_id, url=device["url"]: (
                self.read_miner(miner_id, url)
            )
        with self.lock:
            sources = copy.deepcopy(self.state["sources"])
            old_alerts = self.state["alerts"]
        sources = {
            key: value
            for key, value in sources.items()
            if not key.startswith("miner-") or key[6:] in miners
        }
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {
                name: executor.submit(loader) for name, loader in loaders.items()
            }
            for name, future in futures.items():
                attempted = time.time()
                try:
                    sources[name] = {
                        "ok": True,
                        "data": future.result(),
                        "updated_at": time.time(),
                        "attempted_at": attempted,
                        "error": None,
                    }
                except Exception as error:
                    previous = sources.get(name, {})
                    sources[name] = {
                        "ok": False,
                        "data": previous.get("data"),
                        "updated_at": previous.get("updated_at"),
                        "attempted_at": attempted,
                        "error": str(error),
                    }
        alerts = build_alerts(sources, miners)
        for alert in alerts:
            if alert not in old_alerts:
                self.event(alert["level"], alert["message"])
        for alert in old_alerts:
            if alert not in alerts:
                self.event("info", "Cleared: " + alert["message"])
        timestamp = int(time.time())
        bitcoin = sources["bitcoin"]["data"] if sources["bitcoin"]["ok"] else {}
        chain = sources["chain"]["data"] if sources["chain"]["ok"] else {}
        with self.connect() as connection:
            for miner_id in miners:
                source = sources["miner-" + miner_id]
                miner = source["data"] if source["ok"] else {}
                connection.execute(
                    "INSERT OR REPLACE INTO miner_samples VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        miner_id,
                        timestamp,
                        miner.get("hashRate"),
                        miner.get("temp"),
                        miner.get("power"),
                        miner.get("sharesAccepted"),
                        miner.get("sharesRejected"),
                        bitcoin.get("blocks"),
                        chain.get("chain_tip_height"),
                    ),
                )
            connection.execute(
                "DELETE FROM miner_samples WHERE timestamp < ?",
                (timestamp - 7 * 86400,),
            )
        with self.lock:
            self.state = {"updated_at": timestamp, "sources": sources, "alerts": alerts}

    def run(self):
        while True:
            start = time.monotonic()
            try:
                self.collect()
            except Exception:
                logging.exception("Collection failed")
            time.sleep(max(1, INTERVAL - (time.monotonic() - start)))

    def run_wallet(self):
        while True:
            try:
                self.wallet.balance()
            except Exception:
                logging.warning("Wallet sync unavailable; mining telemetry continues")
            time.sleep(60)

    def history(self, hours, miner_id):
        if miner_id != "fleet" and miner_id not in self.registry.devices():
            raise ValueError("Unknown miner")
        step = max(5, int(hours * 3600 / 720))
        since = int(time.time() - hours * 3600)
        with self.connect() as connection:
            if miner_id == "fleet":
                return [
                    dict(row)
                    for row in connection.execute(
                        """
                    SELECT MAX(timestamp) AS timestamp, AVG(hashrate) AS hashrate,
                           MAX(temperature) AS temperature, AVG(power) AS power,
                           MAX(accepted) AS accepted, MAX(rejected) AS rejected
                    FROM (
                        SELECT timestamp, SUM(hashrate) AS hashrate,
                               MAX(CASE WHEN temperature >= 0 THEN temperature END) AS temperature,
                               SUM(power) AS power, SUM(accepted) AS accepted, SUM(rejected) AS rejected
                        FROM miner_samples WHERE timestamp >= ? GROUP BY timestamp
                    ) GROUP BY CAST(timestamp / ? AS INTEGER) ORDER BY timestamp
                """,
                        (since, step),
                    )
                ]
            return [
                dict(row)
                for row in connection.execute(
                    """
                SELECT MAX(timestamp) AS timestamp, AVG(hashrate) AS hashrate,
                       AVG(CASE WHEN temperature >= 0 THEN temperature END) AS temperature, AVG(power) AS power,
                       MAX(accepted) AS accepted, MAX(rejected) AS rejected
                FROM miner_samples WHERE miner = ? AND timestamp >= ? GROUP BY CAST(timestamp / ? AS INTEGER)
                ORDER BY timestamp
            """,
                    (miner_id, since, step),
                )
            ]

    def action(self, miner_id, action):
        miners = self.registry.devices()
        if miner_id not in miners or action not in ACTIONS:
            raise ValueError("Unknown miner action")
        destination = miners[miner_id]["url"]
        worker = miners[miner_id]["worker"]
        with self.action_lock:
            current = self.read_miner(miner_id, destination)
            if action not in current.get(
                "supported_actions", {"restart", "configure-pool"}
            ):
                raise ValueError("This action is not supported by the device firmware")
            if action == "configure-pool":
                try:
                    advertised = ipaddress.ip_address(CONFIG.public_host)
                    unusable = advertised.is_loopback or advertised.is_unspecified
                except ValueError:
                    unusable = CONFIG.public_host.lower() == "localhost"
                if unusable:
                    raise ValueError(
                        "Set network.public_host to a LAN-reachable node address before configuring a physical miner"
                    )
                backup = (
                    ROOT
                    / "backups"
                    / f"bitaxe-{miner_id}-pool-before-{time.time_ns()}.json"
                )
                with backup.open("x") as output:
                    json.dump(
                        {
                            key: value
                            for key, value in current.items()
                            if "stratum" in key.lower()
                        },
                        output,
                        indent=2,
                    )
                backup.chmod(0o600)
                data = {
                    "stratumURL": CONFIG.public_host,
                    "stratumPort": CONFIG.stratum_port,
                    "stratumUser": self.settings["bitcoin_payout_address"]
                    + "."
                    + worker,
                    "stratumPassword": "x",
                    "stratumSuggestedDifficulty": 1000,
                    "stratumTLS": 0,
                    "stratumProtocol": "SV1",
                    "fallbackStratumURL": CONFIG.public_host,
                    "fallbackStratumPort": CONFIG.stratum_port,
                    "fallbackStratumUser": self.settings["bitcoin_payout_address"]
                    + "."
                    + worker,
                    "fallbackStratumPassword": "x",
                    "fallbackStratumSuggestedDifficulty": 1000,
                    "fallbackStratumTLS": 0,
                    "fallbackStratumProtocol": "SV1",
                }
                expected = {
                    key: data[key]
                    for key in (
                        "stratumURL",
                        "stratumPort",
                        "stratumUser",
                        "fallbackStratumURL",
                        "fallbackStratumPort",
                        "fallbackStratumUser",
                    )
                }
                if current.get("pool_config_format") == "array":
                    pool = {
                        key: value
                        for key, value in data.items()
                        if key.startswith("stratum")
                    }
                    indices = sorted(
                        {
                            current.get("primaryPoolIndex", 0),
                            current.get("secondaryPoolIndex", 1),
                        }
                    )
                    for index in indices:
                        request(destination + f"/api/system/pools/{index}", "PUT", pool)
                else:
                    request(destination + "/api/system", "PATCH", data)
                saved = self.read_miner(miner_id, destination)
                if any(saved.get(key) != value for key, value in expected.items()):
                    raise RuntimeError(
                        "Device did not save the requested pool settings; restart was not requested"
                    )
                message = "Primary and fallback saved for local testnet5. Restart the Bitaxe to apply."
            else:
                result = request(destination + "/api/system/" + action, "POST", {})
                message = result.get("message", f"Bitaxe {action} requested.")
            message = f"Bitaxe .{miner_id}: {message}"
            self.event("info", message)
            return {"message": message}


class Handler(BaseHTTPRequestHandler):
    server_version = "TestnetDashboard/1"

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, format, *args):
        if args and str(args[1]) not in ("200", "304"):
            logging.info(format, *args)

    def reply(self, value, status=200, content_type="application/json"):
        body = (
            json.dumps(value, allow_nan=False).encode()
            if content_type == "application/json"
            else value
        )
        self.send_response(status)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
        )
        try:
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def valid_host(self):
        return self.headers.get("Host") in CONFIG.allowed_hosts

    def do_GET(self):
        if not self.valid_host():
            return self.reply({"error": "Unrecognized dashboard host"}, 403)
        path = urlsplit(self.path)
        try:
            if path.path == "/api/state":
                return self.reply(self.server.monitor.snapshot())
            if path.path == "/api/wallet":
                return self.reply(self.server.monitor.wallet.snapshot())
            if path.path == "/api/devices/discovery":
                return self.reply(self.server.monitor.registry.discovery())
            if path.path == "/api/wallet/qr":
                address = parse_qs(path.query).get("address", [""])[0]
                return self.reply(
                    self.server.monitor.wallet.receive_qr(address),
                    content_type="image/svg+xml",
                )
            if path.path == "/api/history":
                hours = float(parse_qs(path.query).get("hours", ["1"])[0])
                miner_id = parse_qs(path.query).get("miner", [""])[0]
                if not math.isfinite(hours) or hours not in (1, 6, 24, 168):
                    return self.reply({"error": "Choose 1, 6, 24 or 168 hours"}, 400)
                return self.reply(self.server.monitor.history(hours, miner_id))
            if path.path == "/api/logs":
                source = parse_qs(path.query).get("source", ["pool"])[0]
                services = CONFIG.log_units
                if source not in services:
                    return self.reply({"error": "Unknown log source"}, 400)
                scope = ["--user"] if CONFIG.log_scope == "user" else []
                result = subprocess.run(
                    [
                        "journalctl",
                        *scope,
                        "-u",
                        services[source],
                        "--no-pager",
                        "-n",
                        "70",
                        "-o",
                        "short-iso",
                    ],
                    capture_output=True,
                    timeout=5,
                    check=True,
                )
                return self.reply({"text": result.stdout.decode(errors="replace")})
            files = {
                "/": "index.html",
                "/app.js": "app.js",
                "/devices.js": "devices.js",
                "/wallet.js": "wallet.js",
                "/style.css": "style.css",
                "/icon.svg": "icon.svg",
            }
            if path.path in files:
                file = STATIC / files[path.path]
                return self.reply(
                    file.read_bytes(),
                    content_type=mimetypes.guess_type(file)[0] or "text/plain",
                )
            self.reply({"error": "Not found"}, 404)
        except WalletError as error:
            self.reply({"error": str(error)}, error.status)
        except ValueError:
            self.reply({"error": "Invalid request"}, 400)
        except Exception:
            logging.exception("Dashboard request failed")
            self.reply({"error": "Could not load dashboard data"}, 502)

    def do_POST(self):
        if (
            not self.valid_host()
            or self.headers.get("Origin") != "http://" + self.headers.get("Host", "")
            or not secrets.compare_digest(
                self.headers.get("X-Dashboard-Token", ""), self.server.monitor.token
            )
        ):
            return self.reply(
                {"error": "Open the dashboard to use these controls"}, 403
            )
        wallet_actions = {
            "/api/wallet/refresh": lambda data: self.server.monitor.wallet.refresh(),
            "/api/wallet/receive": lambda data: (
                self.server.monitor.wallet.new_address()
            ),
            "/api/wallet/preview": self.server.monitor.wallet.preview,
            "/api/wallet/send": self.server.monitor.wallet.send,
            "/api/devices/discover": lambda data: self.server.monitor.registry.discover(
                data.get("cidr")
            ),
            "/api/devices/add": lambda data: self.server.monitor.registry.add(
                data.get("address"), data.get("name")
            ),
            "/api/devices/remove": lambda data: self.server.monitor.registry.remove(
                data.get("id")
            ),
        }
        if self.path in wallet_actions:
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if (
                    not 0 < length <= 2048
                    or self.headers.get("Content-Type", "").split(";")[0]
                    != "application/json"
                ):
                    return self.reply({"error": "Expected a small JSON request"}, 400)
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    return self.reply({"error": "Expected a JSON object"}, 400)
                return self.reply(wallet_actions[self.path](data))
            except WalletError as error:
                return self.reply({"error": str(error)}, error.status)
            except (ValueError, TypeError) as error:
                return self.reply(
                    {
                        "error": str(error)
                        if self.path.startswith("/api/devices/")
                        else "Invalid wallet request"
                    },
                    400,
                )
            except Exception:
                logging.exception("Wallet request failed")
                return self.reply(
                    {"error": "Wallet operation is currently unavailable."}, 503
                )
        match = re.fullmatch(r"/api/miner/([A-Za-z0-9_-]+)/([a-z-]+)", self.path)
        if (
            not match
            or match[1] not in self.server.monitor.registry.devices()
            or match[2] not in ACTIONS
        ):
            return self.reply({"error": "Unknown miner action"}, 404)
        miner_id, action = match.groups()
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > 1024:
                return self.reply({"error": "Invalid request size"}, 400)
            self.rfile.read(length)
            self.reply(self.server.monitor.action(miner_id, action))
        except Exception as error:
            self.server.monitor.event(
                "warning", f"Bitaxe .{miner_id} {action} failed: {error}"
            )
            self.reply({"error": f"Bitaxe request failed: {error}"}, 502)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if not (ROOT / "miner-settings.json").is_file():
        raise SystemExit(
            "Initialize this lab first: p2pool-lab --config config.toml init"
        )
    monitor = Monitor()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.monitor = monitor
    threading.Thread(target=monitor.run, daemon=True, name="collector").start()
    threading.Thread(target=monitor.run_wallet, daemon=True, name="wallet-sync").start()
    logging.info("Dashboard listening at %s", ORIGIN)
    server.serve_forever()


if __name__ == "__main__":
    main()
