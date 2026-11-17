# P2Pool Lab

A self-hosted **Bitcoin testnet5** mining lab: rbitcoin, P2Poolv2, a BDK wallet,
and a local multi-Axe dashboard. Linux, with Nix-pinned source builds.

Source: [EthnTuttle/p2pool-lab](https://github.com/EthnTuttle/p2pool-lab)

## What you get

- A validating rbitcoin node and a P2Poolv2 Stratum v1 endpoint.
- Fleet totals and per-miner charts for hashrate, power, and temperature.
- LAN discovery, adding/removing miners, and persistent device inventory.
- Individual AxeOS controls, adapted to the connected firmware version.
- Bitcoin wallet receive addresses/QR codes, payment previews, signing,
  broadcasting, and transaction history.
- A small RPC-to-ZMQ adapter for rbitcoin's block notifications.
- Foreground operation, user systemd units, and an optional NixOS module.

The fleet view sums hashrate and power and shows the **hottest** ASIC, rather than
averaging away a hot miner. Offline/stale devices are excluded and the reporting
count stays visible. Share counts are the sum of current device sessions; they
can decrease when a miner reboots. Fleet totals cover all monitored miners,
including miners pointed at other pools. Local P2Pool metrics are shown separately.

## Quick start

Install Nix with `nix-command` and `flakes` enabled, then:

```sh
git clone https://github.com/EthnTuttle/p2pool-lab.git
cd p2pool-lab
cp config.example.toml config.toml
# Edit config.toml for your host. Defaults use loopback and monitor no miners.
nix build
nix run . -- --config config.toml init
nix run . -- --config config.toml run
```

Open **http://127.0.0.1:8080/**. `init` prints the Stratum settings and backup
location. Allow rbitcoin and then the P2Pool share chain to synchronize before
connecting a miner. `Ctrl-C` stops the foreground stack.

For LAN access, set both `network.bind_host` and `network.public_host` to your
host's LAN IPv4 address. If listening on `0.0.0.0`, set `public_host` to the
address or hostname your browser and miners actually use. Use a DHCP reservation.
`public_host` is also checked for dashboard browser origins.

Configure miner firmware with the Stratum host/port printed by `init`, your
Bitcoin payout address as the username, and password `x`. The independent share
owner is configured on the pool node. Default starting difficulty is 1,000.

All runtime state defaults to **`~/.local/share/p2pool-lab`**, outside the checkout.
The initializer calls BDK's existing descriptor generator for two independent
wallets. It does not implement entropy generation or private-key derivation.
Repeated initialization preserves existing wallets and RPC credentials.

### Keep it running

```sh
nix run . -- --config config.toml install-user-services --start
systemctl --user status p2pool-lab-{bitcoin,pool,hashblock,dashboard}
journalctl --user -u p2pool-lab-dashboard -f
```

Do not run the foreground stack and systemd stack on the same state/ports at once.
The generated units retain the Nix package path used during installation. After
updating the flake, rerun service installation and restart the units. Services
need their home/state storage mounted; enable user lingering separately if you
want operation without a logged-in session.

### Firewall

Open the chosen Stratum/dashboard/explorer TCP ports to your LAN. For example,
adapt this to your actual interface, subnet, address, and ports:

```sh
sudo ufw allow in on <interface> proto tcp from <LAN-CIDR> to <node-LAN-IP> port 3333,8080,46884 comment 'P2Pool LAN'
```

A connection from the node to itself does not test inbound firewall access.
Verify a real miner connects and accepted shares increase. RPC, Electrum, Esplora,
health, and ZMQ listeners remain on loopback.

## Multi-Axe dashboard

The default **All miners** view combines fleet telemetry. Select any miner for
its own graphs, firmware details, pool destination, and controls.

Under **Discover & manage miners**:

1. Enter your private IPv4 subnet, `/24` or smaller, and click **Discover**.
2. The scan makes bounded, read-only HTTP requests to AxeOS port 80.
3. Click **Add miner**, or enter an IP manually.

Discovery does not follow redirects or scan public networks. It does not change
miner settings. Device identity is tracked by MAC address to deduplicate adds and
preserve a miner's history when explicitly re-added at a new DHCP address.
Removing a miner stops monitoring, frees its address in the inventory, and keeps
its historical samples until normal retention expires.

Device inventory and seven days of samples are stored in `data/dashboard.sqlite`.
Optional `[miners.<id>]` entries seed the inventory at startup. Dashboard-added
devices do not need to be written back into the public config. Removed configured
devices stay removed across restarts; adding them again re-enables monitoring.

## Wallet

Open the **Wallet** section. It uses BDK and the local rbitcoin Electrum server.
Testnet5 shares testnet address encoding, so BDK uses `network = "testnet4"` plus
the pinned, independent testnet5 genesis hash:

```
00000000000008c1637e7d3252dc1c333d4546fd6fff6a474b200d674a91d497
```

New receive addresses do not change the miner's fixed payout address. Mining
rewards mature after 100 Bitcoin confirmations. Payment previews show the exact
fee and expire after ten minutes. Signing happens only when the reviewed payment
is sent. Signed bytes are persisted before broadcast, so retries reuse the same
transaction rather than creating another payment.

Back up the runtime **`backups/`** directory to your own backup storage. It contains
private mnemonics/descriptors. `data/bdk-testnet5/` contains wallet configuration
and SQLite state. Never add these to the repository or a Nix derivation.

The share-owner wallet controls a separate P2Pool address. The Bitcoin wallet UI
does not implement share-chain spending or atomic swaps.

```sh
nix run . -- --config config.toml wallet balance
nix run . -- --config config.toml wallet full_scan
nix run . -- --config config.toml rpc getblockchaininfo
nix run . -- --config config.toml status
```

## Access model

The default dashboard is loopback-only. Anyone you allow to access the dashboard
can operate its miner controls **and testnet wallet**. Mutations check the Host,
same-origin browser Origin, and a dashboard token; these checks prevent
cross-origin requests but are not user authentication. Put authenticated remote
access in front of it if you share it beyond trusted local operators.

Wallet private descriptors and RPC credentials are runtime files with owner-only
permissions. Dashboard responses contain public metadata, never private keys.

## Nix and reproducibility

`flake.lock` fixes nixpkgs and the exact source trees of rbitcoin, P2Poolv2, and
BDK CLI. Each Rust package uses its pinned `Cargo.lock`. The BDK changes are
included as a reviewable patch, not fetched from a floating branch. Python and
native dependencies are pinned through nixpkgs; embit additionally has a fixed
source hash. Nodes are built from source, not downloaded release executables.

```sh
nix build .#dashboard   # Python app and tests, without the native node builds
nix build .#rbitcoin .#p2pool .#bdk
nix flake check
nix develop -c python -m unittest discover -s tests -p 'test_*.py' -v
```

Definitions cover `x86_64-linux` and `aarch64-linux`. See `VALIDATION.md` for the
checks actually performed. A pinned hermetic build is not a claim that independent
builders have already demonstrated byte-for-byte agreement. Chain state, live
peer availability, and generated wallet keys are intentionally runtime inputs.

Nix source filtering includes only application code, static assets, tests, and
package metadata. It excludes runtime state and Python bytecode even if someone
runs development tools in the checkout.

### NixOS

Add this flake as an input and import `inputs.p2pool-lab.nixosModules.default`:

```nix
{
  services.p2pool-lab = {
    enable = true;
    settings.network = {
      bind_host = "127.0.0.1";
      public_host = "127.0.0.1";
    };
  };
}
```

The module runs as `p2pool-lab`, uses `/var/lib/p2pool-lab`, and initializes private
state at service startup. `settings` is public TOML in the Nix store: never put
wallet material or passwords there. The module does not open firewall ports.

## Development / publishing

See `CONTRIBUTING.md`, `NOTICE`, and `SECURITY.md`. This is an independent starter,
not an official release of any upstream project. The repository contains no
operator-specific wallets, mining addresses, machine paths, or node databases.

License: MIT for this application; upstream projects retain their own licenses.
