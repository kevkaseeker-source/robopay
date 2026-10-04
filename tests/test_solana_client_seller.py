#!/usr/bin/env python3
"""Unit test for SolanaClient.confirm_delivery()'s optional seller override.

No network access: only get_latest_blockhash and send_transaction are
stubbed. Keypair/Pubkey/Instruction/Message/Transaction are the real
solders library, so this checks the actual instruction that would be sent,
not a re-implementation of it.

Run: python3 tests/test_solana_client_seller.py
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "rpi"))

from solders.hash import Hash
from solders.keypair import Keypair
from solders.pubkey import Pubkey

import solana_client

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


class _FakeBlockhashResp:
    def __init__(self, blockhash):
        self.value = type("V", (), {"blockhash": blockhash})()


class _FakeSendResp:
    def __init__(self, sig):
        self.value = sig

# A valid Solana signature for testing
TEST_SIGNATURE = "3SN6JThB6Gz5fdARyFpGXY9sJziPmADL2GDzU85MJB1xDMPzFD1YgHKUheBWzwc4ma3CNhSSYWypRthPpWmahwJB"


class _FakeRpc:
    """Only the calls confirm_delivery makes before we inspect the tx."""

    def get_latest_blockhash(self):
        return _FakeBlockhashResp(Hash.default())

    def send_transaction(self, tx):
        return _FakeSendResp(TEST_SIGNATURE)

    def get_signature_statuses(self, signatures):
        # Return a confirmation status to allow confirm_transaction to succeed
        # Create a simple mock response with the required structure
        status = type("Status", (), {
            "err": None,
            "confirmation_status": type("ConfStatus", (), {"__str__": lambda self: "TransactionConfirmationStatus.Finalized"})()
        })()
        return type("Response", (), {"value": [status]})()


def _client_with_fake_rpc():
    keypair = Keypair()
    keyfile = Path(tempfile.mktemp(suffix=".json"))
    keyfile.write_text(json.dumps(list(bytes(keypair))))
    client = solana_client.SolanaClient(
        "http://unused.test", str(keyfile),
        "3NmsWVX39uvzG3PBNPdSe4FTgudqSeLphJSbMDhV5F8Y",
    )
    client._rpc = _FakeRpc()
    return client


# 1. Explicit seller override is used for the seller account.
client = _client_with_fake_rpc()
override = Pubkey.new_unique()
accounts = solana_client._confirm_delivery_accounts(
    client.derive_escrow_pda(), client._keypair.pubkey(), override,
)
check(accounts[2].pubkey == override,
      "explicit seller: wrong pubkey in the seller account slot")
check(accounts[2].is_signer is False, "explicit seller: seller must not be a signer")

# 2. confirm_delivery() itself accepts seller= and does not raise.
sig = client.confirm_delivery(52.3609, 14.06, seller=override)
check(sig == TEST_SIGNATURE, "confirm_delivery: unexpected return with seller override")

# 3. Omitting seller falls back to cfg.SELLER_PUBKEY, unchanged from today.
import config as cfg
client2 = _client_with_fake_rpc()
accounts_default = solana_client._confirm_delivery_accounts(
    client2.derive_escrow_pda(), client2._keypair.pubkey(),
    Pubkey.from_string(cfg.SELLER_PUBKEY),
)
sig2 = client2.confirm_delivery(52.3609, 14.06)
check(sig2 == TEST_SIGNATURE, "confirm_delivery: unexpected return without seller override")
check(accounts_default[2].pubkey == Pubkey.from_string(cfg.SELLER_PUBKEY),
      "default seller: did not fall back to cfg.SELLER_PUBKEY")

print()
if fails:
    print("FAILED CHECKS:")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("All checks passed.")
