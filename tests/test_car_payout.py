#!/usr/bin/env python3
"""Offline test for car/car_main.py's payout path: the car checks the order's
escrow before driving, re-checks the owner before paying, and pays the
escrow's own seller - never a freshly looked-up wallet that the program would
reject anyway. No network: Solana, peaq and the buyer app are faked.

Run: python tests/test_car_payout.py
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "car"))
sys.path.insert(0, str(ROOT / "rpi"))
os.environ.setdefault("PAYOUT_MODE", "did")
os.environ.setdefault("MACHINE_ID", "42")

from solders.keypair import Keypair  # noqa: E402

import car_main  # noqa: E402
import payout_policy as pp  # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


A, B = Keypair().pubkey(), Keypair().pubkey()
logs, reported = [], []
car_main.send_log = logs.append
car_main.report_delivered = lambda lat, lon, sig: reported.append(sig)
owner = {"now": A}
car_main.resolve_owner = lambda: owner["now"]


def escrow(seller=A, status=pp.STATUS_PENDING, deadline=None):
    import time
    return {"buyer": str(Keypair().pubkey()), "seller": str(seller), "drone_operator": "x",
            "amount_lamports": 200_000_000, "target_lat_e7": 0, "target_lon_e7": 0,
            "deadline": deadline if deadline is not None else int(time.time()) + 600,
            "status": status, "bump": 255}


class FakeSolana:
    def __init__(self, esc):
        self.esc, self.paid, self.closed = esc, [], 0

    def read_escrow(self):
        return self.esc

    def confirm_delivery(self, lat, lon, seller=None):
        self.paid.append(str(seller))
        return "SIG"

    def close_escrow(self):
        self.closed += 1
        return "CLOSE"


# 1. Preflight accepts a pending escrow that pays the current owner.
sol = FakeSolana(escrow(seller=A))
try:
    check(car_main.preflight(sol)["seller"] == str(A), "preflight must return the escrow")
except pp.PayoutRefused as e:
    fails.append(f"preflight refused a valid order: {e}")

# 2. Preflight refuses an escrow that pays someone other than the owner.
try:
    car_main.preflight(FakeSolana(escrow(seller=B)))
    fails.append("preflight accepted an escrow paying a non-owner")
except pp.PayoutRefused:
    pass

# 3. Happy path: owner unchanged -> pays the escrow's seller and closes the escrow.
sol = FakeSolana(escrow(seller=A))
car_main._confirm(sol, False, 1.0, 2.0, "order-1", sol.esc)
check(sol.paid == [str(A)], f"must pay the escrow seller, paid {sol.paid}")
check(sol.closed == 1, "must close the escrow after paying")
check(reported == ["SIG"], "must report the delivery to the buyer app")

# 4. Ownership changed during the delivery -> nobody is paid, escrow left for refund.
logs.clear(); reported.clear()
sol = FakeSolana(escrow(seller=A))
owner["now"] = B
car_main._confirm(sol, False, 1.0, 2.0, "order-2", sol.esc)
check(sol.paid == [], f"owner changed: must not pay anyone, paid {sol.paid}")
check(sol.closed == 0, "owner changed: must not close the escrow (buyer needs it for the refund)")
check(reported == [], "owner changed: must not report a delivery")
check(any("Besitzer" in m for m in logs), f"owner changed: the buyer UI log must say why, got {logs}")

# 5. Owner lookup fails at payout time -> refuse, no payment.
def boom():
    raise pp.PayoutRefused("rpc down")
car_main.resolve_owner = boom
sol = FakeSolana(escrow(seller=A))
car_main._confirm(sol, False, 1.0, 2.0, "order-3", sol.esc)
check(sol.paid == [], "lookup failure: must not pay")

# 6. Startup: DID mode without MACHINE_ID must refuse to start; static must be explicit.
for mode, mid, ok in (("did", None, False), ("did", 42, True), ("static", None, True), ("nonsense", 42, False)):
    car_main.cfg.PAYOUT_MODE, car_main.cfg.MACHINE_ID = mode, mid
    try:
        car_main.check_payout_config()
        check(ok, f"check_payout_config accepted mode={mode} machine_id={mid}")
    except SystemExit:
        check(not ok, f"check_payout_config refused mode={mode} machine_id={mid}")

print()
if fails:
    print("FAILED CHECKS:")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("All checks passed.")
