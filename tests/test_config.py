from pathlib import Path
import base64
import hashlib
import hmac
import json
import os
import tempfile
import tomllib
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from p2pool_lab.config import POOL_AUTH_FILE, Config, load, pool_auth_token
from p2pool_lab.runtime import (
    ensure_pool_auth,
    ensure_runtime_config,
    initialize,
    render_nodes,
)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_paths_are_relative_to_configuration_and_ports_are_configurable(self):
        path = self.root / "config.toml"
        path.write_text(
            '[runtime]\nstate_dir="state"\n[network]\npublic_host="miner-host.local"\ndashboard_host="192.0.2.9"\ndashboard_port=8099\n[miners.office]\nurl="http://192.0.2.123"\n'
        )
        config = load(path)
        self.assertEqual(config.state_dir, self.root / "state")
        self.assertEqual(config.origin, "http://192.0.2.9:8099")
        self.assertEqual(config.miners["office"]["worker"], "office")
        self.assertNotIn("foreign.example:8099", config.allowed_hosts)
        self.assertIn("miner-host.local:8099", config.allowed_hosts)

    def test_duplicate_ports_fail_before_starting_services(self):
        path = self.root / "config.toml"
        path.write_text("[network]\ndashboard_port=3333\n")
        with self.assertRaisesRegex(ValueError, "distinct"):
            load(path)

    def test_miner_urls_cannot_contain_credentials(self):
        path = self.root / "config.toml"
        path.write_text('[miners.office]\nurl="http://user:password@192.0.2.123"\n')
        with self.assertRaisesRegex(ValueError, "without credentials"):
            load(path)

    def test_miner_urls_must_use_ipv4_literals(self):
        path = self.root / "config.toml"
        path.write_text('[miners.office]\nurl="http://bitaxe.local"\n')
        with self.assertRaisesRegex(ValueError, "IPv4"):
            load(path)
        path.write_text('[miners.office]\nurl="http://localhost"\n')
        with self.assertRaisesRegex(ValueError, "IPv4"):
            load(path)
        for accepted in ("127.0.0.1", "10.0.0.5", "192.0.2.123"):
            path.write_text(f'[miners.office]\nurl="http://{accepted}"\n')
            self.assertEqual(load(path).miners["office"]["url"], f"http://{accepted}")

    def test_defaults_have_no_lan_targets_or_host_specific_paths(self):
        config = Config()
        self.assertEqual(config.bind_host, "127.0.0.1")
        self.assertEqual(config.dashboard_host, "127.0.0.1")
        self.assertEqual(config.miners, {})
        self.assertEqual(config.state_dir, Path.home() / ".local/share/p2pool-lab")

    def test_generated_rpc_secret_is_private_runtime_data(self):
        config = Config(state_dir=self.root)
        render_nodes(
            config,
            {
                "bitcoin_payout_address": "test-address",
                "share_owner_address": "test-share-address",
            },
            "p2pool:test-secret",
        )
        generated = self.root / "config.toml"
        parsed = tomllib.loads(generated.read_text())
        self.assertEqual(parsed["bitcoinrpc"]["password"], "test-secret")
        self.assertEqual(parsed["stratum"]["network"], "testnet5")
        self.assertTrue(parsed["stratum"]["wait_for_chain_sync"])
        self.assertEqual(generated.stat().st_mode & 0o777, 0o600)

    def test_initializer_refuses_unrelated_nonempty_state(self):
        (self.root / "user-work.txt").write_text("keep me")
        with patch("p2pool_lab.runtime.run_bdk") as bdk:
            with self.assertRaisesRegex(RuntimeError, "nonempty"):
                initialize(Config(state_dir=self.root))
        bdk.assert_not_called()

    def test_repeated_initialization_preserves_wallets_and_rpc_credentials(self):
        config = Config(state_dir=self.root / "state")
        payout = "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx"
        owner = "tb1pmfr3p9j00pfxjh0zmgp99y8zftmd3s5pmedqhyptwy6lm87hf5ssk79hv2"

        def bdk(arguments, environment=None):
            if "descriptor" in arguments:
                return {
                    "private_descriptors": {
                        "external": "test-only descriptor",
                        "internal": "test-only change",
                    }
                }
            if "unused_address" in arguments:
                return {
                    "address": owner if "testnet5-share-owner" in arguments else payout,
                    "index": 0,
                }
            return {}

        with (
            patch("p2pool_lab.runtime.run_bdk", side_effect=bdk) as calls,
            patch(
                "p2pool_lab.runtime.subprocess.run",
                return_value=SimpleNamespace(stdout="tp2pool1-test-only"),
            ),
        ):
            first = initialize(config)
            cookie = (config.state_dir / "data/rpc.cookie").read_text()
            pool_auth = (config.state_dir / POOL_AUTH_FILE).read_text()
            second = initialize(config)
        self.assertEqual(first, second)
        self.assertEqual((config.state_dir / "data/rpc.cookie").read_text(), cookie)
        self.assertEqual(
            (config.state_dir / POOL_AUTH_FILE).read_text(),
            pool_auth,
        )
        self.assertEqual(
            sum("descriptor" in call.args[0] for call in calls.call_args_list), 2
        )
        self.assertEqual(len(list((config.state_dir / "backups").glob("*.json"))), 2)

    def write_config(self, network):
        path = self.root / "config.toml"
        path.write_text(f"[network]\n{network}\n")
        return load(path)

    def test_dashboard_host_defaults_to_loopback_independent_of_bind_host(self):
        config = self.write_config('bind_host="192.0.2.10"')
        self.assertEqual(config.dashboard_host, "127.0.0.1")
        self.assertEqual(config.origin, "http://127.0.0.1:8080")
        self.assertNotIn("192.0.2.10:8080", config.allowed_hosts)

    def test_explicit_lan_dashboard_host_controls_listener_and_origin(self):
        config = self.write_config(
            'bind_host="192.0.2.10"\ndashboard_host="192.0.2.20"\npublic_host="miner-host.local"'
        )
        self.assertEqual(config.origin, "http://192.0.2.20:8080")
        self.assertEqual(
            config.allowed_hosts,
            {
                "192.0.2.20:8080",
                "miner-host.local:8080",
                "localhost:8080",
                "127.0.0.1:8080",
            },
        )

    def test_wildcard_dashboard_host_derives_origin_from_public_host(self):
        config = self.write_config(
            'bind_host="0.0.0.0"\ndashboard_host="0.0.0.0"\npublic_host="miner-host.local"'
        )
        self.assertEqual(config.origin, "http://miner-host.local:8080")
        self.assertNotIn("0.0.0.0:8080", config.allowed_hosts)
        self.assertIn("miner-host.local:8080", config.allowed_hosts)

    def test_dashboard_host_must_be_an_ipv4_address(self):
        with self.assertRaisesRegex(ValueError, "dashboard_host must be an IPv4"):
            self.write_config('dashboard_host="miner-host.local"')

    def test_pool_api_credentials_are_generated_once_and_kept_private(self):
        config = Config(state_dir=self.root)
        first = ensure_pool_auth(config)
        path = self.root / POOL_AUTH_FILE
        self.assertEqual(first["username"], "p2pool-lab")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / "data").stat().st_mode & 0o777, 0o700)
        second = ensure_pool_auth(config)
        self.assertEqual(first, second)
        self.assertEqual(json.loads(path.read_text()), first)
        self.assertEqual(first["salt"], first["token"].split("$", 1)[0])
        # Upstream computes the HMAC over the salt string's ASCII bytes.
        expected = hmac.new(
            first["salt"].encode(),
            first["password"].encode(),
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(first["token"], f"{first['salt']}${expected}")

    def test_pool_auth_token_matches_upstream_computation(self):
        salt = "0123456789abcdef0123456789abcdef"
        self.assertEqual(
            pool_auth_token(salt, "testpassword123"),
            salt + "$e6eb13e97ec482011d014112984f4b4e2ff82c25117f07bf4b616257df1923b7",
        )

    def test_render_nodes_writes_pool_api_auth_without_password(self):
        config = Config(state_dir=self.root)
        render_nodes(
            config,
            {
                "bitcoin_payout_address": "test-address",
                "share_owner_address": "test-share-address",
            },
            "p2pool:test-secret",
        )
        parsed = tomllib.loads((self.root / "config.toml").read_text())
        credentials = json.loads((self.root / POOL_AUTH_FILE).read_text())
        self.assertEqual(parsed["api"]["auth_user"], "p2pool-lab")
        self.assertEqual(parsed["api"]["auth_token"], credentials["token"])
        self.assertNotIn("auth_password", parsed["api"])

    def test_pool_auth_headers_carry_basic_credentials(self):
        config = Config(state_dir=self.root)
        credentials = ensure_pool_auth(config)
        expected = base64.b64encode(
            f"{credentials['username']}:{credentials['password']}".encode()
        ).decode("ascii")
        self.assertEqual(
            config.pool_auth_headers, {"Authorization": f"Basic {expected}"}
        )

    def test_pool_auth_headers_are_empty_when_credentials_are_unusable(self):
        config = Config(state_dir=self.root)
        self.assertEqual(config.pool_auth_headers, {})
        path = self.root / POOL_AUTH_FILE
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text("{not json")
        self.assertEqual(config.pool_auth_headers, {})
        path.write_text('{"username": "p2pool-lab"}')
        self.assertEqual(config.pool_auth_headers, {})
        path.write_text('{"username": "p2pool-lab", "password": 5}')
        self.assertEqual(config.pool_auth_headers, {})

    def test_corrupt_pool_api_credentials_are_regenerated(self):
        config = Config(state_dir=self.root)
        path = self.root / POOL_AUTH_FILE
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(
            '{"username": "x", "password": "y", "salt": "00", "token": "wrong"}'
        )
        regenerated = ensure_pool_auth(config)
        self.assertEqual(json.loads(path.read_text()), regenerated)
        self.assertEqual(regenerated["username"], "p2pool-lab")
        self.assertNotEqual(regenerated["token"], "wrong")

    def test_pool_api_credentials_permissions_are_restored(self):
        config = Config(state_dir=self.root)
        first = ensure_pool_auth(config)
        path = self.root / POOL_AUTH_FILE
        original = path.read_text()
        os.chmod(path, 0o644)
        os.chmod(path.parent, 0o755)
        again = ensure_pool_auth(config)
        self.assertEqual(again, first)
        self.assertEqual(path.read_text(), original)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def prepare_initialized_state(self):
        (self.root / "data").mkdir()
        (self.root / ".p2pool-lab").write_text("")
        (self.root / "miner-settings.json").write_text(
            json.dumps(
                {
                    "bitcoin_payout_address": "tb1q-test-payout",
                    "share_owner_address": "tp2pool1-test",
                }
            )
        )
        (self.root / "data/rpc.cookie").write_text("p2pool:test-secret")

    def test_upgrade_adds_pool_api_auth_to_existing_state(self):
        self.prepare_initialized_state()
        # A pre-upgrade rendered pool config with no authentication.
        (self.root / "config.toml").write_text(
            '[api]\nhostname="127.0.0.1"\nport=46884\n'
        )
        config = Config(state_dir=self.root)
        ensure_runtime_config(config)
        parsed = tomllib.loads((self.root / "config.toml").read_text())
        credentials = json.loads((self.root / POOL_AUTH_FILE).read_text())
        self.assertEqual(parsed["api"]["auth_user"], "p2pool-lab")
        self.assertEqual(parsed["api"]["auth_token"], credentials["token"])
        self.assertEqual(parsed["bitcoinrpc"]["password"], "test-secret")
        path = self.root / POOL_AUTH_FILE
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / "data").stat().st_mode & 0o777, 0o700)
        rendered = (self.root / "config.toml").read_text()
        stored = path.read_text()
        ensure_runtime_config(config)
        self.assertEqual((self.root / "config.toml").read_text(), rendered)
        self.assertEqual(path.read_text(), stored)

    def test_runtime_config_requires_fully_initialized_state(self):
        config = Config(state_dir=self.root / "state")
        with self.assertRaisesRegex(RuntimeError, "init"):
            ensure_runtime_config(config)
        self.prepare_initialized_state()
        (self.root / "data/rpc.cookie").unlink()
        with self.assertRaisesRegex(RuntimeError, "initialized"):
            ensure_runtime_config(Config(state_dir=self.root))


if __name__ == "__main__":
    unittest.main()
