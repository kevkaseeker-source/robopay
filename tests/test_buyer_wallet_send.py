#!/usr/bin/env python3
"""Offline test for the buyer app's own wallet: send SOL / PEAQ / a Machine-NFT
from the server-held buyer wallet (POST /wallet/send), and show both
identities (GET /wallet). Chains are faked via car/wallet_ops.py.

Run: python tests/test_buyer_wallet_send.py
"""
import base64
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "car"))
for k, v in {"BUYER_USERNAME": "b", "BUYER_PASSWORD": "b", "MACHINE_TOKEN": "tok"}.items():
    os.environ[k] = v

from solders.keypair import Keypair  # noqa: E402

kp = Keypair()
keyfile = Path(tempfile.mktemp(suffix=".json"))
keyfile.write_text(json.dumps(list(bytes(kp))))
os.environ["BUYER_KEYPAIR_PATH"] = str(keyfile)
peaq_keyfile = Path(tempfile.mktemp(suffix=".key"))
peaq_keyfile.write_text("0x" + "11" * 32)

import robopay_common as common  # noqa: E402
common.STATE_FILE = Path(tempfile.mktemp(suffix=".json"))
common.MACHINE_STATE_FILE = Path(tempfile.mktemp(suffix=".json"))
common.get_balance_sol = lambda pk: 1.5

import wallet_ops  # noqa: E402
import activity  # noqa: E402
activity.LOG_FILE = Path(tempfile.mktemp(suffix=".json"))

calls = []
wallet_ops.send_sol = lambda rpc, keypair, to, amount: calls.append(("sol", str(keypair.pubkey()), to, amount)) or "SOLSIG"


class FakePeaq:
    def __init__(self, key, rpc_url=None, chain_id=None):
        self.address = "0xBuyerPeaq"

    def balance(self):
        return 3.25

    def send_peaq(self, to, amount):
        calls.append(("peaq", to, amount))
        return "0xpeaqtx"

    def transfer_nft(self, machine_id, to):
        calls.append(("nft", machine_id, to))
        return "0xnfttx"


wallet_ops.PeaqWallet = FakePeaq

import buyer_app  # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


AUTH = {"Authorization": "Basic " + base64.b64encode(b"b:b").decode()}
c = buyer_app.app.test_client()
SOL_TO = str(Keypair().pubkey())

# 1. Without a peaq wallet configured: SOL works, PEAQ/NFT say so clearly.
os.environ.pop("BUYER_PEAQ_KEY_PATH", None)
d = c.get("/wallet", headers=AUTH).get_json()
check(d.get("pubkey") == str(kp.pubkey()) and d.get("peaq") is None, f"/wallet without peaq: {d}")
r = c.post("/wallet/send", json={"asset": "sol", "to": SOL_TO, "amount": "0.1"}, headers=AUTH)
check(r.status_code == 200 and calls[-1] == ("sol", str(kp.pubkey()), SOL_TO, 0.1), f"send SOL: {r.get_json()} {calls}")
r = c.post("/wallet/send", json={"asset": "peaq", "to": "0x" + "ab" * 20, "amount": "1"}, headers=AUTH)
check(r.status_code == 503 and "peaq" in r.get_json().get("error", "").lower(), "PEAQ without wallet must be 503")

# 2. With a peaq wallet: /wallet shows it, PEAQ and NFT sends go through.
os.environ["BUYER_PEAQ_KEY_PATH"] = str(peaq_keyfile)
d = c.get("/wallet", headers=AUTH).get_json()
check(d.get("peaq", {}).get("address") == "0xBuyerPeaq" and d["peaq"].get("balance") == 3.25, f"/wallet with peaq: {d}")
r = c.post("/wallet/send", json={"asset": "peaq", "to": "0x" + "ab" * 20, "amount": "0.5"}, headers=AUTH)
check(r.status_code == 200 and calls[-1] == ("peaq", "0x" + "ab" * 20, 0.5), f"send PEAQ: {r.get_json()}")
r = c.post("/wallet/send", json={"asset": "nft", "to": "0x" + "cd" * 20, "machine_id": "77"}, headers=AUTH)
check(r.status_code == 200 and calls[-1] == ("nft", 77, "0x" + "cd" * 20), f"send NFT: {r.get_json()}")

# 3. The car's own NFT can't move while a delivery is running (Kevin's rule).
buyer_app._active_order = {"status": "pending", "deadline": 9_999_999_999}
n = len(calls)
r = c.post("/wallet/send", json={"asset": "nft", "to": "0x" + "cd" * 20, "machine_id": common.PEAQ_MACHINE_ID},
           headers=AUTH)
check(r.status_code == 409 and len(calls) == n, f"car NFT during delivery must be refused: {r.status_code}")
buyer_app._active_order = None

# 4. Bad input is rejected before anything is signed.
n = len(calls)
for body in ({"asset": "sol", "to": "nope", "amount": "1"}, {"asset": "sol", "to": SOL_TO, "amount": "-1"},
             {"asset": "peaq", "to": "0x" + "ab" * 20, "amount": "abc"}, {"asset": "gold", "to": SOL_TO, "amount": "1"}):
    r = c.post("/wallet/send", json=body, headers=AUTH)
    check(r.status_code == 400, f"bad input {body} must be 400, got {r.status_code}")
check(len(calls) == n, "bad input must not reach the chain")
check(c.post("/wallet/send", json={"asset": "sol", "to": SOL_TO, "amount": "1"}).status_code == 401, "needs login")

# 5. The buyer's own sends appear in the shared activity feed.
f = activity.feed([], None, "b", "op")
check(any(e["kind"] == "sol" and e["tx"] == "SOLSIG" for e in f["sol_transfers"]), f"SOL send in feed: {f['sol_transfers']}")
check(any(e["kind"] == "nft" and e["tx"] == "0xnfttx" for e in f["nft_transfers"]), "NFT send in feed")

print()
if fails:
    print("FAILED CHECKS:")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("All checks passed.")
