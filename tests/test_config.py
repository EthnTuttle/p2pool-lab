from pathlib import Path
import tempfile
import tomllib
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from p2pool_lab.config import Config, load
from p2pool_lab.runtime import initialize, render_nodes


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_paths_are_relative_to_configuration_and_ports_are_configurable(self):
        path = self.root / "config.toml"
        path.write_text(
            '[runtime]\nstate_dir="state"\n[network]\npublic_host="miner-host.local"\ndashboard_port=8099\n[miners.office]\nurl="http://192.0.2.123"\n'
        )
        config = load(path)
        self.assertEqual(config.state_dir, self.root / "state")
        self.assertEqual(config.origin, "http://miner-host.local:8099")
        self.assertEqual(config.miners["office"]["worker"], "office")
        self.assertNotIn("foreign.example:8099", config.allowed_hosts)

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

    def test_defaults_have_no_lan_targets_or_host_specific_paths(self):
        config = Config()
        self.assertEqual(config.bind_host, "127.0.0.1")
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
            second = initialize(config)
        self.assertEqual(first, second)
        self.assertEqual((config.state_dir / "data/rpc.cookie").read_text(), cookie)
        self.assertEqual(
            sum("descriptor" in call.args[0] for call in calls.call_args_list), 2
        )
        self.assertEqual(len(list((config.state_dir / "backups").glob("*.json"))), 2)


if __name__ == "__main__":
    unittest.main()
