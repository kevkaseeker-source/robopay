#!/usr/bin/env python3
"""Offline test for rpi/payout_policy.py - the rules for who gets paid for a
delivery and when the car must refuse.

Kevin's rule (2026-10-05): the car's ownership must not change during a
delivery. The escrow's seller is fixed when the buyer orders; the car checks
before driving that this is the machine's current owner, and checks again
before paying - if the owner changed in between, nobody is paid and the
buyer gets a refund after the deadline.

Run: python tests/test_payout_policy.py
"""
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "rpi"))

from solders.keypair import Keypair  # noqa: E402

import payout_policy as pp  # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


def raises(fn, *args, contains=None, exc=pp.PayoutRefused):
    try:
        fn(*args)
    except exc as e:
        return contains is None or contains in str(e)
    return False


A, B, BUYER, OP = (Keypair().pubkey() for _ in range(4))
NOW = 1_800_000_000


def escrow(seller=A, status=pp.STATUS_PENDING, deadline=NOW + 600):
    return {"buyer": str(BUYER), "seller": str(seller), "drone_operator": str(OP),
            "amount_lamports": 200_000_000, "target_lat_e7": 0, "target_lon_e7": 0,
            "deadline": deadline, "status": status, "bump": 255}


# 1. decode_escrow round-trips the on-chain layout (8-byte discriminator,
#    buyer, seller, operator, amount, lat, lon, deadline, status, bump).
raw = (b"\x00" * 8 + bytes(BUYER) + bytes(A) + bytes(OP)
       + struct.pack("<Qqqq", 200_000_000, 523609000, 140600000, NOW + 60) + bytes([0, 254]))
d = pp.decode_escrow(raw)
check(d["seller"] == str(A) and d["buyer"] == str(BUYER) and d["drone_operator"] == str(OP), "decode: pubkeys")
check(d["amount_lamports"] == 200_000_000 and d["deadline"] == NOW + 60, "decode: amount/deadline")
check(d["status"] == pp.STATUS_PENDING and d["bump"] == 254, "decode: status/bump")
check(raises(pp.decode_escrow, raw[:-1], exc=ValueError), "decode: wrong length must raise")

# 2. Before driving: accept a pending, unexpired escrow that pays the current owner.
try:
    pp.check_order(escrow(), A, NOW)
except pp.PayoutRefused as e:
    fails.append(f"check_order rejected a valid order: {e}")

# 3. Before driving: refuse everything else, with a reason a human can act on.
check(raises(pp.check_order, None, A, NOW, contains="no escrow"), "check_order: missing escrow")
check(raises(pp.check_order, escrow(status=pp.STATUS_DELIVERED), A, NOW, contains="DELIVERED"),
      "check_order: not pending")
check(raises(pp.check_order, escrow(deadline=NOW - 1), A, NOW, contains="deadline"),
      "check_order: expired")
check(raises(pp.check_order, escrow(seller=B), A, NOW, contains="current owner"),
      "check_order: escrow pays someone who is not the owner")

# 4. Before paying: same owner -> pay the escrow's seller; owner changed -> refuse.
try:
    check(str(pp.check_payout(escrow(), A)) == str(A), "check_payout: must return the escrow's seller")
except pp.PayoutRefused as e:
    fails.append(f"check_payout refused an unchanged owner: {e}")
check(raises(pp.check_payout, escrow(seller=A), B, contains="changed"),
      "check_payout: ownership changed during the delivery must refuse")

# 5. Resolving the owner: static mode uses the configured key, DID mode needs a
#    machine ID and never falls back to the static key.
check(str(pp.resolve_payout_owner("static", None, "http://unused", str(B))) == str(B), "resolve: static")
check(raises(pp.resolve_payout_owner, "did", None, "http://unused", str(B), contains="MACHINE_ID"),
      "resolve: did without machine id must refuse")
check(raises(pp.resolve_payout_owner, "sometimes", 1, "http://unused", str(B), contains="PAYOUT_MODE"),
      "resolve: unknown mode must refuse")


class FakePeaq:
    """Stands in for peaq_ownership: build_client + current_owner."""
    class OwnerLookupError(Exception):
        pass

    def __init__(self, result):
        self.result, self.built = result, 0

    def build_client(self, url):
        self.built += 1
        return object()

    def current_owner(self, client, machine_id):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


fake = FakePeaq(A)
pp._peaq, pp._clients = fake, {}
check(str(pp.resolve_payout_owner("did", 42, "http://rpc-1", str(B))) == str(A), "resolve: did returns DID owner")
pp.resolve_payout_owner("did", 42, "http://rpc-1", str(B))
check(fake.built == 1, "resolve: the heavy SDK client must be built once per RPC URL")
pp._peaq = FakePeaq(FakePeaq.OwnerLookupError("rpc down"))
check(raises(pp.resolve_payout_owner, "did", 42, "http://rpc-2", str(B), contains="rpc down"),
      "resolve: lookup failure must refuse, not fall back to the static key")

print()
if fails:
    print("FAILED CHECKS:")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("All checks passed.")
