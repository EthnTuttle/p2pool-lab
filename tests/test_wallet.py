"""Wallet payment tests use synthetic UTXOs and never broadcast to a network."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import threading
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
        with self.connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS events ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "timestamp INTEGER, level TEXT, message TEXT)"
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
            "transactions": {"items": []},
        }
        return responses[command]

    def real_event(self, level, message):
        """Production-like callback: the monitor writes events through a
        second connection to the same database, so any state change must be
        committed before this runs."""
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO events(timestamp,level,message) VALUES (?,?,?)",
                (int(time.time()), level, message),
            )

    def node_accepts(self, method, params):
        """An honest node: correct chain, accepts the tx, broadcasts it."""
        if method == "getblockhash":
            return GENESIS
        if method == "testmempoolaccept":
            return [{"allowed": True}]
        return self.txid

    def racing_node(self, barrier, broadcasts):
        """Both services must finish signing and pre-checking (the barrier)
        before either may race for the ready->signed transition."""

        def race(method, params):
            if method == "getblockhash":
                return GENESIS
            if method == "testmempoolaccept":
                barrier.wait(timeout=10)
                return [{"allowed": True}]
            if method == "sendrawtransaction":
                broadcasts.append(params[0])
                return self.txid
            raise AssertionError(method)

        return race

    def attempt(self, service, draft):
        try:
            return ("ok", service.send(draft)["txid"])
        except WalletError as error:
            return ("error", error.status)

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
        self.rpc.side_effect = self.node_accepts
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
            if method == "testmempoolaccept":
                return [{"allowed": True}]
            raise TimeoutError("Network unavailable")

        self.rpc.side_effect = unavailable
        with patch.object(self.wallet, "_cli", side_effect=self.cli):
            with self.assertRaisesRegex(WalletError, "transfer is saved"):
                self.wallet.send(preview)
        self.assertEqual(self.wallet.snapshot()["transfers"][0]["status"], "signed")
        # While the saved transfer is unresolved, no new preview may be built.
        with patch.object(self.wallet, "_cli", side_effect=self.cli) as cli:
            with self.assertRaisesRegex(WalletError, "pending transfer"):
                self.make_preview()
        self.assertEqual([call.args[0] for call in cli.call_args_list], ["sync"])
        restarted = WalletService(
            self.root, self.connect, self.rpc, GENESIS, self.event
        )
        self.rpc.side_effect = self.node_accepts
        with patch.object(restarted, "_cli") as cli:
            self.assertEqual(restarted.send(preview)["txid"], self.txid)
        cli.assert_not_called()
        broadcasts = [
            call.args[1][0]
            for call in self.rpc.call_args_list
            if call.args[0] == "sendrawtransaction"
        ]
        self.assertEqual(broadcasts, [self.raw, self.raw])

    def test_sending_one_draft_supersedes_competing_ready_draft(self):
        self.rpc.side_effect = self.node_accepts
        with patch.object(self.wallet, "_cli", side_effect=self.cli):
            first = self.make_preview()
            second = self.make_preview()
            self.assertEqual(self.wallet.send(first)["txid"], self.txid)
        with self.connect() as connection:
            statuses = {
                row["id"]: row["status"]
                for row in connection.execute("SELECT id,status FROM wallet_transfers")
            }
        self.assertEqual(statuses[first["draft_id"]], "broadcast")
        self.assertEqual(statuses[second["draft_id"]], "superseded")
        superseded = next(
            transfer
            for transfer in self.wallet.snapshot()["transfers"]
            if transfer["draft_id"] == second["draft_id"]
        )
        self.assertEqual(superseded["status"], "superseded")
        self.assertIn("reason", superseded)
        # The stale draft can never be signed or broadcast later.
        with patch.object(self.wallet, "_cli") as cli:
            with self.assertRaisesRegex(WalletError, "no longer usable"):
                self.wallet.send(second)
        cli.assert_not_called()
        self.assertEqual(
            sum(
                call.args[0] == "sendrawtransaction" for call in self.rpc.call_args_list
            ),
            1,
        )

    def test_mempool_rejection_fails_draft_without_wedging_preview(self):
        with patch.object(self.wallet, "_cli", side_effect=self.cli):
            preview = self.make_preview()

        def rejects(method, params):
            if method == "getblockhash":
                return GENESIS
            if method == "testmempoolaccept":
                return [{"allowed": False, "reject-reason": "mempool min fee not met"}]
            raise AssertionError("a rejected transaction must not be broadcast")

        self.rpc.side_effect = rejects
        with patch.object(self.wallet, "_cli", side_effect=self.cli):
            with self.assertRaisesRegex(WalletError, "mempool min fee not met"):
                self.wallet.send(preview)
        transfer = self.wallet.snapshot()["transfers"][0]
        self.assertEqual(transfer["status"], "failed")
        self.assertIn("mempool min fee not met", transfer["reason"])
        # The failed draft neither blocks new previews nor can be resent.
        with patch.object(self.wallet, "_cli", side_effect=self.cli):
            fresh = self.make_preview()
        self.assertNotEqual(fresh["draft_id"], preview["draft_id"])
        with patch.object(self.wallet, "_cli") as cli:
            with self.assertRaisesRegex(WalletError, "no longer usable"):
                self.wallet.send(preview)
        cli.assert_not_called()

    def test_broadcast_missing_inputs_marks_draft_terminal(self):
        with patch.object(self.wallet, "_cli", side_effect=self.cli):
            preview = self.make_preview()

        def spends_gone(method, params):
            if method == "getblockhash":
                return GENESIS
            if method == "testmempoolaccept":
                return [{"allowed": True}]
            if method == "sendrawtransaction":
                raise RuntimeError({"code": -26, "message": "missing-inputs"})
            raise TimeoutError("Transaction not found")

        self.rpc.side_effect = spends_gone
        with patch.object(self.wallet, "_cli", side_effect=self.cli):
            with self.assertRaisesRegex(WalletError, "missing or already spent"):
                self.wallet.send(preview)
        self.assertEqual(self.wallet.snapshot()["transfers"][0]["status"], "failed")
        # Terminal: retrying can neither sign nor broadcast a second payment.
        with patch.object(self.wallet, "_cli") as cli:
            with self.assertRaisesRegex(WalletError, "no longer usable"):
                self.wallet.send(preview)
        cli.assert_not_called()
        self.assertEqual(
            sum(
                call.args[0] == "sendrawtransaction" for call in self.rpc.call_args_list
            ),
            1,
        )

    def test_mempool_precheck_failure_keeps_draft_retryable(self):
        with patch.object(self.wallet, "_cli", side_effect=self.cli):
            preview = self.make_preview()

        def unavailable(method, params):
            if method == "getblockhash":
                return GENESIS
            raise TimeoutError("Network unavailable")

        self.rpc.side_effect = unavailable
        with patch.object(self.wallet, "_cli", side_effect=self.cli):
            with self.assertRaisesRegex(WalletError, "pre-check"):
                self.wallet.send(preview)
        with self.connect() as connection:
            status = connection.execute(
                "SELECT status FROM wallet_transfers WHERE id=?",
                (preview["draft_id"],),
            ).fetchone()["status"]
        self.assertEqual(status, "ready")
        # Nothing was broadcast; a later attempt with a reachable node succeeds.
        self.rpc.side_effect = self.node_accepts
        with patch.object(self.wallet, "_cli", side_effect=self.cli):
            self.assertEqual(self.wallet.send(preview)["txid"], self.txid)
        self.assertEqual(
            sum(
                call.args[0] == "sendrawtransaction" for call in self.rpc.call_args_list
            ),
            1,
        )

    def test_node_already_knowing_transaction_is_not_marked_failed(self):
        with patch.object(self.wallet, "_cli", side_effect=self.cli):
            preview = self.make_preview()

        def knows(method, params):
            if method == "getblockhash":
                return GENESIS
            if method == "testmempoolaccept":
                return [{"allowed": False, "reject-reason": "txn-already-in-mempool"}]
            return self.txid

        self.rpc.side_effect = knows
        with patch.object(self.wallet, "_cli", side_effect=self.cli):
            self.assertEqual(self.wallet.send(preview)["txid"], self.txid)
        self.assertEqual(self.wallet.snapshot()["transfers"][0]["status"], "broadcast")

    def test_snapshot_exposes_no_utxos_or_transaction_bytes(self):
        self.rpc.side_effect = self.node_accepts
        with patch.object(self.wallet, "_cli", side_effect=self.cli):
            preview = self.make_preview()
            self.wallet.send(preview)
        snapshot = self.wallet.snapshot()
        self.assertNotIn("utxos", snapshot)
        for transfer in snapshot["transfers"]:
            self.assertNotIn("psbt", transfer)
            self.assertNotIn("raw_tx", transfer)

    def test_two_services_racing_one_ready_draft_broadcast_once(self):
        barrier = threading.Barrier(2)
        broadcasts = []
        self.rpc.side_effect = self.racing_node(barrier, broadcasts)
        first = WalletService(self.root, self.connect, self.rpc, GENESIS, self.event)
        second = WalletService(self.root, self.connect, self.rpc, GENESIS, self.event)
        with patch.object(self.wallet, "_cli", side_effect=self.cli):
            preview = self.make_preview()
        with (
            patch.object(first, "_cli", side_effect=self.cli),
            patch.object(second, "_cli", side_effect=self.cli),
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = list(
                    executor.map(self.attempt, [first, second], [preview, preview])
                )
        # Both read the ready draft and passed the pre-check, yet exactly one
        # process won the compare-and-swap and broadcast; the loser never
        # reached sendrawtransaction.
        self.assertEqual(sorted(outcome[0] for outcome in outcomes), ["error", "ok"])
        self.assertEqual(
            [outcome for outcome in outcomes if outcome[0] == "error"], [("error", 409)]
        )
        self.assertEqual(broadcasts, [self.raw])
        with self.connect() as connection:
            row = connection.execute(
                "SELECT status,txid FROM wallet_transfers WHERE id=?",
                (preview["draft_id"],),
            ).fetchone()
        self.assertEqual(row["status"], "broadcast")
        self.assertEqual(row["txid"], self.txid)

    def test_two_services_racing_competing_drafts_broadcast_once(self):
        barrier = threading.Barrier(2)
        broadcasts = []
        self.rpc.side_effect = self.racing_node(barrier, broadcasts)
        first = WalletService(self.root, self.connect, self.rpc, GENESIS, self.event)
        second = WalletService(self.root, self.connect, self.rpc, GENESIS, self.event)
        with patch.object(self.wallet, "_cli", side_effect=self.cli):
            # Two tabs, two drafts, same spendable inputs.
            one = self.make_preview()
            two = self.make_preview()
        with (
            patch.object(first, "_cli", side_effect=self.cli),
            patch.object(second, "_cli", side_effect=self.cli),
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = list(executor.map(self.attempt, [first, second], [one, two]))
        self.assertEqual(sorted(outcome[0] for outcome in outcomes), ["error", "ok"])
        self.assertEqual(broadcasts, [self.raw])
        with self.connect() as connection:
            statuses = sorted(
                row["status"]
                for row in connection.execute(
                    "SELECT status FROM wallet_transfers"
                ).fetchall()
            )
        # The loser's draft was superseded in the winner's transaction, so it
        # can never become a second spend of the same inputs.
        self.assertEqual(statuses, ["broadcast", "superseded"])

    def test_broadcast_result_never_overwrites_terminal_state(self):
        with patch.object(self.wallet, "_cli", side_effect=self.cli):
            preview = self.make_preview()

        def conflicting(method, params):
            if method == "getblockhash":
                return GENESIS
            if method == "testmempoolaccept":
                return [{"allowed": True}]
            if method == "sendrawtransaction":
                # Simulate another process resolving the transfer while this
                # broadcast is in flight.
                with self.connect() as connection:
                    connection.execute(
                        "UPDATE wallet_transfers SET status='failed' WHERE id=?",
                        (preview["draft_id"],),
                    )
                return self.txid
            raise AssertionError(method)

        self.rpc.side_effect = conflicting
        with patch.object(self.wallet, "_cli", side_effect=self.cli):
            with self.assertRaisesRegex(WalletError, "already resolved"):
                self.wallet.send(preview)
        with self.connect() as connection:
            status = connection.execute(
                "SELECT status FROM wallet_transfers WHERE id=?",
                (preview["draft_id"],),
            ).fetchone()["status"]
        self.assertEqual(status, "failed")

    def test_abandon_absent_signed_transfer_unblocks_preview(self):
        with patch.object(self.wallet, "_cli", side_effect=self.cli):
            preview = self.make_preview()

        def unavailable(method, params):
            if method == "getblockhash":
                return GENESIS
            if method == "testmempoolaccept":
                return [{"allowed": True}]
            raise TimeoutError("Network unavailable")

        self.rpc.side_effect = unavailable
        with patch.object(self.wallet, "_cli", side_effect=self.cli):
            with self.assertRaisesRegex(WalletError, "transfer is saved"):
                self.wallet.send(preview)

        def absent(method, params):
            if method == "getblockhash":
                return GENESIS
            raise RuntimeError(
                {"code": -5, "message": "No such mempool or blockchain transaction"}
            )

        self.rpc.side_effect = absent
        with patch.object(self.wallet, "_cli", side_effect=self.cli):
            abandoned = self.wallet.abandon(
                {"draft_id": preview["draft_id"], "confirmed": True}
            )
        self.assertEqual(abandoned["status"], "abandoned")
        transfer = self.wallet.snapshot()["transfers"][0]
        self.assertEqual(transfer["status"], "abandoned")
        self.assertIn("reason", transfer)
        # Abandoned is terminal: no retry, and previews are unblocked.
        with patch.object(self.wallet, "_cli") as cli:
            with self.assertRaisesRegex(WalletError, "no longer usable"):
                self.wallet.send(preview)
        cli.assert_not_called()
        with patch.object(self.wallet, "_cli", side_effect=self.cli):
            self.make_preview()

    def test_abandon_transaction_present_reconciles_to_broadcast(self):
        with patch.object(self.wallet, "_cli", side_effect=self.cli):
            preview = self.make_preview()

        def unavailable(method, params):
            if method == "getblockhash":
                return GENESIS
            if method == "testmempoolaccept":
                return [{"allowed": True}]
            raise TimeoutError("Network unavailable")

        self.rpc.side_effect = unavailable
        with patch.object(self.wallet, "_cli", side_effect=self.cli):
            with self.assertRaisesRegex(WalletError, "transfer is saved"):
                self.wallet.send(preview)

        def present(method, params):
            if method == "getblockhash":
                return GENESIS
            if method == "getrawtransaction":
                return self.raw
            raise AssertionError(method)

        self.rpc.side_effect = present
        with self.assertRaisesRegex(WalletError, "already has this transaction"):
            self.wallet.abandon({"draft_id": preview["draft_id"], "confirmed": True})
        self.assertEqual(self.wallet.snapshot()["transfers"][0]["status"], "broadcast")
        # Reconciled transfers stay safely retryable.
        self.assertEqual(self.wallet.send(preview)["txid"], self.txid)

    def test_only_signed_transfers_can_be_abandoned(self):
        with patch.object(self.wallet, "_cli", side_effect=self.cli):
            ready = self.make_preview()
        with self.assertRaisesRegex(WalletError, "irreversible"):
            self.wallet.abandon({"draft_id": ready["draft_id"]})
        with self.assertRaisesRegex(WalletError, "irreversible"):
            self.wallet.abandon({"draft_id": ready["draft_id"], "confirmed": False})
        with patch.object(self.wallet, "_cli", side_effect=self.cli):
            with self.assertRaisesRegex(WalletError, "Only a saved transfer"):
                self.wallet.abandon({"draft_id": ready["draft_id"], "confirmed": True})

        def rejects(method, params):
            if method == "getblockhash":
                return GENESIS
            if method == "testmempoolaccept":
                return [{"allowed": False, "reject-reason": "mempool min fee not met"}]
            raise AssertionError(method)

        self.rpc.side_effect = rejects
        with patch.object(self.wallet, "_cli", side_effect=self.cli):
            failed = self.make_preview()
            with self.assertRaisesRegex(WalletError, "mempool min fee not met"):
                self.wallet.send(failed)
        self.rpc.side_effect = self.node_accepts
        with patch.object(self.wallet, "_cli", side_effect=self.cli):
            kept = self.make_preview()
            superseded = self.make_preview()
            self.wallet.send(kept)
        for draft in (failed, superseded):
            with self.assertRaisesRegex(WalletError, "Only a saved transfer"):
                self.wallet.abandon({"draft_id": draft["draft_id"], "confirmed": True})
        with self.assertRaisesRegex(WalletError, "not found"):
            self.wallet.abandon({"draft_id": "x" * 32, "confirmed": True})

    def test_abandon_requires_definitive_absence(self):
        with patch.object(self.wallet, "_cli", side_effect=self.cli):
            preview = self.make_preview()

        def unavailable(method, params):
            if method == "getblockhash":
                return GENESIS
            if method == "testmempoolaccept":
                return [{"allowed": True}]
            raise TimeoutError("Network unavailable")

        self.rpc.side_effect = unavailable
        with patch.object(self.wallet, "_cli", side_effect=self.cli):
            with self.assertRaisesRegex(WalletError, "transfer is saved"):
                self.wallet.send(preview)
        request = {"draft_id": preview["draft_id"], "confirmed": True}
        # Timeouts, unstructured backend errors, null and malformed responses
        # are not proof of absence: the transfer must stay signed.
        scenarios = [
            TimeoutError("Network unavailable"),
            RuntimeError("authentication failed"),
            None,
            "not-hex",
        ]
        for scenario in scenarios:

            def node(method, params, scenario=scenario):
                if method == "getblockhash":
                    return GENESIS
                if method == "getrawtransaction":
                    if isinstance(scenario, Exception):
                        raise scenario
                    return scenario
                raise AssertionError(method)

            self.rpc.side_effect = node
            with patch.object(self.wallet, "_cli", side_effect=self.cli):
                with self.assertRaisesRegex(
                    WalletError, "could not confirm|unexpected response"
                ):
                    self.wallet.abandon(request)
            self.assertEqual(self.wallet.snapshot()["transfers"][0]["status"], "signed")

    def test_abandon_reconcile_race_does_not_emit_false_success(self):
        with patch.object(self.wallet, "_cli", side_effect=self.cli):
            preview = self.make_preview()

        def unavailable(method, params):
            if method == "getblockhash":
                return GENESIS
            if method == "testmempoolaccept":
                return [{"allowed": True}]
            raise TimeoutError("Network unavailable")

        self.rpc.side_effect = unavailable
        with patch.object(self.wallet, "_cli", side_effect=self.cli):
            with self.assertRaisesRegex(WalletError, "transfer is saved"):
                self.wallet.send(preview)

        def present_but_resolved(method, params):
            if method == "getblockhash":
                return GENESIS
            if method == "getrawtransaction":
                # Simulate another process resolving the transfer while the
                # node lookup is in flight.
                with self.connect() as connection:
                    connection.execute(
                        "UPDATE wallet_transfers SET status='failed' WHERE id=?",
                        (preview["draft_id"],),
                    )
                return self.raw
            raise AssertionError(method)

        self.rpc.side_effect = present_but_resolved
        with self.assertRaisesRegex(WalletError, "already resolved otherwise"):
            self.wallet.abandon({"draft_id": preview["draft_id"], "confirmed": True})
        with self.connect() as connection:
            status = connection.execute(
                "SELECT status FROM wallet_transfers WHERE id=?",
                (preview["draft_id"],),
            ).fetchone()["status"]
        self.assertEqual(status, "failed")
        self.event.assert_not_called()

    def test_preview_cannot_insert_draft_after_signing_commits(self):
        with patch.object(self.wallet, "_cli", side_effect=self.cli):
            draft = self.make_preview()
        service = WalletService(self.root, self.connect, self.rpc, GENESIS, self.event)
        entered, release = threading.Event(), threading.Event()

        def paused_cli(command, *args):
            if command == "create_tx":
                # The preview has passed the signed-transfer check and is
                # paused mid-build, exactly where a concurrent signer commits.
                entered.set()
                self.assertTrue(release.wait(timeout=10))
            return self.cli(command, *args)

        def uncertain(method, params):
            if method == "getblockhash":
                return GENESIS
            if method == "testmempoolaccept":
                return [{"allowed": True}]
            raise TimeoutError("Network unavailable")

        outcome = {}

        def build_preview():
            try:
                service.preview(
                    {"address": ADDRESS, "amount_sat": 1000, "fee_rate": "1"}
                )
            except WalletError as error:
                outcome["status"] = error.status

        self.rpc.side_effect = uncertain
        with patch.object(service, "_cli", side_effect=paused_cli):
            thread = threading.Thread(target=build_preview)
            thread.start()
            self.assertTrue(entered.wait(timeout=10))
            # While the preview is paused, another service signs the draft and
            # keeps it in the saved (signed) state.
            with patch.object(self.wallet, "_cli", side_effect=self.cli):
                with self.assertRaisesRegex(WalletError, "transfer is saved"):
                    self.wallet.send(draft)
            release.set()
            thread.join(timeout=10)
        # The resumed preview must not insert a competing ready draft.
        self.assertEqual(outcome.get("status"), 409)
        with self.connect() as connection:
            rows = connection.execute("SELECT status FROM wallet_transfers").fetchall()
        self.assertEqual([row["status"] for row in rows], ["signed"])
        # Resolving the saved transfer broadcasts the shared outpoint exactly
        # once; the rejected preview never existed.
        broadcasts = []

        def finishing(method, params):
            if method == "getblockhash":
                return GENESIS
            if method == "sendrawtransaction":
                broadcasts.append(params[0])
                return self.txid
            raise AssertionError(method)

        self.rpc.side_effect = finishing
        with patch.object(self.wallet, "_cli", side_effect=self.cli):
            self.assertEqual(self.wallet.send(draft)["txid"], self.txid)
        self.assertEqual(broadcasts, [self.raw])

    def test_preview_committed_before_sign_is_superseded(self):
        at_precheck, release = threading.Event(), threading.Event()
        broadcasts = []

        def paused_precheck(method, params):
            if method == "getblockhash":
                return GENESIS
            if method == "testmempoolaccept":
                at_precheck.set()
                self.assertTrue(release.wait(timeout=10))
                return [{"allowed": True}]
            if method == "sendrawtransaction":
                broadcasts.append(params[0])
                return self.txid
            raise AssertionError(method)

        self.rpc.side_effect = paused_precheck
        signer = WalletService(self.root, self.connect, self.rpc, GENESIS, self.event)
        with patch.object(self.wallet, "_cli", side_effect=self.cli):
            draft = self.make_preview()
        result = {}

        def send_slowly():
            try:
                result["value"] = signer.send(draft)
            except WalletError as error:
                result["error"] = error.status

        with patch.object(signer, "_cli", side_effect=self.cli):
            thread = threading.Thread(target=send_slowly)
            thread.start()
            self.assertTrue(at_precheck.wait(timeout=10))
            # The signer has read the ready draft; commit a competing preview
            # while it is paused before its compare-and-swap.
            with patch.object(self.wallet, "_cli", side_effect=self.cli):
                preview = self.make_preview()
            release.set()
            thread.join(timeout=10)
        # The signer's transaction wins the outpoint; the later-inserted draft
        # is superseded in the same transaction and can never be sent.
        self.assertEqual(result["value"]["txid"], self.txid)
        self.assertEqual(broadcasts, [self.raw])
        with self.connect() as connection:
            statuses = sorted(
                row["status"]
                for row in connection.execute(
                    "SELECT status FROM wallet_transfers"
                ).fetchall()
            )
        self.assertEqual(statuses, ["broadcast", "superseded"])
        with patch.object(self.wallet, "_cli") as cli:
            with self.assertRaisesRegex(WalletError, "no longer usable"):
                self.wallet.send(preview)
        cli.assert_not_called()

    def test_abandon_reconciliation_commits_before_event_callback(self):
        with patch.object(self.wallet, "_cli", side_effect=self.cli):
            preview = self.make_preview()

        def uncertain(method, params):
            if method == "getblockhash":
                return GENESIS
            if method == "testmempoolaccept":
                return [{"allowed": True}]
            raise TimeoutError("Network unavailable")

        self.rpc.side_effect = uncertain
        with patch.object(self.wallet, "_cli", side_effect=self.cli):
            with self.assertRaisesRegex(WalletError, "transfer is saved"):
                self.wallet.send(preview)

        def present(method, params):
            if method == "getblockhash":
                return GENESIS
            if method == "getrawtransaction":
                return self.raw
            raise AssertionError(method)

        self.rpc.side_effect = present
        self.wallet.event = self.real_event
        with patch.object(self.wallet, "_cli", side_effect=self.cli):
            with self.assertRaisesRegex(WalletError, "already has this transaction"):
                self.wallet.abandon(
                    {"draft_id": preview["draft_id"], "confirmed": True}
                )
        # The signed->broadcast transition committed even though the event
        # callback opened a second connection mid-request, and the event row
        # itself persisted.
        self.assertEqual(self.wallet.snapshot()["transfers"][0]["status"], "broadcast")
        with self.connect() as connection:
            events = connection.execute(
                "SELECT level,message FROM events ORDER BY id"
            ).fetchall()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["level"], "info")
        self.assertIn("found on the node", events[0]["message"])

    def test_abandon_terminal_commits_before_event_callback(self):
        with patch.object(self.wallet, "_cli", side_effect=self.cli):
            preview = self.make_preview()

        def uncertain(method, params):
            if method == "getblockhash":
                return GENESIS
            if method == "testmempoolaccept":
                return [{"allowed": True}]
            raise TimeoutError("Network unavailable")

        self.rpc.side_effect = uncertain
        with patch.object(self.wallet, "_cli", side_effect=self.cli):
            with self.assertRaisesRegex(WalletError, "transfer is saved"):
                self.wallet.send(preview)

        def absent(method, params):
            if method == "getblockhash":
                return GENESIS
            raise RuntimeError(
                {"code": -5, "message": "No such mempool or blockchain transaction"}
            )

        self.rpc.side_effect = absent
        self.wallet.event = self.real_event
        with patch.object(self.wallet, "_cli", side_effect=self.cli):
            self.assertEqual(
                self.wallet.abandon(
                    {"draft_id": preview["draft_id"], "confirmed": True}
                )["status"],
                "abandoned",
            )
        with self.connect() as connection:
            status = connection.execute(
                "SELECT status FROM wallet_transfers WHERE id=?",
                (preview["draft_id"],),
            ).fetchone()["status"]
            events = connection.execute(
                "SELECT level,message FROM events ORDER BY id"
            ).fetchall()
        self.assertEqual(status, "abandoned")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["level"], "info")
        self.assertIn("abandoned by the operator", events[0]["message"])

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
