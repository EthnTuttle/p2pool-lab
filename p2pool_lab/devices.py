"""Persistent AxeOS device inventory and bounded, read-only LAN discovery."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from http.client import HTTPConnection, HTTPException, HTTPResponse, HTTPSConnection
import ipaddress
import json
import logging
import math
import re
import socket
import sqlite3
import threading
import time
from urllib.error import HTTPError
from urllib.request import (
    HTTPHandler,
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)

SQLITE_INT_MIN, SQLITE_INT_MAX = -(2**63), 2**63 - 1
MAX_TELEMETRY = 1e15
PROBE_TIMEOUT = 2
DISCOVERY_GRACE = 1

PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        response.close()
        raise HTTPError(
            request.full_url,
            code,
            "Redirects are not followed",
            headers,
            response,
        )


class DeadlineHTTPResponse(HTTPResponse):
    """Close the per-connection watchdog as soon as the body is consumed."""

    _deadline_connection = None

    def _close_conn(self):
        try:
            super()._close_conn()
        finally:
            connection = self._deadline_connection
            self._deadline_connection = None
            if connection is not None:
                connection.cancel_deadline()


class DeadlineHTTPConnection(HTTPConnection):
    """HTTP connection whose daemon watchdog interrupts all socket phases."""

    response_class = DeadlineHTTPResponse

    def __init__(self, *args, deadline=None, **kwargs):
        self._absolute_deadline = deadline
        self._deadline_timer = None
        self._deadline_expired = False
        super().__init__(*args, **kwargs)
        self._create_connection = self._deadline_create_connection

    def _deadline_create_connection(self, address, timeout, source_address):
        """Connect without DNS; lab HTTP targets must be numeric IP literals."""
        host, port = address
        try:
            ipaddress.ip_address(host)
        except ValueError as error:
            raise OSError(
                "Outbound HTTP targets must use numeric IP addresses; DNS names are not resolved"
            ) from error
        last_error = None
        # AI_NUMERICHOST makes resolution a numeric parse only. No resolver can
        # escape the absolute deadline because hostname targets were rejected.
        for family, kind, proto, _, sockaddr in socket.getaddrinfo(
            host, port, 0, socket.SOCK_STREAM, flags=socket.AI_NUMERICHOST
        ):
            sock = None
            try:
                remaining = self._absolute_deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Outbound connection exceeded its deadline")
                effective_timeout = remaining
                if (
                    timeout is not None
                    and timeout is not socket._GLOBAL_DEFAULT_TIMEOUT
                ):
                    effective_timeout = min(timeout, remaining)
                sock = socket.socket(family, kind, proto)
                self.sock = sock
                sock.settimeout(effective_timeout)
                if source_address:
                    sock.bind(source_address)
                sock.connect(sockaddr)
                return sock
            except OSError as error:
                if sock is not None:
                    sock.close()
                    if self.sock is sock:
                        self.sock = None
                if (
                    self._deadline_expired
                    or time.monotonic() >= self._absolute_deadline
                ):
                    raise TimeoutError(
                        "Outbound connection exceeded its deadline"
                    ) from error
                last_error = error
        if last_error is not None:
            raise last_error
        raise OSError("getaddrinfo returned no connection candidates")

    def _expire(self):
        self._deadline_expired = True
        sock = self.sock
        if sock is not None:
            try:
                # shutdown wakes another thread blocked in header/body reads;
                # close alone is not reliable while SocketIO holds a reference.
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.close()

    def cancel_deadline(self):
        timer = self._deadline_timer
        self._deadline_timer = None
        if timer is not None:
            timer.cancel()

    def connect(self):
        deadline = self._absolute_deadline
        if deadline is None:
            deadline = time.monotonic() + self.timeout
            self._absolute_deadline = deadline
        timer = threading.Timer(max(0, deadline - time.monotonic()), self._expire)
        timer.daemon = True
        self._deadline_timer = timer
        timer.start()
        try:
            # Numeric-only addressing avoids resolver waits. TCP, TLS, headers
            # and bodies remain interruptible by the absolute-deadline watchdog.
            super().connect()
            if self._deadline_expired or time.monotonic() >= deadline:
                self._expire()
                raise TimeoutError("Outbound connection exceeded its deadline")
        except BaseException:
            self.cancel_deadline()
            self.close()
            raise

    def request(self, *args, **kwargs):
        try:
            return super().request(*args, **kwargs)
        except BaseException:
            self.cancel_deadline()
            self.close()
            raise

    def getresponse(self):
        try:
            response = super().getresponse()
        except BaseException:
            self.cancel_deadline()
            self.close()
            raise
        response._deadline_connection = self
        return response


class DeadlineHTTPSConnection(DeadlineHTTPConnection, HTTPSConnection):
    """TLS variant; HTTPSConnection wraps the watchdog-managed raw socket."""


class DeadlineHTTPHandler(HTTPHandler):
    def http_open(self, request):
        return self.do_open(
            DeadlineHTTPConnection,
            request,
            deadline=getattr(request, "_p2pool_deadline", None),
        )


class DeadlineHTTPSHandler(HTTPSHandler):
    def https_open(self, request):
        return self.do_open(
            DeadlineHTTPSConnection,
            request,
            deadline=getattr(request, "_p2pool_deadline", None),
        )


def deadline_opener():
    return build_opener(
        ProxyHandler({}), NoRedirect(), DeadlineHTTPHandler(), DeadlineHTTPSHandler()
    )


def open_response(opener, request, timeout, deadline):
    request._p2pool_deadline = deadline
    try:
        return opener.open(request, timeout=timeout)
    except HTTPError as error:
        error.close()
        raise


def finite_number(value):
    """Return a finite value bounded for JSON, SQLite and fleet aggregates."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, int):
        return value if SQLITE_INT_MIN <= value <= SQLITE_INT_MAX else None
    try:
        return value if math.isfinite(value) and abs(value) <= MAX_TELEMETRY else None
    except OverflowError:
        return None


def safe_text(value, limit=256):
    """Return a bounded UTF-8 string, or None for arbitrary containers/types."""
    if not isinstance(value, str):
        return None
    return value.encode("utf-8", errors="replace").decode()[:limit]


def read_response(response, deadline, limit):
    """Read a bounded body without allowing a slow drip to exceed a deadline."""
    chunks = []
    size = 0
    read = getattr(response, "read1", response.read)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Outbound request exceeded its deadline")
        # urllib's HTTPResponse exposes its socket here. Reducing its timeout
        # also bounds a stalled read; read1 returns each available chunk so a
        # slow drip reaches the monotonic checks below.
        sock = getattr(
            getattr(getattr(response, "fp", None), "raw", None), "_sock", None
        )
        if sock is not None:
            sock.settimeout(remaining)
        chunk = read(min(64 * 1024, limit - size + 1))
        if time.monotonic() > deadline:
            raise TimeoutError("Outbound request exceeded its deadline")
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if size > limit:
            raise ValueError("Outbound response is too large")
    return b"".join(chunks)


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
        deadline = time.monotonic() + PROBE_TIMEOUT
        opener = deadline_opener()
        request = Request(f"http://{address}/api/system/info")
        with open_response(opener, request, PROBE_TIMEOUT, deadline) as response:
            info = json.loads(read_response(response, deadline, 128 * 1024))
        model = safe_text(info.get("ASICModel"), 64) if isinstance(info, dict) else None
        if not model or not model.strip():
            return None
        mac_value = info.get("macAddr")
        mac = mac_value.lower().replace(":", "") if isinstance(mac_value, str) else ""
        if not re.fullmatch(r"[0-9a-f]{12}", mac) or "hashRate" not in info:
            return None
        return {
            "address": address,
            "mac": mac,
            "name": safe_text(info.get("hostname"), 64) or "Bitaxe",
            "model": model,
            "version": safe_text(info.get("version"), 32) or "unknown",
        }
    except (HTTPException, OSError, ValueError, TypeError, RecursionError):
        return None


class DeviceRegistry:
    def __init__(self, connect, configured):
        self.connect = connect
        self.lock = threading.Lock()
        self.executor = ThreadPoolExecutor(max_workers=16)
        self._probe_futures = set()
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
                try:
                    connection.execute(
                        """INSERT INTO devices(id,name,url,worker) VALUES (?,?,?,?)
                        ON CONFLICT(id) DO UPDATE SET name=excluded.name,url=CASE WHEN devices.enabled=1 THEN excluded.url ELSE devices.url END,worker=excluded.worker""",
                        (identifier, device["name"], device["url"], device["worker"]),
                    )
                except sqlite3.IntegrityError:
                    # A dashboard-added device may already own this URL. Keep
                    # its inventory and continue importing the remaining rows.
                    logging.warning(
                        "Configured miner %s conflicts with an existing device; skipping it",
                        identifier,
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
        futures = []
        try:
            with self.lock:
                unfinished = any(not future.done() for future in self._probe_futures)
                self._probe_futures = {
                    future for future in self._probe_futures if future.done()
                }
            if unfinished:
                # A non-network callable may be uninterruptible. Do not queue a
                # second scan behind it; real probes have socket watchdogs.
                raise TimeoutError("Previous discovery probes are still stopping")
            futures = [self.executor.submit(probe, address) for address in addresses]
            self._probe_futures = set(futures)
            waves = max(1, (len(addresses) + 15) // 16)
            deadline = time.monotonic() + waves * PROBE_TIMEOUT + DISCOVERY_GRACE
            for future in as_completed(
                futures, timeout=max(0, deadline - time.monotonic())
            ):
                try:
                    found = future.result()
                except Exception:
                    logging.debug("A discovery probe failed", exc_info=True)
                    found = None
                with self.lock:
                    self.scan["checked"] += 1
                    if found:
                        self.scan["results"].append(found)
        except Exception:
            logging.debug("Discovery did not finish cleanly", exc_info=True)
            with self.lock:
                self.scan["error"] = "Discovery could not complete"
        finally:
            for future in futures:
                future.cancel()
            with self.lock:
                self.scan["running"] = False
                self.scan["finished_at"] = int(time.time())


def fleet_summary(sources, devices, now):
    live = [
        sources["miner-" + identifier]["data"]
        for identifier in devices
        if sources.get("miner-" + identifier, {}).get("ok")
        and isinstance(sources["miner-" + identifier].get("data"), dict)
        and now - sources["miner-" + identifier]["updated_at"] < 35
    ]

    def values(key):
        readings = (finite_number(item.get(key)) for item in live)
        return [value for value in readings if value is not None]

    def total(key):
        items = values(key)
        if not items:
            return None
        try:
            return finite_number(sum(items))
        except OverflowError:
            return None

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
