# Contributing

Use a topic branch. Keep patches small, and describe the behavior being changed.

## Local checks

```sh
nix develop -c python -m unittest discover -s tests -p 'test_*.py' -v
nix flake check
nix build
```

For dashboard changes, check a wide desktop and a narrow phone viewport. Verify
that stale devices are excluded from fleet totals, changing the selected device
changes action targets, and discovery/addition never changes mining settings.

Payment changes need tests for preview consistency, wrong-chain rejection,
expired previews, concurrent requests, and uncertain-broadcast retries. Use
synthetic inputs or a dedicated test environment, not a funded operator wallet.

## Updating upstream

1. Change a source revision deliberately and update `flake.lock`.
2. Rebase `patches/bdk-testnet5-wallet.patch` against the pinned BDK source.
3. Build each native package and run the relevant tests.
4. Verify chain identity, miner authorization, and a block proposal against a
   dedicated testnet5 lab before deploying.
5. Record what was actually tested in `VALIDATION.md`.

Never put runtime credentials, descriptors, mnemonics, local miner settings, or
databases in a pull request. Default runtime state is outside the checkout.
