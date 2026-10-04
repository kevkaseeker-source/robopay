#!/usr/bin/env python3
"""Offline test for buyer_app.py's /delivered handler - confirms the new
escrow_tx field lands in tx_history without changing existing behavior.

Run: python tests/test_buyer_app_delivered.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "car"))

os.environ.setdefault("BUYER_USERNAME", "test")
os.environ.setdefault("BUYER_PASSWORD", "test")
os.environ.setdefault("SELLER_USERNAME", "test")
os.environ.setdefault("SELLER_PASSWORD", "test")
os.environ["MACHINE_TOKEN"] = "machine-secret-for-tests"
MACHINE = {"X-RoboPay-Machine-Token": "machine-secret-for-tests"}

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


# A throwaway keypair file - buyer_app.py loads one at import time.
from solders.keypair import Keypair
kp = Keypair()
keyfile = Path(tempfile.mktemp(suffix=".json"))
keyfile.write_text(json.dumps(list(bytes(kp))))
os.environ["BUYER_KEYPAIR_PATH"] = str(keyfile)

import robopay_common as common
common.STATE_FILE = Path(tempfile.mktemp(suffix=".json"))

import buyer_app

client = buyer_app.app.test_client()


def _order(escrow_tx):
    return {
        "lat": 52.3609, "lon": 14.06, "buyer_pubkey": "BUYERPK111",
        "escrow_tx": escrow_tx, "status": "pending", "delivery_tx": None,
        "ordered_at": 0.0, "trigger_mode": "fixed",
    }


# 1. Happy path: escrow_tx lands in the new tx_history entry, existing
# fields/behavior unchanged.
buyer_app._active_order = _order("ESCROWTX555")
buyer_app._tx_history = []
resp = client.post("/delivered", json={"delivery_tx": "SIGCONFIRM999"}, headers=MACHINE)
check(resp.status_code == 200, "delivered: unexpected status %s" % resp.status_code)
check(buyer_app._active_order["status"] == "delivered",
      "delivered: order status not updated (existing behavior)")
check(buyer_app._active_order["delivery_tx"] == "SIGCONFIRM999",
      "delivered: delivery_tx not recorded (existing behavior)")
check(len(buyer_app._tx_history) == 1, "delivered: expected exactly one tx_history entry")
if buyer_app._tx_history:
    entry = buyer_app._tx_history[0]
    check(entry["type"] == "confirm_delivery", "delivered: wrong entry type")
    check(entry["sig"] == "SIGCONFIRM999", "delivered: wrong sig")
    check(entry["escrow_tx"] == "ESCROWTX555", "delivered: escrow_tx not included (the new field)")

# 2. dry-run-tx must still be excluded from tx_history (existing behavior,
# unchanged by this task).
buyer_app._active_order = _order("ESCROWTX666")
buyer_app._tx_history = []
resp = client.post("/delivered", json={"delivery_tx": "dry-run-tx"}, headers=MACHINE)
check(resp.status_code == 200, "dry_run: unexpected status %s" % resp.status_code)
check(len(buyer_app._tx_history) == 0,
      "dry_run: dry-run-tx should still be excluded from tx_history (existing behavior)")

# 3. Machine endpoints reject callers without the shared machine token.
# They skip the human login, so without this anyone who reads the public
# code could fake a delivery or block the queue with a fake order.
for bad in ({}, {"X-RoboPay-Machine-Token": "wrong"}):
    buyer_app._active_order = _order("ESCROWTX777")
    buyer_app._tx_history = []
    resp = client.post("/delivered", json={"delivery_tx": "FAKESIG"}, headers=bad)
    check(resp.status_code == 401, "token %r: /delivered answered %s, expected 401" % (bad, resp.status_code))
    check(buyer_app._active_order["status"] == "pending", "token %r: /delivered changed the order" % (bad,))
    check(buyer_app._tx_history == [], "token %r: /delivered wrote tx_history" % (bad,))
    for path in ("/active_order", "/force_delivery"):
        r = client.get(path, headers=bad)
        check(r.status_code == 401, "token %r: GET %s answered %s, expected 401" % (bad, path, r.status_code))
    r = client.post("/rpi_log", json={"msg": "x"}, headers=bad)
    check(r.status_code == 401, "token %r: /rpi_log answered %s" % (bad, r.status_code))
    buyer_app._active_order = None
    r = client.post("/register_external_order", json={"lat": 1, "lon": 2, "escrow_tx": "X", "buyer_pubkey": "Y"}, headers=bad)
    check(r.status_code == 401, "token %r: /register_external_order answered %s" % (bad, r.status_code))
    check(buyer_app._active_order is None, "token %r: fake external order was registered" % (bad,))

# 4. With the token, the car can still poll; human pages still need login.
buyer_app._active_order = _order("ESCROWTX888")
r = client.get("/active_order", headers=MACHINE)
check(r.status_code == 200, "active_order with token answered %s" % r.status_code)
r = client.get("/", headers=MACHINE)
check(r.status_code == 401, "machine token must not unlock the human login (got %s)" % r.status_code)

print()
if fails:
    print("FAILED CHECKS:")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("All checks passed.")
