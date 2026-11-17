"""Wallet payment tests use synthetic UTXOs and never broadcast to a network."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from contextlib import contextmanager
from unittest.mock import Mock, patch

from embit import script
from embit.psbt import PSBT
from embit.transaction import Transaction, TransactionInput, TransactionOutput

from p2pool_lab.wallet_api import WalletError, WalletService

ADDRESS = "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx"
CHANGE = "tb1pmfr3p9j00pfxjh0zmgp99y8zftmd3s5pmedqhyptwy6lm87hf5ssk79hv2"
GENESIS = "00000000000008c1637e7d3252dc1c333d4546fd6fff6a474b200d674a91d497"


class WalletTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        config = self.root / "data/bdk-testnet5/config.toml"
        config.parent.mkdir(parents=True)
        config.write_text(
            f'[wallets.testnet5-payout]\nnetwork="testnet4"\ngenesis_hash="{GENESIS}"\n'
        )
        self.database = self.root / "db.sqlite"
        self.rpc = Mock(return_value=GENESIS)
        self.event = Mock()
        self.wallet = WalletService(
            self.root, self.connect, self.rpc, GENESIS, self.event
        )
        transaction = Transaction(
            vin=[TransactionInput(bytes.fromhex("11" * 32), 0)],
            vout=[
                TransactionOutput(1000, script.address_to_scriptpubkey(ADDRESS)),
                TransactionOutput(8800, script.address_to_scriptpubkey(CHANGE)),
            ],
        )
        self.psbt = PSBT(transaction)
        self.psbt.inputs[0].witness_utxo = TransactionOutput(
            10000, script.address_to_scriptpubkey(ADDRESS)
        )
        self.encoded = self.psbt.to_base64()
        self.raw = transaction.serialize().hex()
        self.txid = transaction.txid().hex()

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def cli(self, command, *args):
        responses = {
            "sync": {},
            "balance": {"confirmed": 10000, "trusted_pending": 0},
            "create_tx": {"psbt": self.encoded},
            "sign": {"psbt": self.encoded, "is_finalized": True},
            "extract_psbt": {"raw_tx": self.raw},
        }
        return responses[command]

    def make_preview(self):
        return self.wallet.preview(
            {"address": ADDRESS, "amount_sat": 1000, "fee_rate": "1"}
        )

    def test_preview_reports_exact_fee_without_signing_or_broadcast(self):
        with patch.object(self.wallet, "_cli", side_effect=self.cli) as cli:
            preview = self.make_preview()
        self.assertEqual(preview["fee_sat"], 200)
        self.assertEqual(preview["total_sat"], 1200)
        self.assertEqual(preview["change_sat"], 8800)
        self.assertNotIn("psbt", preview)
        self.assertEqual(
            [call.args[0] for call in cli.call_args_list],
            ["sync", "balance", "create_tx"],
        )
        self.rpc.assert_called_once_with("getblockhash", [0])

    def test_rejects_mainnet_address(self):
        mainnet = script.address_to_scriptpubkey(ADDRESS).address()
        with self.assertRaisesRegex(WalletError, "testnet address"):
            self.wallet.preview(
                {"address": mainnet, "amount_sat": 1000, "fee_rate": "1"}
            )
        self.rpc.assert_not_called()

    def test_rejects_fractional_satoshis(self):
        with self.assertRaisesRegex(WalletError, "whole number"):
            self.wallet.preview(
                {"address": ADDRESS, "amount_sat": 1.5, "fee_rate": "1"}
            )

    def test_rejects_non_finite_fee(self):
        with self.assertRaisesRegex(WalletError, "Fee rate"):
            self.wallet.preview(
                {"address": ADDRESS, "amount_sat": 1000, "fee_rate": "NaN"}
            )

    def test_wrong_genesis_prevents_wallet_commands(self):
        self.rpc.return_value = "00" * 32
        with patch.object(self.wallet, "_cli") as cli:
            with self.assertRaisesRegex(WalletError, "testnet5 chain"):
                self.make_preview()
        cli.assert_not_called()

    def test_immature_funds_cannot_fund_payment(self):
        with patch.object(
            self.wallet,
            "_cli",
            side_effect=[
                {},
                {"confirmed": 0, "trusted_pending": 0, "immature": 5000000000},
            ],
        ) as cli:
            with self.assertRaisesRegex(WalletError, "Insufficient"):
                self.make_preview()
        self.assertEqual(cli.call_count, 2)

    def test_expired_preview_does_not_sign(self):
        with patch.object(self.wallet, "_cli", side_effect=self.cli):
            preview = self.make_preview()
        with self.connect() as connection:
            connection.execute(
                "UPDATE wallet_transfers SET expires_at=?", (int(time.time()) - 1,)
            )
        with patch.object(self.wallet, "_cli") as cli:
            with self.assertRaisesRegex(WalletError, "expired"):
                self.wallet.send(preview)
        cli.assert_not_called()

    def test_changed_signed_transaction_never_broadcasts(self):
        with patch.object(self.wallet, "_cli", side_effect=self.cli):
            preview = self.make_preview()
        self.psbt.tx.vout[0].value = 2000
        # PSBT reconstructs tx from its output scopes, so alter that stored scope too.
        self.psbt.outputs[0].value = 2000
        with patch.object(
            self.wallet,
            "_cli",
            return_value={"psbt": self.psbt.to_base64(), "is_finalized": True},
        ):
            with self.assertRaises(WalletError):
                self.wallet.send(preview)
        self.assertTrue(
            all(
                call.args[0] != "sendrawtransaction" for call in self.rpc.call_args_list
            )
        )

    def test_double_click_and_service_restart_do_not_create_second_payment(self):
        self.rpc.side_effect = lambda method, params: (
            GENESIS if method == "getblockhash" else self.txid
        )
        with patch.object(self.wallet, "_cli", side_effect=self.cli):
            preview = self.make_preview()
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(self.wallet.send, [preview, preview]))
        self.assertEqual(results[0]["txid"], self.txid)
        self.assertEqual(results[1]["txid"], self.txid)
        self.assertEqual(
            sum(
                call.args[0] == "sendrawtransaction" for call in self.rpc.call_args_list
            ),
            1,
        )
        restarted = WalletService(
            self.root, self.connect, self.rpc, GENESIS, self.event
        )
        self.assertEqual(restarted.send(preview)["txid"], self.txid)
        self.assertEqual(
            sum(
                call.args[0] == "sendrawtransaction" for call in self.rpc.call_args_list
            ),
            1,
        )

    def test_uncertain_broadcast_retries_exact_bytes_without_signing_again(self):
        with patch.object(self.wallet, "_cli", side_effect=self.cli):
            preview = self.make_preview()

        def unavailable(method, params):
            if method == "getblockhash":
                return GENESIS
            raise TimeoutError("Network unavailable")

        self.rpc.side_effect = unavailable
        with patch.object(self.wallet, "_cli", side_effect=self.cli):
            with self.assertRaisesRegex(WalletError, "transfer is saved"):
                self.wallet.send(preview)
        self.assertEqual(self.wallet.snapshot()["transfers"][0]["status"], "signed")
        restarted = WalletService(
            self.root, self.connect, self.rpc, GENESIS, self.event
        )
        self.rpc.side_effect = lambda method, params: (
            GENESIS if method == "getblockhash" else self.txid
        )
        with patch.object(restarted, "_cli") as cli:
            self.assertEqual(restarted.send(preview)["txid"], self.txid)
        cli.assert_not_called()
        broadcasts = [
            call.args[1][0]
            for call in self.rpc.call_args_list
            if call.args[0] == "sendrawtransaction"
        ]
        self.assertEqual(broadcasts, [self.raw, self.raw])

    def test_receive_address_persists_across_service_restart(self):
        with patch.object(
            self.wallet, "_cli", return_value={"address": ADDRESS, "index": 5}
        ):
            address = self.wallet.new_address()
        restarted = WalletService(
            self.root, self.connect, self.rpc, GENESIS, self.event
        )
        with patch.object(restarted, "_cli") as cli:
            self.assertEqual(restarted._receive(), address)
        cli.assert_not_called()

    def test_sync_accepts_bdk_event_output_before_json(self):
        result = Mock(
            returncode=0,
            stdout=b'Chain tip advanced\n{"message":"Sync completed"}',
            stderr=b"",
        )
        with patch("p2pool_lab.wallet_api.subprocess.run", return_value=result):
            self.assertEqual(self.wallet._cli("sync"), {})


if __name__ == "__main__":
    unittest.main()
