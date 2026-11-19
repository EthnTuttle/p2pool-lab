# Security model

This starter serves a Bitcoin testnet5 wallet and miner controls. Keep its
dashboard restricted to trusted operators. Same-origin/CSRF checks are not a
login system. The dashboard binds to `network.dashboard_host` (loopback by
default), independently of `network.bind_host` (Stratum, share-peer, and
authenticated API/explorer listeners), so enabling LAN mining access does not
expose the dashboard; LAN dashboard exposure is explicit configuration.

The P2Pool REST API (including `/blocked_ips`) requires HTTP Basic
authentication. Credentials are generated once at initialization and stored in
`data/p2pool-api-auth.json` under the state directory with owner-only (0600)
permissions: a username, a random password, a salt, and the HMAC-SHA256 token
written to the pool node's private runtime config. They are never logged or
placed in public configuration, and the upstream explorer login accepts the
same username and password. State directories created before these credentials
existed are migrated automatically: the node configuration is re-rendered under
the state-directory lock before every pool launch, and startup fails rather
than running the pool with an unauthenticated API.

Private material is generated at runtime by BDK or the initializer, not
during a Nix build. Do not include state directories in Nix source inputs or
put secrets in NixOS settings.

Discovery is restricted to a private IPv4 subnet of at most 256 addresses and
read-only requests to `/api/system/info` on port 80. HTTP redirects are rejected.
Adding a device verifies AxeOS metadata before registering its control target.
All outbound miner requests use IPv4 literals — DNS names are rejected in
configuration so a stalled resolver cannot outlive the request deadline.

Report vulnerabilities privately to the maintainer of the repository you are
using. Do not include wallet keys, credentials, or an unredacted runtime state
directory in a public issue. Upstream protocol or consensus issues should also
follow the relevant upstream project's reporting policy.
