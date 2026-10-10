#!/usr/bin/env python3
"""Offline test for car/activity.py: deliveries are grouped per order,
reported transfers are only stored after an (faked) on-chain lookup confirms
them, and the HTTP routes work on the seller app.

Run: python tests/test_activity.py
"""
import base64
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "car"))
for k, v in {"SELLER_USERNAME": "s", "SELLER_PASSWORD": "s"}.items():
    os.environ[k] = v

import activity  # noqa: E402

activity.LOG_FILE = Path(tempfile.mktemp(suffix=".json"))
fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


SIG = lambda c: c * 88  # noqa: E731  plausible base58 signature
A_SOL, B_SOL = "BRUeF8xjzM62Q18eSzt2HGiyLErsFnFRi5YaYpsStmpB", "7hUjeMv72iTArXg73QKwkzMJ61hYLTjGL7X9Ki4BCrk4"
A_EVM, B_EVM = "0x4d99BeAD5A4CCE20a7F93CB2CF62f1847263Ea8f", "0x91BF2b4694835652a1792fef35Ca505eb902A2Cc"
BUYER = "HKxfRrpATn6qVBmE6V2ypxBS31CprbhAg84JkzQVzmfX"

# 1. Grouping: old entries without links, new ones with links, a refund, a pending order.
hist = [
    {"type": "create_delivery", "sig": SIG("a"), "t": 1},
    {"type": "confirm_delivery", "sig": SIG("b"), "t": 2},                      # old: no escrow_tx
    {"type": "create_delivery", "sig": SIG("c"), "t": 3, "buyer": BUYER, "seller": A_SOL},
    {"type": "confirm_delivery", "sig": SIG("d"), "t": 4, "escrow_tx": SIG("c")},
    {"type": "create_delivery", "sig": SIG("e"), "t": 5, "buyer": BUYER, "seller": B_SOL},
    {"type": "cancel_delivery", "sig": SIG("f"), "t": 6, "escrow_tx": SIG("e"), "auto": True},
    {"type": "create_delivery", "sig": SIG("g"), "t": 7, "buyer": BUYER, "seller": B_SOL},
]
ds = activity.deliveries(hist, {"escrow_tx": SIG("g"), "status": "pending", "seller": B_SOL}, BUYER)
check([d["n"] for d in ds] == [4, 3, 2, 1], f"numbering newest first: {[d['n'] for d in ds]}")
check(ds[3]["status"] == "delivered" and ds[3]["release_tx"] == SIG("b") and ds[3]["buyer"] == BUYER,
      f"old unlinked confirm pairs with its order: {ds[3]}")
check(ds[2]["release_tx"] == SIG("d") and ds[2]["payee"] == A_SOL, f"linked confirm: {ds[2]}")
check(ds[1]["status"] == "refunded" and ds[1]["refund_tx"] == SIG("f"), f"refund: {ds[1]}")
check(ds[0]["status"] == "pending" and ds[0]["payee"] == B_SOL, f"pending: {ds[0]}")
ds = activity.deliveries(hist, {"escrow_tx": SIG("g"), "status": "delivered"}, BUYER)
check(ds[0]["status"] == "delivered", "active order status reaches the newest delivery")
ds = activity.deliveries(hist[:1] + hist[2:], None, BUYER)
check(ds[-1]["status"] == "closed", f"an earlier order without an outcome is closed: {ds[-1]}")

# 2. Verified recording (fake chain).
NFT_TX, PEAQ_TX = "0x" + "11" * 32, "0x" + "22" * 32


def fake_rpc(url, method, params):
    tx = params[0]
    if method == "eth_getTransactionReceipt":
        if tx == "0x" + "99" * 32:
            return {"status": "0x0", "logs": []}
        logs = []
        if tx == NFT_TX:
            logs = [{"address": activity.MACHINE_REGISTRY, "topics": [
                activity.TRANSFER_TOPIC, "0x" + "0" * 24 + A_EVM[2:].lower(), "0x" + "0" * 24 + B_EVM[2:].lower(),
                hex(5149)]}]
        return {"status": "0x1", "logs": logs}
    if method == "eth_getTransactionByHash":
        return {"from": A_EVM, "to": B_EVM, "value": hex(5 * 10 ** 17)}
    if method == "getTransaction":
        return {"meta": {"err": None}, "transaction": {"message": {"instructions": [
            {"program": "system", "parsed": {"type": "transfer", "info": {
                "source": A_SOL, "destination": B_SOL, "lamports": 50_000_000}}}]}}}
    raise AssertionError(method)


ev = activity.record("peaq", "nft", NFT_TX, rpc=fake_rpc)
check(ev["from"] == A_EVM.lower() and ev["to"] == B_EVM.lower() and ev["token_id"] == "5149", f"nft: {ev}")
ev = activity.record("peaq", "peaq", PEAQ_TX, rpc=fake_rpc)
check(ev["amount"] == 0.5 and ev["to"] == B_EVM.lower(), f"peaq: {ev}")
ev = activity.record("solana", "sol", SIG("h"), rpc=fake_rpc)
check(ev["amount"] == 0.05 and ev["to"] == B_SOL, f"sol: {ev}")
activity.record("peaq", "nft", NFT_TX, rpc=fake_rpc)  # same tx again
f = activity.feed(hist, None, BUYER, "op")
check(len(f["nft_transfers"]) == 1 and len(f["peaq_transfers"]) == 1 and len(f["sol_transfers"]) == 1,
      f"one entry per transaction: {f}")
for args in (("peaq", "nft", "0x" + "99" * 32), ("peaq", "nft", PEAQ_TX.replace("22", "33")),
             ("peaq", "peaq", "nope"), ("solana", "sol", "short"), ("btc", "x", SIG("i"))):
    try:
        activity.record(*args, rpc=fake_rpc)
        fails.append(f"must reject {args}")
    except activity.Rejected:
        pass

# 3. Owner addresses.
activity.set_owner("A", A_SOL, A_EVM)
activity.set_owner("B", B_SOL, B_EVM)
check(activity.feed([], None, BUYER, "op")["owners"]["B"]["evm"] == B_EVM, "owners stored")
for bad in (("C", A_SOL, A_EVM), ("A", "x", A_EVM), ("A", A_SOL, "0x12")):
    try:
        activity.set_owner(*bad)
        fails.append(f"must reject owner {bad}")
    except ValueError:
        pass

# 4. Routes on the seller app (behind its login).
import robopay_common as common  # noqa: E402
common.STATE_FILE = Path(tempfile.mktemp(suffix=".json"))
common.save_state(None, hist)
import seller_app  # noqa: E402
AUTH = {"Authorization": "Basic " + base64.b64encode(b"s:s").decode()}
c = seller_app.app.test_client()
check(c.get("/activity").status_code == 401, "feed needs login")
d = c.get("/activity", headers=AUTH).get_json()
check(len(d["deliveries"]) == 4 and d["owners"]["A"]["sol"] == A_SOL, f"feed over HTTP: {d.keys()}")
r = c.post("/activity", json={"chain": "peaq", "kind": "nft", "tx": "garbage"}, headers=AUTH)
check(r.status_code == 400, f"bad report rejected: {r.status_code}")
r = c.post("/activity/owner", json={"role": "B", "sol": B_SOL, "evm": B_EVM}, headers=AUTH)
check(r.status_code == 200, f"owner registration: {r.status_code}")

print()
if fails:
    print("FAILED CHECKS:")
    for f_ in fails:
        print("  -", f_)
    sys.exit(1)
print("All checks passed.")
