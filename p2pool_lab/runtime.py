"""Initialize runtime state with external BDK tools and render node configuration."""

import fcntl
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import tomllib

from embit import script

from .config import GENESIS, POOL_AUTH_FILE, pool_auth_token


def private_write(path, content):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        os.chmod(temporary, 0o600)
        output.write(content)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def ensure_pool_auth(config):
    """Load the P2Pool API credentials, generating them once if missing.

    The credentials are private runtime data: a fixed username, a random
    URL-safe password, a random hex salt, and the HMAC token P2Poolv2 expects.
    Existing credentials are preserved so initialization stays idempotent, and
    owner-only permissions are restored on an existing valid file.
    """
    path = config.state_dir / POOL_AUTH_FILE
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists():
        try:
            data = json.loads(path.read_text())
            if (
                isinstance(data, dict)
                and all(
                    isinstance(data.get(key), str)
                    for key in ("username", "password", "salt", "token")
                )
                and data["token"] == pool_auth_token(data["salt"], data["password"])
            ):
                os.chmod(path, 0o600)
                os.chmod(path.parent, 0o700)
                return data
        except (OSError, ValueError):
            pass
    data = {
        "username": "p2pool-lab",
        "password": secrets.token_urlsafe(32),
        "salt": secrets.token_hex(16),
    }
    data["token"] = pool_auth_token(data["salt"], data["password"])
    private_write(path, json.dumps(data, indent=2) + "\n")
    os.chmod(path.parent, 0o700)
    return data


def run_bdk(arguments, environment=None):
    result = subprocess.run(
        ["bdk-cli", *arguments],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode:
        raise RuntimeError(
            "BDK command failed. Check that the pinned BDK package is on PATH and the wallet state is accessible."
        )
    return json.loads(result.stdout)


def wallet_address(config, name, descriptor_type):
    root = config.state_dir
    backup = root / "backups" / f"{name}-descriptors.json"
    if not backup.exists():
        # Entropy and key generation are supplied by BDK's own descriptor command.
        descriptors = run_bdk(
            ["--network", "testnet4", "descriptor", "--type", descriptor_type]
        )
        private_write(backup, json.dumps(descriptors, indent=2) + "\n")
    descriptors = json.loads(backup.read_text())
    datadir = root / "data/bdk-testnet5"
    base = [
        "--datadir",
        str(datadir),
        "--network",
        "testnet4",
        "wallet",
        "--wallet",
        name,
    ]
    configuration = datadir / "config.toml"
    existing = (
        tomllib.loads(configuration.read_text()) if configuration.exists() else {}
    )
    if name not in existing.get("wallets", {}):
        environment = dict(
            os.environ,
            EXT_DESCRIPTOR=descriptors["private_descriptors"]["external"],
            INT_DESCRIPTOR=descriptors["private_descriptors"]["internal"],
        )
        run_bdk(
            base
            + [
                "config",
                "--client-type",
                "electrum",
                "--database-type",
                "sqlite",
                "--url",
                f"tcp://127.0.0.1:{config.electrum_port}",
                "--genesis-hash",
                GENESIS,
            ],
            environment,
        )
    else:
        saved = existing["wallets"][name]
        if saved.get("genesis_hash") != GENESIS or saved.get("network") != "testnet4":
            raise RuntimeError(
                "Existing wallet belongs to another chain; use a separate state directory."
            )
        if saved.get("server_url") != f"tcp://127.0.0.1:{config.electrum_port}":
            raise RuntimeError(
                "Existing wallet Electrum port differs. Restore the configured port or update that wallet deliberately."
            )
    address_file = root / "data" / f"{name}-address.json"
    if not address_file.exists():
        private_write(
            address_file, json.dumps(run_bdk(base + ["unused_address"])) + "\n"
        )
    return json.loads(address_file.read_text())["address"]


def render_toml(sections):
    lines = []
    for name, values in sections.items():
        lines.append(f"[{name}]")
        lines.extend(f"{key} = {json.dumps(value)}" for key, value in values.items())
        lines.append("")
    return "\n".join(lines)


def render_nodes(config, settings, credential):
    root = config.state_dir
    auth = ensure_pool_auth(config)
    bitcoin = [
        "network=testnet5",
        f"datadir={root / 'data/rbitcoin-testnet5'}",
        *[f"connect={peer}" for peer in config.bitcoin_peers],
        "no_listen=1",
        "milestone=0",
        "sh_index=1",
        "rpc=1",
        f"rpc_listen=127.0.0.1:{config.bitcoin_rpc_port}",
        f"rpc_cookie_file={root / 'data/rpc.cookie'}",
        f"electrum_listen=127.0.0.1:{config.electrum_port}",
        f"esplora_listen=127.0.0.1:{config.esplora_port}",
        f"health_listen=127.0.0.1:{config.health_port}",
        "metrics=1",
        "log_level=info",
    ]
    private_write(root / "rbitcoin.conf", "\n".join(bitcoin) + "\n")
    sections = {
        "network": {
            "listen_address": f"/ip4/{config.bind_host}/tcp/{config.share_peer_port}",
            "dns_seed": True,
            "dial_peers": [
                "/dns4/testnet5.p2poolv2.org/tcp/56886",
                "/dns4/tn5.21m.dev/tcp/56886",
            ],
            "max_pending_incoming": 10,
            "max_pending_outgoing": 10,
            "max_established_incoming": 20,
            "max_established_outgoing": 20,
            "max_established_per_peer": 2,
            "max_workbase_per_second": 10,
            "max_userworkbase_per_second": 10,
            "max_miningshare_per_second": 100,
            "max_inventory_per_second": 100,
            "max_transaction_per_second": 100,
            "max_requests_per_second": 100,
            "dial_timeout_secs": 30,
        },
        "store": {"path": str(root / "data/p2pool-testnet5.db"), "pplns_ttl_days": 7},
        "stratum": {
            "hostname": config.bind_host,
            "port": config.stratum_port,
            "network": "testnet5",
            "mode": "p2poolv2",
            "wait_for_chain_sync": True,
            "start_difficulty": 1000,
            "minimum_difficulty": 100,
            "bootstrap_address": settings["bitcoin_payout_address"],
            "solo_address": settings["bitcoin_payout_address"],
            "miner_address": settings["share_owner_address"],
            "zmqpubhashblock": config.zmq_url,
            "version_mask": "1fffe000",
            "pool_signature": "P2Poolv2",
        },
        "bitcoinrpc": {
            "url": config.rpc_url,
            "username": credential.split(":", 1)[0],
            "password": credential.split(":", 1)[1],
        },
        "logging": {
            "console": True,
            "level": "info",
            "stats_dir": str(root / "logs/stats"),
        },
        "api": {
            "hostname": config.bind_host,
            "port": config.api_port,
            "auth_user": auth["username"],
            "auth_token": auth["token"],
        },
    }
    private_write(root / "config.toml", render_toml(sections))


def ensure_runtime_config(config):
    """Re-render the node configuration for an already-initialized state dir.

    Upgrade path: state directories initialized before P2Pool API credentials
    existed keep an unauthenticated ``[api]`` section until the rendered config
    is refreshed. This runs before every pool launch so the pool never starts
    without authentication. Idempotent, and guarded by the same marker/lock as
    ``initialize`` for cross-process safety.
    """
    root = config.state_dir
    marker = root / ".p2pool-lab"
    settings_path = root / "miner-settings.json"
    cookie = root / "data/rpc.cookie"
    if not (marker.exists() and settings_path.exists() and cookie.exists()):
        raise RuntimeError(
            "State directory is not fully initialized. Run 'p2pool-lab init' first."
        )
    with marker.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            settings = json.loads(settings_path.read_text())
            credential = cookie.read_text()
        except (OSError, ValueError) as error:
            raise RuntimeError(
                "Runtime state is unreadable. Restore backups or re-run "
                "'p2pool-lab init' in a fresh state directory."
            ) from error
        if (
            not isinstance(settings, dict)
            or "bitcoin_payout_address" not in settings
            or "share_owner_address" not in settings
            or ":" not in credential
        ):
            raise RuntimeError(
                "Runtime state is incomplete. Restore backups or re-run "
                "'p2pool-lab init' in a fresh state directory."
            )
        render_nodes(config, settings, credential)
        rendered = tomllib.loads((root / "config.toml").read_text())
        api = rendered.get("api", {})
        if not api.get("auth_user") or not api.get("auth_token"):
            raise RuntimeError("Refusing to start the pool without API authentication.")


def initialize(config):
    os.umask(0o077)
    root = config.state_dir
    marker = root / ".p2pool-lab"
    if root.exists() and any(root.iterdir()) and not marker.exists():
        raise RuntimeError(
            "State directory is nonempty and was not initialized by this app. Choose a new directory."
        )
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with marker.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for directory in (
            "data/rbitcoin-testnet5",
            "backups",
            "logs/stats",
            "services",
        ):
            (root / directory).mkdir(mode=0o700, parents=True, exist_ok=True)
        cookie = root / "data/rpc.cookie"
        if not cookie.exists():
            private_write(cookie, "p2pool:" + secrets.token_hex(32))
        payout = wallet_address(config, "testnet5-payout", "wpkh")
        owner = wallet_address(config, "testnet5-share-owner", "tr")
        output = script.address_to_scriptpubkey(owner).data
        if len(output) != 34 or output[:2] != b"\x51\x20":
            raise RuntimeError("Share-owner wallet did not return a Taproot address")
        encoded = subprocess.run(
            ["p2poolv2_cli", "address", "--network", "testnet4"],
            input=output[2:].hex(),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if not encoded.startswith("tp2pool1"):
            raise RuntimeError("Unexpected share address encoding")
        settings = {
            "url": f"stratum+tcp://{config.public_host}:{config.stratum_port}",
            "username": payout + ".office",
            "password": "x",
            "bitcoin_payout_address": payout,
            "share_owner_address": encoded,
            "share_owner_bitcoin_encoding": owner,
            "genesis_hash": GENESIS,
        }
        private_write(
            root / "miner-settings.json", json.dumps(settings, indent=2) + "\n"
        )
        render_nodes(config, settings, cookie.read_text())
    return settings
