import hashlib
import json
import logging
import struct
import time
from pathlib import Path

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.instruction import AccountMeta, Instruction
from solders.message import Message
from solders.transaction import Transaction
from solana.rpc.api import Client

import config as cfg
import payout_policy

log = logging.getLogger(__name__)


def _disc(name: str) -> bytes:
    return hashlib.sha256(f"global:{name}".encode()).digest()[:8]


CONFIRM_DELIVERY_DISC = _disc("confirm_delivery")
CLOSE_ESCROW_DISC = _disc("close_escrow")
CREATE_DELIVERY_DISC = _disc("create_delivery")
CANCEL_DELIVERY_DISC = _disc("cancel_delivery")

MEMO_PROGRAM_ID = Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")
SYSTEM_PROGRAM_ID = Pubkey.from_string("11111111111111111111111111111111")


def _load_keypair(path):
    data = json.loads(Path(path).read_text())
    return Keypair.from_bytes(bytes(data))


def _confirm_delivery_accounts(escrow_pda: Pubkey, signer: Pubkey, seller: Pubkey) -> list:
    """The three accounts confirm_delivery signs against, isolated so the
    seller substitution (tokenized-ownership payout) is directly testable
    without inspecting a serialized Transaction."""
    return [
        AccountMeta(pubkey=escrow_pda, is_signer=False, is_writable=True),
        AccountMeta(pubkey=signer, is_signer=True, is_writable=True),
        AccountMeta(pubkey=seller, is_signer=False, is_writable=True),
    ]


class SolanaClient:
    def __init__(self, rpc_url, keypair_path, program_id):
        self._rpc = Client(rpc_url)
        self._keypair = _load_keypair(keypair_path)
        self._program_id = Pubkey.from_string(program_id)
        log.info("Drone operator wallet: %s", self._keypair.pubkey())

    def derive_escrow_pda(self) -> Pubkey:
        pda, _ = Pubkey.find_program_address(
            [b"escrow", bytes(self._keypair.pubkey())], self._program_id
        )
        return pda

    def read_escrow(self):
        """This car's escrow account, decoded (see payout_policy), or None."""
        return payout_policy.read_escrow(self._rpc, self.derive_escrow_pda())

    def derive_escrow_pda_for(self, operator: Pubkey) -> Pubkey:
        """Same derivation as derive_escrow_pda(), but for an operator that is
        not this client's own keypair. Needed on the buyer side: the escrow
        PDA is seeded with the *operator's* pubkey (Section 6.4), not the
        buyer's, so a buyer client must be able to compute the address of the
        escrow it is about to fund before it has any other way to know it."""
        pda, _ = Pubkey.find_program_address([b"escrow", bytes(operator)], self._program_id)
        return pda

    def create_delivery(self, operator: Pubkey, lat: float, lon: float,
                         amount_sol: float, deadline_minutes: int,
                         seller: Pubkey = None) -> str:
        """Fund escrow as the buyer: this client's keypair signs and pays.

        Mirrors car/buyer_app.py's create_delivery() byte-for-byte (same
        discriminator, same struct.pack layout, same account order) so a
        buyer-side script that is not a Flask request handler - agent/
        kitchen_agent.py - can fund an order without duplicating the
        instruction-building logic in a second place. seller overrides
        cfg.SELLER_PUBKEY for this call only, same convention as
        confirm_delivery() above.
        """
        amount_lamports = int(amount_sol * 1_000_000_000)
        deadline = int(time.time()) + deadline_minutes * 60
        data = (CREATE_DELIVERY_DISC
                + struct.pack("<Qqqq", amount_lamports, int(lat * 1e7), int(lon * 1e7), deadline))
        escrow_pda = self.derive_escrow_pda_for(operator)
        seller_pubkey = seller if seller is not None else Pubkey.from_string(cfg.SELLER_PUBKEY)
        accounts = [
            AccountMeta(pubkey=escrow_pda, is_signer=False, is_writable=True),
            AccountMeta(pubkey=self._keypair.pubkey(), is_signer=True, is_writable=True),
            AccountMeta(pubkey=seller_pubkey, is_signer=False, is_writable=False),
            AccountMeta(pubkey=operator, is_signer=False, is_writable=False),
            AccountMeta(pubkey=SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
        ]
        ix = Instruction(program_id=self._program_id, accounts=accounts, data=data)
        try:
            blockhash = self._rpc.get_latest_blockhash().value.blockhash
            msg = Message.new_with_blockhash([ix], self._keypair.pubkey(), blockhash)
            tx = Transaction([self._keypair], msg, blockhash)
            sig = str(self._rpc.send_transaction(tx).value)
            log.info("create_delivery TX sent: %s (escrow %s)", sig, escrow_pda)
            return sig
        except Exception as e:
            raise RuntimeError(f"create_delivery send failed: {e}")

    def cancel_delivery(self, operator: Pubkey) -> str:
        """Reclaim escrow before the deadline: only the buyer who funded it
        can sign this (Anchor's close = buyer constraint), so this must be
        called with the SAME keypair that called create_delivery for this
        operator - never the operator's or a different buyer's."""
        escrow_pda = self.derive_escrow_pda_for(operator)
        accounts = [
            AccountMeta(pubkey=escrow_pda, is_signer=False, is_writable=True),
            AccountMeta(pubkey=self._keypair.pubkey(), is_signer=True, is_writable=True),
        ]
        ix = Instruction(program_id=self._program_id, accounts=accounts, data=CANCEL_DELIVERY_DISC)
        blockhash = self._rpc.get_latest_blockhash().value.blockhash
        msg = Message.new_with_blockhash([ix], self._keypair.pubkey(), blockhash)
        tx = Transaction([self._keypair], msg, blockhash)
        sig = str(self._rpc.send_transaction(tx).value)
        log.info("cancel_delivery TX sent: %s", sig)
        return sig

    def confirm_delivery(self, actual_lat: float, actual_lon: float, timestamp: int = None,
                          seller: Pubkey = None) -> str:
        """GPS-verified payment release: escrow → seller. Retries up to 3x with backoff.

        seller overrides cfg.SELLER_PUBKEY for this call only - used to pay
        the current peaq Machine-NFT owner instead of the static configured
        address. See docs/superpowers/specs/2026-09-23-tokenized-ownership-design.md.
        """
        ts = int(timestamp or time.time())
        data = (CONFIRM_DELIVERY_DISC
                + struct.pack("<qqq", int(actual_lat * 1e7), int(actual_lon * 1e7), ts))

        escrow_pda = self.derive_escrow_pda()
        seller_pubkey = seller if seller is not None else Pubkey.from_string(cfg.SELLER_PUBKEY)
        accounts = _confirm_delivery_accounts(escrow_pda, self._keypair.pubkey(), seller_pubkey)
        ix = Instruction(program_id=self._program_id, accounts=accounts, data=data)

        # Build and send TX exactly once — reuse signature on confirmation retries
        # to avoid double-payment from duplicate transactions.
        try:
            blockhash = self._rpc.get_latest_blockhash().value.blockhash
            msg = Message.new_with_blockhash([ix], self._keypair.pubkey(), blockhash)
            tx = Transaction([self._keypair], msg, blockhash)
            sig = str(self._rpc.send_transaction(tx).value)
            log.info("confirm_delivery TX sent: %s", sig)
        except Exception as e:
            raise RuntimeError(f"confirm_delivery send failed: {e}")

        for attempt in range(1, 4):
            try:
                if self.confirm_transaction(sig):
                    return sig
                log.warning("TX not confirmed on attempt %d, retrying confirmation...", attempt)
            except Exception as e:
                log.warning("confirm_transaction attempt %d error: %s", attempt, e)
            if attempt < 3:
                time.sleep(2 ** attempt)

        raise RuntimeError(f"confirm_delivery TX sent but not confirmed after 3 attempts: {sig}")

    def close_escrow(self) -> str:
        """Close delivered escrow and reclaim rent to drone operator."""
        escrow_pda = self.derive_escrow_pda()
        accounts = [
            AccountMeta(pubkey=escrow_pda, is_signer=False, is_writable=True),
            AccountMeta(pubkey=self._keypair.pubkey(), is_signer=True, is_writable=True),
        ]
        ix = Instruction(program_id=self._program_id, accounts=accounts, data=CLOSE_ESCROW_DISC)
        blockhash = self._rpc.get_latest_blockhash().value.blockhash
        msg = Message.new_with_blockhash([ix], self._keypair.pubkey(), blockhash)
        tx = Transaction([self._keypair], msg, blockhash)
        sig = str(self._rpc.send_transaction(tx).value)
        log.info("close_escrow TX: %s", sig)
        return sig

    def pay_memo(self, to_pubkey: str, lamports: int, memo: str) -> str:
        """SOL transfer + Memo, tagged with an arbitrary reference string.

        Used by agent/delivery_agent.py's x402 second-opinion tool to pay
        agent/x402_verifier.py - the memo carries the request's reference so
        the verifier can match a specific payment to a specific verification
        request instead of trusting the client's word for it. Not used by the
        confirm_delivery/close_escrow flow above, which talks to the escrow
        program directly rather than moving SOL peer-to-peer.
        """
        to = Pubkey.from_string(to_pubkey)
        transfer_ix = Instruction(
            program_id=SYSTEM_PROGRAM_ID,
            accounts=[
                AccountMeta(pubkey=self._keypair.pubkey(), is_signer=True, is_writable=True),
                AccountMeta(pubkey=to, is_signer=False, is_writable=True),
            ],
            # System Program Transfer: u32 instruction index (2) + u64 lamports, both LE.
            data=struct.pack("<IQ", 2, lamports),
        )
        memo_ix = Instruction(
            program_id=MEMO_PROGRAM_ID,
            accounts=[AccountMeta(pubkey=self._keypair.pubkey(), is_signer=True, is_writable=False)],
            data=memo.encode("utf-8"),
        )
        blockhash = self._rpc.get_latest_blockhash().value.blockhash
        msg = Message.new_with_blockhash([transfer_ix, memo_ix], self._keypair.pubkey(), blockhash)
        tx = Transaction([self._keypair], msg, blockhash)
        sig = str(self._rpc.send_transaction(tx).value)
        log.info("pay_memo TX sent: %s (%d lamports to %s, memo %r)", sig, lamports, to_pubkey, memo)
        if not self.confirm_transaction(sig):
            raise RuntimeError(f"pay_memo TX sent but not confirmed: {sig}")
        return sig

    def confirm_transaction(self, signature: str, timeout: float = 60.0) -> bool:
        sig_obj = Signature.from_string(signature)
        deadline = time.time() + timeout
        while time.time() < deadline:
            resp = self._rpc.get_signature_statuses([sig_obj])
            status = resp.value[0]
            if status:
                if status.err is not None:
                    log.error("TX failed on-chain: %s", status.err)
                    return False
                # confirmation_status is a solders enum whose str() is e.g.
                # "TransactionConfirmationStatus.Finalized", not the plain
                # string "finalized" - an exact-match/membership check
                # against ("confirmed", "finalized") silently never matches.
                # Found 2026-09-16: a transaction that actually succeeded
                # on-chain within seconds still made this loop burn the
                # full timeout on every attempt before giving up, even
                # though the payout had already gone through.
                conf = str(status.confirmation_status).lower()
                if "confirmed" in conf or "finalized" in conf:
                    return True
            time.sleep(2.0)
        log.warning("TX confirmation timeout: %s", signature)
        return False
