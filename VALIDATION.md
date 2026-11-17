# Validation record

Environment: Linux x86_64, Nix 2.33.1. Date: 2026-11-16.

## Completed

- Python application tests: 39 passing, including payment replay/concurrency,
  history migration, fleet aggregation, persistent inventory, duplicate adds,
  private-subnet restrictions, and firmware-aware action routing.
- Nix-packaged dashboard builds successfully; its tests run during the build.
- Flake outputs evaluate for x86_64-linux and aarch64-linux.
- The NixOS module evaluation check passes.
- Python static checks pass and Gitleaks reports no secrets in the starter tree.
- The portable dashboard is deployed against an existing live testnet5 lab.
- Browser checks pass for desktop and phone layouts, fleet/per-device selection,
  wallet receive/preview flows, and LAN discovery.
- A live discovery scan found two AxeOS devices. Adding an existing device did
  not create a duplicate.
- Existing BDK software signing was verified using a synthetic input; no test
  payment was broadcast. The wallet UI's broadcast behavior is tested with mocks.
- Fresh-state initialization preserves wallets and RPC credentials on a second
  run. The Nix-packaged launcher was exercised with revision-matched, locally
  built native executables on isolated ports: Bitcoin synced, the P2Pool share
  chain synced, and Stratum subscribe/authorize/job delivery passed. The temporary
  stack then shut down cleanly. This is distinct from validating the native Nix
  derivations below.

## Native Nix validation completed

- `nix build .#rbitcoin .#p2pool .#bdk` passes on x86_64-linux.
- `nix flake check` passes, including Python tests, BDK binary unit tests, and
  NixOS module evaluation.
- `nix build` produces the assembled runtime and its wrapped CLI.
- The assembled runtime was launched with an otherwise empty environment and
  only its Nix-provided executables. On isolated test state/ports, Bitcoin RPC,
  Stratum subscription/authorization/job delivery, P2Pool explorer assets, the
  dashboard, and BDK wallet loading passed. Its temporary processes were stopped.

Upstream BDK test helpers' automatic Core/Electrs downloads are disabled for the
offline binary-unit-test build. The checks do not start these helper daemons.
The full rbitcoin and P2Pool upstream test suites are not run by the package build.

## Not established by these checks

- Independent two-builder byte-for-byte agreement.
- Native execution on aarch64-linux.
- A NixOS VM deployment or deployment on a freshly installed remote machine.
- A funded end-to-end wallet payment from this UI.
