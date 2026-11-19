"""Persistent AxeOS device inventory and bounded, read-only LAN discovery."""

from concurrent.futures import ThreadPoolExecutor, as_completed
import ipaddress
import json
import math
import re
import threading
import time
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        raise HTTPError(
            request.full_url,
            code,
            "Redirects are not followed",
            headers,
            response,
        )


def private_address(value):
    try:
        address = ipaddress.IPv4Address(value)
    except (ValueError, TypeError) as error:
        raise ValueError("Enter the miner's private IPv4 address") from error
    if not any(address in network for network in PRIVATE_NETWORKS):
        raise ValueError(
            "Discovery and adding miners are limited to private LAN addresses"
        )
    return str(address)


def scan_network(value):
    try:
        network = ipaddress.IPv4Network(value, strict=False)
    except (ValueError, TypeError) as error:
        raise ValueError(
            "Enter a private IPv4 subnet, such as 192.168.1.0/24"
        ) from error
    if network.prefixlen < 24 or not any(
        network.subnet_of(allowed) for allowed in PRIVATE_NETWORKS
    ):
        raise ValueError("Scan one private subnet at a time, /24 or smaller")
    return network


def probe(address):
    address = private_address(address)
    try:
        opener = build_opener(ProxyHandler({}), NoRedirect())
        with opener.open(f"http://{address}/api/system/info", timeout=2) as response:
            info = json.loads(response.read(128 * 1024))
        if (
            not isinstance(info, dict)
            or not isinstance(info.get("ASICModel"), str)
            or not info["ASICModel"].strip()
        ):
            return None
        mac = str(info.get("macAddr", "")).lower().replace(":", "")
        if not re.fullmatch(r"[0-9a-f]{12}", mac) or "hashRate" not in info:
            return None
        return {
            "address": address,
            "mac": mac,
            "name": str(info.get("hostname", "Bitaxe"))[:64],
            "model": info["ASICModel"],
            "version": str(info.get("version", "unknown"))[:32],
        }
    except (OSError, ValueError, TypeError):
        return None


class DeviceRegistry:
    def __init__(self, connect, configured):
        self.connect = connect
        self.lock = threading.Lock()
        self.scan = {
            "running": False,
            "checked": 0,
            "total": 0,
            "results": [],
            "cidr": None,
            "error": None,
        }
        with self.connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS devices (id TEXT PRIMARY KEY, name TEXT, url TEXT UNIQUE, worker TEXT, mac TEXT UNIQUE, enabled INTEGER NOT NULL DEFAULT 1)"
            )
            for identifier, device in configured.items():
                connection.execute(
                    """INSERT INTO devices(id,name,url,worker) VALUES (?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET name=excluded.name,url=CASE WHEN devices.enabled=1 THEN excluded.url ELSE devices.url END,worker=excluded.worker""",
                    (identifier, device["name"], device["url"], device["worker"]),
                )

    def devices(self):
        with self.connect() as connection:
            return {
                row["id"]: {key: row[key] for key in ("name", "url", "worker", "mac")}
                for row in connection.execute(
                    "SELECT * FROM devices WHERE enabled=1 ORDER BY id"
                )
            }

    def identify(self, identifier, mac):
        normalized = str(mac or "").lower().replace(":", "")
        valid = re.fullmatch(r"[0-9a-f]{12}", normalized) is not None
        with self.connect() as connection:
            device = connection.execute(
                "SELECT mac FROM devices WHERE id=?", (identifier,)
            ).fetchone()
            if device is None:
                return False
            if not valid:
                # A missing or malformed MAC cannot confirm an entry that is
                # already pinned. Only unpinned entries may be adopted later.
                return device["mac"] is None
            owner = connection.execute(
                "SELECT id FROM devices WHERE mac=?", (normalized,)
            ).fetchone()
            if owner is not None and owner["id"] != identifier:
                return False
            return (
                connection.execute(
                    "UPDATE devices SET mac=? WHERE id=? AND (mac IS NULL OR mac=?)",
                    (normalized, identifier, normalized),
                ).rowcount
                == 1
            )

    def add(self, address, name=None):
        address = private_address(address)
        info = probe(address)
        if not info:
            raise ValueError(
                "No responding AxeOS API found at that address. Check the miner's web page and try again."
            )
        if name is not None and (
            not isinstance(name, str) or not 1 <= len(name.strip()) <= 64
        ):
            raise ValueError("Name must be 1–64 characters")
        url = f"http://{address}"
        with self.lock, self.connect() as connection:
            by_mac = connection.execute(
                "SELECT * FROM devices WHERE mac=?", (info["mac"],)
            ).fetchone()
            by_url = connection.execute(
                "SELECT * FROM devices WHERE url=?", (url,)
            ).fetchone()
            if by_url and by_url["mac"] not in (None, info["mac"]):
                raise ValueError("This address is registered to a different miner")
            if by_mac and by_url and by_mac["id"] != by_url["id"]:
                raise ValueError(
                    "Remove the conflicting entry for this address before adding the moved miner"
                )
            existing = by_mac or by_url
            identifier = existing["id"] if existing else "axe-" + info["mac"]
            label = (
                name.strip() if name else existing["name"] if existing else info["name"]
            )
            worker = existing["worker"] if existing else "axe-" + info["mac"][-6:]
            connection.execute(
                """INSERT INTO devices(id,name,url,worker,mac,enabled) VALUES (?,?,?,?,?,1)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name,url=excluded.url,mac=excluded.mac,enabled=1""",
                (identifier, label, url, worker, info["mac"]),
            )
        return {"id": identifier, "name": label, "url": url, "worker": worker}

    def remove(self, identifier):
        with self.connect() as connection:
            if (
                connection.execute(
                    "UPDATE devices SET enabled=0,url=NULL WHERE id=? AND enabled=1",
                    (identifier,),
                ).rowcount
                != 1
            ):
                raise ValueError("Miner is not registered")
        return {
            "message": "Miner removed from monitoring; its saved history is retained."
        }

    def discovery(self):
        with self.lock:
            result = {
                **self.scan,
                "results": [dict(item) for item in self.scan["results"]],
            }
        registered = self.devices()
        urls = {device["url"] for device in registered.values()}
        for item in result["results"]:
            item["registered"] = f"http://{item['address']}" in urls
        return result

    def discover(self, cidr):
        network = scan_network(cidr)
        addresses = [str(address) for address in network.hosts()]
        with self.lock:
            if self.scan["running"]:
                raise ValueError("A discovery scan is already running")
            self.scan = {
                "running": True,
                "checked": 0,
                "total": len(addresses),
                "results": [],
                "cidr": str(network),
                "error": None,
            }
        threading.Thread(
            target=self._scan, args=(addresses,), daemon=True, name="axe-discovery"
        ).start()
        return self.discovery()

    def _scan(self, addresses):
        try:
            with ThreadPoolExecutor(max_workers=16) as executor:
                futures = [executor.submit(probe, address) for address in addresses]
                for future in as_completed(futures):
                    found = future.result()
                    with self.lock:
                        self.scan["checked"] += 1
                        if found:
                            self.scan["results"].append(found)
        except Exception:
            with self.lock:
                self.scan["error"] = "Discovery could not complete"
        finally:
            with self.lock:
                self.scan["running"] = False
                self.scan["finished_at"] = int(time.time())


def fleet_summary(sources, devices, now):
    live = [
        sources["miner-" + identifier]["data"]
        for identifier in devices
        if sources.get("miner-" + identifier, {}).get("ok")
        and now - sources["miner-" + identifier]["updated_at"] < 35
    ]

    def values(key):
        return [
            item[key]
            for item in live
            if isinstance(item.get(key), (int, float))
            and not isinstance(item[key], bool)
            and math.isfinite(item[key])
        ]

    def total(key):
        items = values(key)
        return sum(items) if items else None

    temperatures = values("temp")
    return {
        "configured": len(devices),
        "reporting": len(live),
        "hashRate": total("hashRate"),
        "power": total("power"),
        "temp": max(temperatures) if temperatures else None,
        "sharesAccepted": total("sharesAccepted"),
        "sharesRejected": total("sharesRejected"),
    }
