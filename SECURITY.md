# Security model

This starter serves a Bitcoin testnet5 wallet and miner controls. Keep its
dashboard restricted to trusted operators. Same-origin/CSRF checks are not a
login system. Defaults bind to loopback; LAN exposure is explicit configuration.

Private material is generated at runtime by BDK, not during a Nix build. Do not
include state directories in Nix source inputs or put secrets in NixOS settings.

Discovery is restricted to a private IPv4 subnet of at most 256 addresses and
read-only requests to `/api/system/info` on port 80. HTTP redirects are rejected.
Adding a device verifies AxeOS metadata before registering its control target.

Report vulnerabilities privately to the maintainer of the repository you are
using. Do not include wallet keys, credentials, or an unredacted runtime state
directory in a public issue. Upstream protocol or consensus issues should also
follow the relevant upstream project's reporting policy.
