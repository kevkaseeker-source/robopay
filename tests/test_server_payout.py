#!/usr/bin/env python3
"""Offline test for the server side of "the payout follows the Machine-NFT":

- buyer_app fixes the escrow's seller to the owner resolved from the peaq DID
  when the order is placed, refuses orders when that lookup fails (never a
  silent fallback) and while the car is paused for an ownership handoff,
  and stores the seller + deadline so the UI can offer a refund.
- seller_app shows the resolved owner, exposes a public /machine_status the
  CarOwnerApp uses to lock the NFT transfer during a delivery, and lets the
  logged-in owner pause/resume the car.

No network: the owner lookup and the Solana RPC are faked.

Run: python tests/test_server_payout.py
"""
import base64
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "car"))
for k, v in {"BUYER_USERNAME": "b", "BUYER_PASSWORD": "b", "SELLER_USERNAME": "s", "SELLER_PASSWORD": "s",
             "MACHINE_TOKEN": "tok", "PAYOUT_MODE": "did"}.items():
    os.environ[k] = v

from solders.keypair import Keypair  # noqa: E402

kp = Keypair()
keyfile = Path(tempfile.mktemp(suffix=".json"))
keyfile.write_text(json.dumps(list(bytes(kp))))
os.environ["BUYER_KEYPAIR_PATH"] = str(keyfile)

import robopay_common as common  # noqa: E402
import payout_policy as pp  # noqa: E402

common.STATE_FILE = Path(tempfile.mktemp(suffix=".json"))
common.MACHINE_STATE_FILE = Path(tempfile.mktemp(suffix=".json"))
common.get_balance_sol = lambda pk: 1.0
common.get_balance_peaq = lambda addr: 2.0

OWNER_A, OWNER_B = Keypair().pubkey(), Keypair().pubkey()
lookup = {"result": (OWNER_A, "0xAAAA")}


def fake_details():
    if isinstance(lookup["result"], Exception):
        raise lookup["result"]
    return lookup["result"]


common._resolve_owner_details = fake_details

import buyer_app  # noqa: E402

sent = []


class FakeRpc:
    class _V:
        def __init__(self, v):
            self.value = v

    def get_latest_blockhash(self):
        from solders.hash import Hash
        return self._V(type("B", (), {"blockhash": Hash.default()})())

    def send_transaction(self, tx):
        sent.append(tx)
        return self._V("SIG%d" % len(sent))


common.rpc = FakeRpc()

import sensor_logger  # noqa: E402
sensor_logger.start = lambda: None
import seller_app  # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


def auth(u, p):
    return {"Authorization": "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()}


B, S = buyer_app.app.test_client(), seller_app.app.test_client()
BA, SA = auth("b", "b"), auth("s", "s")


def reset():
    buyer_app._active_order = None
    common.save_state(None, [])  # seller_app reads the order from this file
    sent.clear()
    common.set_paused(False)
    common._owner_cache.update(t=0.0, value=None)


# 1. Order: escrow seller = DID owner, stored with the deadline.
reset()
r = B.post("/order", json={"trigger_mode": "fixed"}, headers=BA)
check(r.status_code == 200, f"order failed: {r.status_code} {r.get_data(as_text=True)}")
check(len(sent) == 1, "order must send exactly one transaction")
if sent:
    keys = [str(k) for k in sent[0].message.account_keys]
    check(str(OWNER_A) in keys, "the escrow's seller account must be the DID owner")
o = buyer_app._active_order or {}
check(o.get("seller") == str(OWNER_A), "active order must remember the seller")
check(isinstance(o.get("deadline"), int) and o["deadline"] > time.time(), "active order must store the deadline")

# 2. Lookup fails -> 503, no transaction, no order (never a fallback).
reset()
lookup["result"] = pp.PayoutRefused("rpc down")
r = B.post("/order", json={}, headers=BA)
check(r.status_code == 503, f"lookup failure must be 503, got {r.status_code}")
check(sent == [] and buyer_app._active_order is None, "lookup failure must not create an escrow")
lookup["result"] = (OWNER_A, "0xAAAA")

# 3. Paused for a handoff -> 409, nothing sent; /status reports it.
reset()
common.set_paused(True, "ownership handoff")
r = B.post("/order", json={}, headers=BA)
check(r.status_code == 409 and sent == [], f"paused car must refuse orders, got {r.status_code}")
check(B.get("/status", headers=BA).get_json().get("paused") is True, "/status must report paused")
common.set_paused(False)

# 4. seller_app: owner wallet shows the DID owner and their peaq address.
reset()
d = S.get("/wallet", headers=SA).get_json()
check(d.get("pubkey") == str(OWNER_A) and d.get("evm") == "0xAAAA", f"/wallet must show the DID owner, got {d}")
lookup["result"] = pp.PayoutRefused("rpc down")
common._owner_cache.update(t=0.0, value=None)
d = S.get("/wallet", headers=SA).get_json()
check(d.get("pubkey") is None and "rpc down" in (d.get("error") or ""), f"/wallet must show the lookup error, got {d}")
lookup["result"] = (OWNER_A, "0xAAAA")

# 5. Public /machine_status (no login): busy while an order is pending.
reset()
d = S.get("/machine_status").get_json()
check(d.get("busy") is False and d.get("paused") is False, f"idle status wrong: {d}")
B.post("/order", json={}, headers=BA)
d = S.get("/machine_status").get_json()
check(d.get("busy") is True, f"must be busy while an order is pending: {d}")

# 6. Pause/resume need the owner login.
reset()
check(S.post("/machine/pause").status_code == 401, "pause without login must be refused")
check(S.post("/machine/pause", json={"reason": "handoff"}, headers=SA).status_code == 200, "pause with login")
check(common.is_paused() is True, "pause must persist")
check(S.post("/machine/resume", headers=SA).status_code == 200 and common.is_paused() is False, "resume")

# 7. Automatic handoff lock: when the owner (or their payout wallet) changes,
#    orders stop until the NEW owner signs in once with the Solana wallet now
#    in the DID. This closes the window where the old owner could have planted
#    their own key in the DID before the new owner took control.
reset()
common.MACHINE_STATE_FILE.unlink(missing_ok=True)
check(B.post("/order", json={}, headers=BA).status_code == 200, "first owner is acknowledged automatically")
reset()
lookup["result"] = (OWNER_B, "0xBBBB")           # NFT handed to B, DID now names B's wallet
r = B.post("/order", json={}, headers=BA)
check(r.status_code == 409 and sent == [], f"owner changed: orders must be locked, got {r.status_code}")
check(S.get("/machine_status").get_json().get("handoff_pending") is True, "status must show the pending handoff")
check(common.acknowledge_owner(str(OWNER_A)) is False, "the OLD owner's key must not unlock the handoff")
seller_app.mobile_ownership.verify_ownership = lambda *a, **k: None   # signature check is tested elsewhere
r = S.post("/mobile/verify", json={"pubkey": str(OWNER_B), "message": "m", "signature": "s"})
check(r.status_code == 200, f"new owner's sign-in must succeed, got {r.status_code}")
reset()
r = B.post("/order", json={}, headers=BA)
check(r.status_code == 200 and buyer_app._active_order.get("seller") == str(OWNER_B),
      f"after the new owner signed in, orders pay the new owner, got {r.status_code}")
check(S.get("/machine_status").get_json().get("handoff_pending") is False, "handoff no longer pending")
lookup["result"] = (OWNER_A, "0xAAAA")
common.MACHINE_STATE_FILE.unlink(missing_ok=True)

# 8. Automatic refund once the deadline has passed without a delivery.
reset()
B.post("/order", json={}, headers=BA)
deadline = buyer_app._active_order["deadline"]
sent.clear()
check(buyer_app.auto_refund(now=deadline - 1) is None and sent == [], "no refund before the deadline")
sig = buyer_app.auto_refund(now=deadline + 10)
check(sig is not None and len(sent) == 1, "refund must be sent after the deadline")
o = buyer_app._active_order or {}
check(o.get("status") == "refunded" and o.get("refund_tx") == sig, f"order must show the refund, got {o}")
check(any(t["type"] == "cancel_delivery" for t in buyer_app._tx_history), "refund must appear in the history")
sent.clear()
check(buyer_app.auto_refund(now=deadline + 20) is None and sent == [], "no second refund")
st = B.get("/status", headers=BA).get_json()
check(st.get("status") == "refunded", "status endpoint must report the refund to the UI")
check(B.post("/order", json={}, headers=BA).status_code == 200, "a refunded order must not block new orders")

print()
if fails:
    print("FAILED CHECKS:")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("All checks passed.")
