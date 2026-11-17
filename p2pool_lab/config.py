"""Public configuration; wallet keys and RPC credentials belong in runtime state."""

from dataclasses import dataclass, field
import ipaddress
import os
from pathlib import Path
import re
import tomllib
from urllib.parse import urlsplit

GENESIS = "00000000000008c1637e7d3252dc1c333d4546fd6fff6a474b200d674a91d497"


@dataclass(frozen=True)
class Config:
    state_dir: Path = field(
        default_factory=lambda: Path.home() / ".local/share/p2pool-lab"
    )
    config_path: Path | None = None
    bind_host: str = "127.0.0.1"
    public_host: str = "127.0.0.1"
    dashboard_port: int = 8080
    stratum_port: int = 3333
    api_port: int = 46884
    share_peer_port: int = 56886
    bitcoin_rpc_port: int = 18352
    electrum_port: int = 50001
    esplora_port: int = 3002
    health_port: int = 9332
    zmq_port: int = 28335
    log_scope: str = "user"
    log_units: dict = field(
        default_factory=lambda: {
            "pool": "p2pool-lab-pool",
            "bitcoin": "p2pool-lab-bitcoin",
            "bridge": "p2pool-lab-hashblock",
        }
    )
    bitcoin_peers: tuple = ("lapis.erisian.com.au:18353", "tn5.21m.dev:18353")
    miners: dict = field(default_factory=dict)

    @property
    def origin(self):
        return f"http://{self.public_host}:{self.dashboard_port}"

    @property
    def allowed_hosts(self):
        return {
            f"{host}:{self.dashboard_port}"
            for host in (self.public_host, "localhost", "127.0.0.1")
        }

    @property
    def pool_url(self):
        host = "127.0.0.1" if self.bind_host == "0.0.0.0" else self.bind_host
        return f"http://{host}:{self.api_port}"

    @property
    def rpc_url(self):
        return f"http://127.0.0.1:{self.bitcoin_rpc_port}"

    @property
    def zmq_url(self):
        return f"tcp://127.0.0.1:{self.zmq_port}"

    @property
    def wallet_command(self):
        return [
            "bdk-cli",
            "--datadir",
            str(self.state_dir / "data/bdk-testnet5"),
            "--network",
            "testnet4",
            "wallet",
            "--wallet",
            "testnet5-payout",
        ]


def load(path=None):
    requested = path or os.environ.get("P2POOL_LAB_CONFIG")
    config_path = Path(requested or "config.toml").expanduser().resolve()
    if not config_path.exists():
        if requested:
            raise ValueError(f"Configuration not found: {config_path}")
        return Config()
    data = tomllib.loads(config_path.read_text())
    if set(data) - {"runtime", "network", "bitcoin", "miners"}:
        raise ValueError("Unknown configuration section")
    runtime, network, bitcoin = (
        data.get("runtime", {}),
        data.get("network", {}),
        data.get("bitcoin", {}),
    )
    if set(runtime) - {"state_dir", "log_scope", "log_units"} or set(bitcoin) - {
        "peers"
    }:
        raise ValueError("Unknown runtime or bitcoin setting")
    ports = {key for key in Config.__dataclass_fields__ if key.endswith("_port")}
    if set(network) - ports - {"bind_host", "public_host"}:
        raise ValueError("Unknown network setting")
    for name, value in network.items():
        if name in ports and (type(value) is not int or not 1024 <= value <= 65535):
            raise ValueError(f"{name} must be an unprivileged TCP port")
        if name.endswith("host") and (
            not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", value)
        ):
            raise ValueError(f"{name} must be an IPv4 address or hostname")
    if "bind_host" in network:
        try:
            ipaddress.IPv4Address(network["bind_host"])
        except ValueError as error:
            raise ValueError("bind_host must be an IPv4 address") from error
    state = Path(runtime.get("state_dir", "~/.local/share/p2pool-lab")).expanduser()
    if not state.is_absolute():
        state = config_path.parent / state
    if "\n" in str(state) or "\r" in str(state):
        raise ValueError("State directory must be a single-line path")
    scope = runtime.get("log_scope", "user")
    if scope not in ("user", "system"):
        raise ValueError("log_scope must be user or system")
    units = runtime.get("log_units", Config().log_units)
    if (
        not isinstance(units, dict)
        or set(units) != {"pool", "bitcoin", "bridge"}
        or any(
            not isinstance(unit, str) or not re.fullmatch(r"[A-Za-z0-9@_.-]+", unit)
            for unit in units.values()
        )
    ):
        raise ValueError(
            "log_units must name the pool, bitcoin and bridge systemd units"
        )
    peers = bitcoin.get("peers", list(Config().bitcoin_peers))
    if (
        not isinstance(peers, list)
        or not peers
        or any(
            not isinstance(peer, str)
            or not re.fullmatch(r"[A-Za-z0-9.-]+:[0-9]+", peer)
            for peer in peers
        )
    ):
        raise ValueError("Specify at least one bitcoin peer as hostname:port")
    miners = {}
    for identifier, miner in data.get("miners", {}).items():
        if (
            identifier == "fleet"
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", identifier)
            or not isinstance(miner, dict)
        ):
            raise ValueError(
                "Miner identifiers must be short letters, digits, hyphens or underscores"
            )
        if set(miner) - {"name", "url", "worker"}:
            raise ValueError(f"Unknown setting for miner {identifier}")
        url = miner.get("url", "")
        parsed = urlsplit(url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            raise ValueError(
                f"Miner {identifier} needs an HTTP(S) origin without credentials or a path"
            )
        worker = miner.get("worker", identifier)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", worker):
            raise ValueError(f"Invalid worker name for miner {identifier}")
        miners[identifier] = {
            "name": str(miner.get("name", identifier)),
            "url": url.rstrip("/"),
            "worker": worker,
        }
    config = Config(
        state_dir=state.resolve(),
        config_path=config_path,
        log_scope=scope,
        log_units=units,
        bitcoin_peers=tuple(peers),
        miners=miners,
        **network,
    )
    if len({getattr(config, port) for port in ports}) != len(ports):
        raise ValueError("Listener ports must be distinct")
    return config
