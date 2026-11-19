"""A small testnet5 wallet adapter. BDK owns keys, coin selection and signing."""

import copy
from decimal import Decimal, InvalidOperation
import json
import logging
import re
import secrets
import subprocess
import threading
import time
import tomllib

import qrcode
from qrcode.image.svg import SvgPathImage

from embit import script
from embit.networks import NETWORKS
from embit.psbt import PSBT
from embit.transaction import Transaction

from .config import Config


class WalletError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# Bitcoin Core reject reasons proving a transaction can never confirm because
# its inputs are gone; such a transfer must not stay retryable forever.
SPENT_INPUT_REASONS = ("missing-inputs", "inputs-missingorspent", "mempool-conflict")

# Bitcoin Core's structured "transaction not found" RPC error, as returned by
# getrawtransaction for a transaction the node does not know.
TX_NOT_FOUND_CODE = -5


class WalletService:
    def __init__(self, root, connect, rpc, genesis, event):
        self.root, self.connect, self.rpc = root, connect, rpc
        self.genesis, self.event = genesis, event
        self.command = Config(state_dir=root).wallet_command
        self.lock = threading.Lock()
        self.cache = {
            "balance": None,
            "transactions": [],
            "receive": None,
            "updated_at": None,
        }
        self.error = None
        with self.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS wallet_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS wallet_transfers (
                    id TEXT PRIMARY KEY, created_at INTEGER, expires_at INTEGER,
                    status TEXT, summary TEXT, psbt TEXT, raw_tx TEXT, txid TEXT
                );
            """)

    def _check_chain(self):
        config = tomllib.loads(
            (self.root / "data/bdk-testnet5/config.toml").read_text()
        )["wallets"]["testnet5-payout"]
        if (
            config.get("network") != "testnet4"
            or config.get("genesis_hash") != self.genesis
        ):
            raise WalletError("Wallet configuration does not match testnet5.", 503)
        if self.rpc("getblockhash", [0]) != self.genesis:
            raise WalletError(
                "The Bitcoin backend is not the configured testnet5 chain.", 503
            )

    def _cli(self, *args):
        try:
            result = subprocess.run(
                [*self.command, *args], capture_output=True, timeout=40
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise WalletError(
                "BDK is unavailable or timed out. Try refreshing the wallet.", 503
            ) from error
        if result.returncode:
            logging.warning(
                "BDK command %s failed, exit %s", args[0], result.returncode
            )
            error = result.stderr.decode(errors="replace").lower()
            if "insufficient" in error:
                raise WalletError(
                    "Insufficient spendable funds for the amount plus the network fee."
                )
            if "dust" in error:
                raise WalletError(
                    "The amount is below the destination's minimum (dust) value."
                )
            raise WalletError(
                f"BDK could not complete {args[0]}. Refresh the wallet and check its configuration.",
                503,
            )
        if args[0] == "sync":
            return {}
        try:
            return json.loads(result.stdout)
        except (ValueError, TypeError) as error:
            raise WalletError("BDK returned an unexpected response.", 503) from error

    def _sync(self):
        self._check_chain()
        self._cli("sync")

    def _receive(self, new=False):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value FROM wallet_meta WHERE key='receive'"
            ).fetchone()
        if row and not new:
            return json.loads(row["value"])
        address = self._cli("new_address" if new else "unused_address")
        value = {"address": address["address"], "index": address["index"]}
        with self.connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO wallet_meta VALUES ('receive',?)",
                (json.dumps(value),),
            )
        return value

    def _refresh(self):
        self._sync()
        balance = self._cli("balance")
        transactions = self._cli("transactions")["items"]

        def newest(item):
            position = item.get("chain_position") or {}
            confirmed = position.get("Confirmed")
            if confirmed:
                return (
                    0,
                    confirmed.get("anchor", {}).get("block_id", {}).get("height", 0),
                )
            return (1, (position.get("Unconfirmed") or {}).get("last_seen") or 0)

        transactions.sort(key=newest, reverse=True)
        # Only public transaction information is returned to the browser.
        transactions = [
            {
                key: item.get(key)
                for key in (
                    "txid",
                    "is_coinbase",
                    "received",
                    "sent",
                    "fee",
                    "chain_position",
                )
            }
            for item in transactions[:100]
        ]
        self.cache = {
            "balance": balance,
            "transactions": transactions,
            "receive": self._receive(),
            "updated_at": int(time.time()),
        }
        self.error = None
        return balance

    def balance(self):
        with self.lock:
            try:
                return self._refresh()
            except Exception as error:
                self.error = (
                    str(error)
                    if isinstance(error, WalletError)
                    else "Wallet sync is currently unavailable."
                )
                raise

    def snapshot(self):
        data = copy.deepcopy(self.cache)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id,status,summary,txid FROM wallet_transfers WHERE status IN ('signed','broadcast','failed','superseded','abandoned') ORDER BY created_at DESC LIMIT 20"
            ).fetchall()
        data["transfers"] = [
            {**json.loads(row["summary"]), "status": row["status"], "txid": row["txid"]}
            for row in rows
        ]
        data["error"] = self.error
        data["network"] = "testnet5"
        return data

    def refresh(self):
        self.balance()
        return self.snapshot()

    def new_address(self):
        with self.lock:
            self._check_chain()
            address = self._receive(new=True)
            self.cache = {**self.cache, "receive": address}
            return address

    def receive_qr(self, address):
        if not address or address != (self.cache.get("receive") or {}).get("address"):
            raise WalletError("Refresh the receive address.", 409)
        return qrcode.make(
            "bitcoin:" + address, image_factory=SvgPathImage, border=4
        ).to_string()

    @staticmethod
    def validate_payment(data):
        address = data.get("address")
        if not isinstance(address, str) or len(address) > 100:
            raise WalletError("Enter a Bitcoin testnet address.")
        address = address.strip()
        try:
            output_script = script.address_to_scriptpubkey(address)
            canonical = output_script.address(NETWORKS["test"])
            compared = address.lower() if address.lower().startswith("tb1") else address
            if canonical != compared:
                raise ValueError("Wrong network")
        except Exception as error:
            raise WalletError(
                "Enter a valid Bitcoin testnet address (tb1…, m…, n… or 2…)."
            ) from error
        amount = data.get("amount_sat")
        if type(amount) is not int or not 1 <= amount <= 2_100_000_000_000_000:
            raise WalletError("Amount must be a positive whole number of test sats.")
        try:
            fee = Decimal(str(data.get("fee_rate")))
            if (
                not fee.is_finite()
                or not Decimal("0.1") <= fee <= Decimal("1000")
                or fee.as_tuple().exponent < -3
            ):
                raise ValueError("Invalid fee rate")
        except (InvalidOperation, ValueError):
            raise WalletError(
                "Fee rate must be 0.1–1,000 sat/vB, with at most three decimal places."
            )
        return canonical, amount, str(fee), output_script

    @staticmethod
    def inspect_psbt(encoded, recipient_script, amount):
        psbt = PSBT.from_base64(encoded)
        matches = [
            output
            for output in psbt.tx.vout
            if output.script_pubkey.data == recipient_script.data
        ]
        if len(matches) != 1 or matches[0].value != amount:
            raise WalletError(
                "The built transaction does not match the requested payment.", 503
            )
        fee = psbt.fee()
        if type(fee) is not int or fee < 0:
            raise WalletError("Could not determine the transaction fee.", 503)
        change = sum(output.value for output in psbt.tx.vout) - amount
        return psbt, fee, change

    @staticmethod
    def _mark_terminal(connection, identifier, status, reason, expected):
        """Compare-and-swap a transfer into a terminal status.

        The row only transitions while it still holds `expected`, so two
        WalletService processes sharing one database can never both move the
        same transfer, and a terminal row can never be revived. Terminal
        transfers never block previews and can never be signed or broadcast
        again. Returns True when this call performed the transition.
        """
        row = connection.execute(
            "SELECT summary FROM wallet_transfers WHERE id=?", (identifier,)
        ).fetchone()
        if row is None:
            return False
        summary = {**json.loads(row["summary"]), "reason": reason}
        cursor = connection.execute(
            "UPDATE wallet_transfers SET status=?,summary=? WHERE id=? AND status=?",
            (status, json.dumps(summary), identifier, expected),
        )
        return cursor.rowcount == 1

    def _supersede_ready(self, connection, identifier):
        """Withdraw every other ready draft when one draft is signed.

        Two ready drafts can select the same wallet inputs (two browser
        tabs). Once one of them is signed, the others must never become a
        second spend of those inputs. The row guard keeps this safe across
        processes: only rows that are still ready are withdrawn.
        """
        rows = connection.execute(
            "SELECT id,summary FROM wallet_transfers WHERE status='ready' AND id<>?",
            (identifier,),
        ).fetchall()
        for row in rows:
            summary = {
                **json.loads(row["summary"]),
                "reason": "Another payment was signed first; this preview was withdrawn.",
            }
            connection.execute(
                "UPDATE wallet_transfers SET status='superseded',summary=? "
                "WHERE id=? AND status='ready'",
                (json.dumps(summary), row["id"]),
            )

    def _mempool_rejection(self, raw):
        """Return the node's reject reason for this raw transaction.

        Returns None when the node accepts the transaction into its mempool.
        Raises (503) when the node cannot answer, leaving the draft ready:
        nothing has been broadcast, so guessing either way is unsafe.
        """
        try:
            results = self.rpc("testmempoolaccept", [[raw]])
        except Exception as error:
            raise WalletError(
                "The Bitcoin backend could not pre-check the transaction. Try sending again.",
                503,
            ) from error
        result = results[0] if isinstance(results, list) and len(results) == 1 else None
        if not isinstance(result, dict):
            raise WalletError(
                "The Bitcoin backend returned an unexpected response.", 503
            )
        if result.get("allowed") is True:
            return None
        return str(result.get("reject-reason") or "transaction not accepted").strip()

    @staticmethod
    def _inputs_are_gone(error):
        message = str(error).lower()
        return any(reason in message for reason in SPENT_INPUT_REASONS)

    @staticmethod
    def _tx_absent(error):
        """True only for a definitive node answer that the tx is unknown.

        Bitcoin Core reports a missing transaction as a structured RPC error
        with code -5. Timeouts, disconnects, auth or backend errors and
        unstructured exceptions are never treated as proof of absence.
        """
        for detail in error.args:
            if isinstance(detail, dict) and detail.get("code") == TX_NOT_FOUND_CODE:
                return True
        return False

    def _found_on_node(self, txid):
        """Return True when the node knows the exact transaction.

        Absence is only accepted from a structured not-found error; any other
        failure or a malformed response raises so the transfer stays signed.
        """
        try:
            known = self.rpc("getrawtransaction", [txid, False])
        except Exception as error:
            if self._tx_absent(error):
                return False
            raise WalletError(
                "The Bitcoin backend could not confirm whether the transaction "
                "exists. Try again later; retrying the same transfer stays safe.",
                503,
            ) from error
        if not isinstance(known, str) or not known:
            raise WalletError(
                "The Bitcoin backend returned an unexpected response.", 503
            )
        try:
            match = Transaction.parse(bytes.fromhex(known)).txid().hex() == txid
        except Exception as error:
            raise WalletError(
                "The Bitcoin backend returned an unexpected response.", 503
            ) from error
        if not match:
            raise WalletError(
                "The Bitcoin backend returned an unexpected response.", 503
            )
        return True

    def _found_in_wallet(self, txid):
        """Sync once and look for the transaction in the wallet's history.

        A failed sync raises, so it can never be mistaken for absence: a
        transaction that already confirmed through the wallet's own backend
        is reconciled instead of abandoned.
        """
        self._sync()
        items = self._cli("transactions")["items"]
        return any(item.get("txid") == txid for item in items)

    def preview(self, data):
        address, amount, fee_rate, output_script = self.validate_payment(data)
        with self.lock:
            self._sync()
            with self.connect() as connection:
                if connection.execute(
                    "SELECT 1 FROM wallet_transfers WHERE status='signed'"
                ).fetchone():
                    raise WalletError(
                        "Resolve the saved pending transfer before creating another payment.",
                        409,
                    )
            balance = self._cli("balance")
            if amount >= balance["confirmed"] + balance["trusted_pending"]:
                raise WalletError(
                    "Insufficient spendable funds for the amount plus the network fee."
                )
            encoded = self._cli(
                "create_tx", "--to", f"{address}:{amount}", "--fee_rate", fee_rate
            )["psbt"]
            psbt, fee, change = self.inspect_psbt(encoded, output_script, amount)
            now = int(time.time())
            identifier = secrets.token_urlsafe(24)
            summary = {
                "draft_id": identifier,
                "address": address,
                "amount_sat": amount,
                "fee_sat": fee,
                "total_sat": amount + fee,
                "change_sat": change,
                "fee_rate": fee_rate,
                "input_count": len(psbt.tx.vin),
                "expires_at": now + 600,
            }
            with self.connect() as connection:
                connection.execute(
                    "DELETE FROM wallet_transfers WHERE expires_at < ? AND status IN ('ready','failed','superseded','abandoned')",
                    (now,),
                )
                # Insert atomically only while no signed transfer exists:
                # another process can sign between the check above and this
                # insert, and a ready draft created after that could become a
                # second spend of the same inputs. If signing commits first
                # this insert matches zero rows; if this insert commits first,
                # signing's supersede step withdraws the new draft.
                cursor = connection.execute(
                    "INSERT INTO wallet_transfers "
                    "SELECT ?,?,?,'ready',?,?,NULL,NULL "
                    "WHERE NOT EXISTS (SELECT 1 FROM wallet_transfers WHERE status='signed')",
                    (identifier, now, now + 600, json.dumps(summary), encoded),
                )
                if cursor.rowcount != 1:
                    raise WalletError(
                        "Resolve the saved pending transfer before creating another payment.",
                        409,
                    )
            return summary

    def send(self, data):
        identifier = data.get("draft_id")
        if not isinstance(identifier, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{32}", identifier
        ):
            raise WalletError("Create a payment preview first.")
        with self.lock:
            with self.connect() as connection:
                row = connection.execute(
                    "SELECT * FROM wallet_transfers WHERE id=?", (identifier,)
                ).fetchone()
            if row is None:
                raise WalletError(
                    "Payment preview not found. Create a new preview.", 404
                )
            if row["status"] == "broadcast":
                return {
                    "txid": row["txid"],
                    "status": "broadcast",
                    "draft_id": identifier,
                }
            if row["status"] != "ready" and row["status"] != "signed":
                # failed, superseded and abandoned are terminal: they can
                # never be signed or broadcast again.
                raise WalletError(
                    "This transfer is no longer usable. Create a new preview.", 409
                )
            if row["status"] == "ready" and row["expires_at"] < time.time():
                raise WalletError("This preview expired. Create a new preview.", 409)
            self._check_chain()
            summary = json.loads(row["summary"])
            raw, txid = row["raw_tx"], row["txid"]
            if row["status"] == "ready":
                signed = self._cli("sign", row["psbt"])
                if signed.get("is_finalized") is not True:
                    raise WalletError("BDK could not fully sign this transaction.", 503)
                recipient = script.address_to_scriptpubkey(summary["address"])
                original = PSBT.from_base64(row["psbt"])
                checked, fee, _ = self.inspect_psbt(
                    signed["psbt"], recipient, summary["amount_sat"]
                )
                if (
                    checked.tx.serialize() != original.tx.serialize()
                    or fee != summary["fee_sat"]
                ):
                    raise WalletError(
                        "Signed transaction differs from the payment preview.", 503
                    )
                raw = self._cli("extract_psbt", signed["psbt"])["raw_tx"]
                txid = Transaction.parse(bytes.fromhex(raw)).txid().hex()
                if txid != original.tx.txid().hex():
                    raise WalletError(
                        "Signed transaction identity differs from the preview.", 503
                    )
                rejection = self._mempool_rejection(raw)
                # A reject reason mentioning "already" (txn-already-in-mempool)
                # means this exact transaction may already be live; failing the
                # draft could invite a second payment, so let the broadcast
                # attempt below confirm it instead.
                if rejection is not None and "already" not in rejection.lower():
                    # The node refuses this exact transaction, so it can never
                    # broadcast. Fail the draft before saving retryable signed
                    # bytes so previews stay unblocked.
                    logging.warning(
                        "Wallet transfer %s rejected by the node: %s",
                        identifier,
                        rejection,
                    )
                    with self.connect() as connection:
                        transitioned = self._mark_terminal(
                            connection,
                            identifier,
                            "failed",
                            f"The node rejected the transaction: {rejection}.",
                            "ready",
                        )
                    if transitioned:
                        raise WalletError(
                            f"The node rejected the transaction ({rejection}). "
                            "Create a new preview.",
                            409,
                        )
                    # Another process moved this draft first; do not report a
                    # rejection that no longer applies to it.
                    raise WalletError(
                        "This preview was already sent or withdrawn. Create a new preview.",
                        409,
                    )
                # Save the exact signed bytes before broadcasting so retries cannot build a second payment.
                with self.connect() as connection:
                    # Compare-and-swap: only the first process to move this
                    # draft from ready to signed may broadcast it.
                    cursor = connection.execute(
                        "UPDATE wallet_transfers SET status='signed',raw_tx=?,txid=? "
                        "WHERE id=? AND status='ready'",
                        (raw, txid, identifier),
                    )
                    if cursor.rowcount != 1:
                        raise WalletError(
                            "This preview was already sent or withdrawn. "
                            "Create a new preview.",
                            409,
                        )
                    # Competing ready drafts may spend the same inputs and must
                    # never become a second signed transaction.
                    self._supersede_ready(connection, identifier)
            try:
                result = self.rpc("sendrawtransaction", [raw])
                if result != txid:
                    raise RuntimeError("Unexpected broadcast transaction ID")
            except Exception as error:
                try:
                    known = self.rpc("getrawtransaction", [txid, False])
                    if Transaction.parse(bytes.fromhex(known)).txid().hex() != txid:
                        raise ValueError("Transaction not found")
                except Exception:
                    if self._inputs_are_gone(error):
                        # The node reports the inputs as missing or already
                        # spent, so this transaction can never confirm. Keep it
                        # terminal instead of retryable forever.
                        logging.warning(
                            "Wallet transfer %s inputs missing or spent: %s",
                            identifier,
                            error,
                        )
                        with self.connect() as connection:
                            self._mark_terminal(
                                connection,
                                identifier,
                                "failed",
                                "The transaction's inputs are missing or already spent.",
                                "signed",
                            )
                        raise WalletError(
                            "The transaction's inputs are missing or already spent; "
                            "the transfer was discarded. Create a new preview.",
                            409,
                        ) from error
                    raise WalletError(
                        "Broadcast could not be confirmed. The transfer is saved; retry this same transfer instead of creating another payment.",
                        503,
                    ) from error
            with self.connect() as connection:
                # Compare-and-swap: never overwrite a status another process
                # resolved while the broadcast was in flight.
                cursor = connection.execute(
                    "UPDATE wallet_transfers SET status='broadcast' "
                    "WHERE id=? AND status='signed'",
                    (identifier,),
                )
                if cursor.rowcount != 1:
                    resolved = connection.execute(
                        "SELECT status,txid FROM wallet_transfers WHERE id=?",
                        (identifier,),
                    ).fetchone()
                    if (
                        resolved is not None
                        and resolved["status"] == "broadcast"
                        and resolved["txid"] == txid
                    ):
                        return {
                            "txid": txid,
                            "status": "broadcast",
                            "draft_id": identifier,
                        }
                    raise WalletError(
                        "This transfer was already resolved otherwise. "
                        "Check the wallet before creating a new preview.",
                        409,
                    )
            self.event(
                "info",
                f"Testnet5 wallet sent {summary['amount_sat']} sats; transaction {txid}",
            )
            return {"txid": txid, "status": "broadcast", "draft_id": identifier}

    def abandon(self, data):
        """Operator recovery for a signed transfer that cannot broadcast.

        Only a saved (signed) transfer can be abandoned, only with an
        explicit confirmation, and only when neither the node nor the
        wallet's own backend knows its transaction; if either has it, the
        transfer is reconciled to broadcast instead. Abandoning is terminal:
        the saved bytes can never be broadcast again and the operator is
        responsible for the risk that the absent transaction later
        propagates.
        """
        identifier = data.get("draft_id")
        if not isinstance(identifier, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{32}", identifier
        ):
            raise WalletError("Create a payment preview first.")
        if data.get("confirmed") is not True:
            raise WalletError(
                "Abandoning is irreversible; confirm that the saved transfer "
                "should be discarded and that its transaction is not propagating.",
                400,
            )
        with self.lock:
            with self.connect() as connection:
                row = connection.execute(
                    "SELECT * FROM wallet_transfers WHERE id=?", (identifier,)
                ).fetchone()
            if row is None:
                raise WalletError(
                    "Payment preview not found. Create a new preview.", 404
                )
            if row["status"] != "signed":
                raise WalletError(
                    "Only a saved transfer awaiting broadcast can be abandoned.", 409
                )
            self._check_chain()
            txid = row["txid"]
            if self._found_on_node(txid) or self._found_in_wallet(txid):
                # The transaction is live: never abandon a payment that may
                # already be propagating. Reconcile with a compare-and-swap
                # so a concurrent resolution is never overwritten or double
                # reported.
                reconciled = False
                with self.connect() as connection:
                    cursor = connection.execute(
                        "UPDATE wallet_transfers SET status='broadcast' "
                        "WHERE id=? AND status='signed'",
                        (identifier,),
                    )
                    if cursor.rowcount == 1:
                        reconciled = True
                    else:
                        resolved = connection.execute(
                            "SELECT status,txid FROM wallet_transfers WHERE id=?",
                            (identifier,),
                        ).fetchone()
                        if not (
                            resolved is not None
                            and resolved["status"] == "broadcast"
                            and resolved["txid"] == txid
                        ):
                            raise WalletError(
                                "This transfer was already resolved otherwise. "
                                "Check the wallet before creating a new preview.",
                                409,
                            )
                # The connection context has exited and the update is committed
                # before this runs: the event callback opens its own database
                # connection and must never block on our write lock.
                if reconciled:
                    self.event(
                        "info",
                        f"Testnet5 wallet transaction {txid} found on the node; "
                        "transfer marked broadcast.",
                    )
                raise WalletError(
                    "The node already has this transaction; the transfer was "
                    "marked broadcast and stays safe to retry.",
                    409,
                )
            logging.warning("Wallet transfer %s abandoned by operator", identifier)
            with self.connect() as connection:
                transitioned = self._mark_terminal(
                    connection,
                    identifier,
                    "abandoned",
                    "Abandoned by the operator after broadcast could not be confirmed; "
                    "the transaction was not known to the node.",
                    "signed",
                )
            if not transitioned:
                raise WalletError(
                    "This transfer was already resolved. Create a new preview.", 409
                )
            self.event(
                "info",
                "A saved testnet5 wallet transfer was abandoned by the operator.",
            )
            return {"draft_id": identifier, "status": "abandoned"}
