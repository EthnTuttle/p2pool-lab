"""Command-line entry point and user-service installation."""

import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

from .config import GENESIS, load
from .runtime import ensure_runtime_config, initialize, private_write

COMPONENTS = ("bitcoin", "pool", "hashblock", "dashboard")


def interrupt(signum, frame):
    raise KeyboardInterrupt()


def component_command(config, component):
    if component == "bitcoin":
        return ["rbitcoin-node", "--conf", str(config.state_dir / "rbitcoin.conf")]
    if component == "pool":
        return ["p2poolv2", "--config", str(config.state_dir / "config.toml")]
    raise ValueError("Unknown native component")


def run_stack(config):
    from .dashboard import bitcoin_rpc

    executable = os.environ.get("P2POOL_LAB_EXECUTABLE") or shutil.which("p2pool-lab")
    if not executable:
        raise RuntimeError(
            "Run the stack through its installed entry point, such as nix run . -- run"
        )
    children = []
    signal.signal(signal.SIGTERM, interrupt)
    try:
        bitcoin = subprocess.Popen(
            component_command(config, "bitcoin"), start_new_session=True
        )
        children.append(bitcoin)
        deadline = time.monotonic() + 120
        while True:
            if bitcoin.poll() is not None:
                raise RuntimeError("rbitcoin exited before its RPC became ready")
            try:
                if bitcoin_rpc("getblockhash", [0]) != GENESIS:
                    raise RuntimeError("Wrong Bitcoin genesis")
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "rbitcoin RPC did not become ready within two minutes"
                    )
                time.sleep(1)
        children.append(
            subprocess.Popen(component_command(config, "pool"), start_new_session=True)
        )
        for name in ("hashblock", "dashboard"):
            children.append(
                subprocess.Popen(
                    [executable, name],
                    start_new_session=True,
                )
            )
        while all(child.poll() is None for child in children):
            time.sleep(1)
        raise RuntimeError("A lab component exited; inspect its preceding log messages")
    finally:
        for child in reversed(children):
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
        for child in reversed(children):
            try:
                child.wait(timeout=60)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()


def unit_quote(value):
    return json.dumps(str(value).replace("%", "%%"))


def install_user_services(config, start=False):
    if not config.config_path:
        raise RuntimeError(
            "Service installation requires an explicit configuration file"
        )
    executable = os.environ.get("P2POOL_LAB_EXECUTABLE") or shutil.which("p2pool-lab")
    if not executable:
        raise RuntimeError("Run service installation using nix run . -- ...")
    executable = str(Path(executable).resolve())
    units = []
    for name in COMPONENTS:
        unit = config.state_dir / "services" / f"p2pool-lab-{name}.service"
        content = f"""[Unit]
Description=P2Pool Lab {name}
After=network-online.target
Wants=network-online.target

[Service]
ExecStart={unit_quote(executable)} --config {unit_quote(config.config_path)} {name}
Restart=on-failure
RestartSec=10
TimeoutStopSec=120
UMask=0077
LimitNOFILE=65536
NoNewPrivileges=true

[Install]
WantedBy=default.target
"""
        private_write(unit, content)
        units.append(str(unit))
    subprocess.run(["systemctl", "--user", "link", *units], check=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    arguments = ["systemctl", "--user", "enable"] + (["--now"] if start else [])
    subprocess.run(
        [*arguments, *[f"p2pool-lab-{name}.service" for name in COMPONENTS]], check=True
    )


def main():
    parser = argparse.ArgumentParser(
        description="Run a private Bitcoin testnet5 mining lab"
    )
    parser.add_argument(
        "--config", help="Public TOML configuration (or P2POOL_LAB_CONFIG)"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "run", "status", *COMPONENTS):
        commands.add_parser(name)
    services = commands.add_parser("install-user-services")
    services.add_argument("--start", action="store_true")
    commands.add_parser("wallet").add_argument("arguments", nargs=argparse.REMAINDER)
    commands.add_parser("rpc").add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        config = load(args.config)
        if config.config_path:
            os.environ["P2POOL_LAB_CONFIG"] = str(config.config_path)
        os.umask(0o077)
        if args.command == "init":
            print(json.dumps(initialize(config), indent=2))
            print(f"Back up {config.state_dir / 'backups'}", file=sys.stderr)
            return
        if args.command == "wallet":
            os.execvp("bdk-cli", [*config.wallet_command, *args.arguments])
        if args.command == "rpc":
            os.execvp(
                "rbitcoin-cli",
                [
                    "rbitcoin-cli",
                    "--network",
                    "testnet5",
                    "--datadir",
                    str(config.state_dir / "data/rbitcoin-testnet5"),
                    *args.arguments,
                ],
            )
        if not (config.state_dir / "miner-settings.json").exists():
            raise RuntimeError("Run init first")
        if args.command in ("pool", "run", "dashboard", "status"):
            # Upgrade path: refresh the rendered pool config (adding API
            # authentication to older state dirs) before the pool can start.
            ensure_runtime_config(config)
        if args.command in ("bitcoin", "pool"):
            command = component_command(config, args.command)
            os.execvp(command[0], command)
        if args.command == "dashboard":
            from .dashboard import main as dashboard

            dashboard()
        elif args.command == "hashblock":
            from .hashblock import main as hashblock

            hashblock()
        elif args.command == "install-user-services":
            install_user_services(config, args.start)
        elif args.command == "run":
            run_stack(config)
        elif args.command == "status":
            from .dashboard import bitcoin_status, request

            print(
                json.dumps(
                    {
                        "bitcoin": bitcoin_status(),
                        "pool": request(
                            config.pool_url + "/chain_info",
                            headers=config.pool_auth_headers,
                        ),
                        "dashboard": config.origin,
                    },
                    indent=2,
                )
            )
    except KeyboardInterrupt:
        raise SystemExit(130)
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, f"p2pool-lab: {error}\n")


if __name__ == "__main__":
    main()
