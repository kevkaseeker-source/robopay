#!/usr/bin/env python3
"""Offline self-test for agent/kitchen_agent.py.

Same philosophy as agent/selftest.py (which does this for delivery_agent.py):
stub Solana and the buyer_app.py HTTP endpoints, so the decision logic, the
price-cap guardrail, the "don't order into a pending escrow" preflight and
the trace can all be checked without a chain, without devnet SOL and without
buyer_app.py running.

    python3 agent/kitchen_agent_selftest.py

Exits non-zero if any check fails. rpi/config.py itself is imported for
real (it is plain os.getenv() defaults, nothing that touches a network or a
missing package), so only solders.pubkey and solana_client are stubbed.
"""
import json, sys, tempfile, types
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent
RUNS = Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="kitchen_selftest_"))
fails = []

# ------------------------------------------------------------------ Pubkey stub
# Identical approach to agent/selftest.py's Pubkey stub: a real base58 en/decode
# so pubkey strings round-trip and compare equal, without needing the solders
# package installed.
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
def b58enc(b):
    n = int.from_bytes(b, "big"); out = ""
    while n:
        n, r = divmod(n, 58); out = B58[r] + out
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + out
def b58dec(s):
    n = 0
    for c in s: n = n * 58 + B58.index(c)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return b"\0" * (len(s) - len(s.lstrip("1"))) + raw

class Pubkey:
    def __init__(self, raw): self._raw = bytes(raw)
    def __bytes__(self): return self._raw
    def __str__(self): return b58enc(self._raw)
    def __eq__(self, o): return bytes(o) == self._raw
    @staticmethod
    def from_string(s): return Pubkey(b58dec(s).rjust(32, b"\0"))
    @staticmethod
    def find_program_address(seeds, prog): return (Pubkey(b"E" * 32), 255)

SELLER = "7uoFeSG546UvK5HYyA97GVmJUTvrXWgGgkTgxspH4d1C"
KITCHEN_WALLET = "HKSt5XDrvupqbkj8JG8wyX4jJnf3aXdbjGp6zXUckEXD"
FIXED_OP = "7VizNvqBSnHnP8ySnsjxxyUnBQCybVnJHBDRyvaThXia"
AGENT_OP = "FCSTjYn6tKKVFCdaaA7khkiQGrQhAF7n2staJQ8bWnhA"

CALLS = []

class KeypairStub:
    def __init__(self, pk): self._pk = pk
    def pubkey(self): return self._pk

class SolanaClientStub:
    behaviour = "ok"  # ok | send_failed
    loaded_pubkey = KITCHEN_WALLET

    def __init__(self, rpc, keypair_path, program_id):
        self.keypair_path = keypair_path
        self._keypair = KeypairStub(Pubkey.from_string(SolanaClientStub.loaded_pubkey))
        CALLS.append(("init", keypair_path))

    def create_delivery(self, operator, lat, lon, amount_sol, deadline_minutes, seller=None):
        CALLS.append(("create_delivery", str(operator), lat, lon, amount_sol, deadline_minutes))
        if SolanaClientStub.behaviour == "send_failed":
            raise RuntimeError("create_delivery send failed: boom")
        return "SIGcreate111"

    def cancel_delivery(self, operator):
        CALLS.append(("cancel_delivery", str(operator)))
        return "SIGcancel111"


# ------------------------------------------------------------------ Anthropic stub
# Kitchen_agent.py makes at most ONE messages.create call per run (a single
# photo, a single required verdict) - unlike delivery_agent.py's multi-turn
# loop, so this stub just returns one fixed response, not a scripted sequence.
class Blk:
    def __init__(self, **kw): self.__dict__.update(kw)

def tool(name, tid, inp=None):
    return Blk(type="tool_use", name=name, id=tid, input=inp or {})

def text(t):
    return Blk(type="text", text=t)

class FakeResponse:
    def __init__(self, content): self.content = content

class FakeMessages:
    def create(self, **kw):
        assert kw["model"] and kw["tools"] and kw["system"]
        return FakeResponse(MODEL["content"])

class FakeAnthropic:
    def __init__(self, *a, **kw):
        assert "timeout" in kw and "max_retries" in kw, "API timeout/retries not set"
        self.messages = FakeMessages()

MODEL = {"content": [tool("report_milk_level", "t1",
                          {"visible": True, "almost_empty": True, "reason": "carton looks nearly empty"})]}


def install_stubs():
    pk = types.ModuleType("solders.pubkey"); pk.Pubkey = Pubkey
    solders = types.ModuleType("solders")
    sys.modules["solders"] = solders
    sys.modules["solders.pubkey"] = pk
    sc = types.ModuleType("solana_client"); sc.SolanaClient = SolanaClientStub
    sys.modules["solana_client"] = sc
    m = types.ModuleType("anthropic"); m.Anthropic = FakeAnthropic
    sys.modules["anthropic"] = m


# ------------------------------------------------------------------ HTTP stub
HTTP_LOG = []
JPEG = b"\xff\xd8\xff\xe0FAKE-JPEG-NOT-REAL-BYTES"

class FakeResp:
    def __init__(self, code=200, payload=None, text="", content=b""):
        self.status_code = code; self._payload = payload; self.content = content
        self.text = text or json.dumps(payload or {})
    def json(self): return self._payload

ACTIVE_ORDER = {"present": False}
REGISTER_BEHAVIOUR = {"mode": "ok"}  # ok | http_error | rejects
CAMERA = {"mode": "ok"}  # ok | unreachable | http_error

def fake_get(url, timeout=None, **kw):
    HTTP_LOG.append(("GET", url))
    if url.endswith("/active_order"):
        if ACTIVE_ORDER["present"]:
            return FakeResp(200, {"trigger_mode": "fixed", "escrow_tx": "TXold"})
        return FakeResp(204, None)
    if url.endswith("/debug/frame"):
        if CAMERA["mode"] == "unreachable":
            raise RuntimeError("connection refused")
        if CAMERA["mode"] == "http_error":
            return FakeResp(503, None, content=b"")
        return FakeResp(200, None, content=JPEG)
    raise AssertionError("unexpected GET " + url)

def fake_post(url, json=None, timeout=None, **kw):
    HTTP_LOG.append(("POST", url, json))
    if url.endswith("/register_external_order"):
        mode = REGISTER_BEHAVIOUR["mode"]
        if mode == "http_error":
            return FakeResp(500, {"success": False}, text="internal error")
        if mode == "rejects":
            return FakeResp(200, {"success": False, "error": "Es laeuft bereits eine Bestellung"})
        return FakeResp(200, {"success": True})
    raise AssertionError("unexpected POST " + url)


# ------------------------------------------------------------------ Harness
def run_case(name, argv=("--dry-run",), goal="cake", active_order=False, behaviour="ok",
             register="ok", keypath=None, max_price=None, env=None, expect_rc=0,
             expect_abort=None, full_pantry=False, camera="ok", model_verdict=None):
    ACTIVE_ORDER["present"] = active_order
    REGISTER_BEHAVIOUR["mode"] = register
    SolanaClientStub.behaviour = behaviour
    CAMERA["mode"] = camera
    MODEL["content"] = [model_verdict] if model_verdict is not None else \
        [tool("report_milk_level", "t1",
              {"visible": True, "almost_empty": True, "reason": "carton looks nearly empty"})]
    del HTTP_LOG[:]; del CALLS[:]

    for mod in [m for m in list(sys.modules) if m in ("kitchen_agent", "config", "solana_client",
                                                        "solders", "solders.pubkey", "anthropic")]:
        del sys.modules[mod]
    install_stubs()
    sys.path.insert(0, str(AGENT_DIR))
    sys.path.insert(0, str(AGENT_DIR.parent / "rpi"))
    import requests as rq
    rq.get, rq.post = fake_get, fake_post

    import os
    keyfile = keypath
    if keyfile is None:
        keyfile = str(RUNS / "kitchen_key.json")
        Path(keyfile).write_text("[1,2,3]")
    for k in ("KITCHEN_AGENT_WALLET_KEYPAIR_PATH", "PC_SERVER_URL", "KITCHEN_TRIGGER_MODE",
              "KITCHEN_AGENT_MAX_PRICE_SOL", "SELLER_PUBKEY", "OPERATOR_PUBKEY",
              "AGENT_OPERATOR_PUBKEY", "KITCHEN_USE_CAMERA", "KITCHEN_CAMERA_URL",
              "ANTHROPIC_API_KEY"):
        os.environ.pop(k, None)
    os.environ["KITCHEN_AGENT_WALLET_KEYPAIR_PATH"] = keyfile
    os.environ["PC_SERVER_URL"] = "http://buyer.test"
    os.environ["SELLER_PUBKEY"] = SELLER
    # Off by default so the ordering/payment-logic cases above don't need to
    # also think about the camera - tests that exercise the milk check turn
    # it back on explicitly via env={"KITCHEN_USE_CAMERA": "true", ...}.
    os.environ["KITCHEN_USE_CAMERA"] = "false"
    os.environ["KITCHEN_CAMERA_URL"] = "http://camera.test"
    os.environ["ANTHROPIC_API_KEY"] = "test"
    if max_price is not None:
        os.environ["KITCHEN_AGENT_MAX_PRICE_SOL"] = str(max_price)
    for k, v in (env or {}).items():
        os.environ[k] = v

    import importlib
    ka = importlib.import_module("kitchen_agent")
    ka.DECISIONS_DIR = RUNS / name
    if full_pantry:
        ka.PANTRY = {item: True for item in ka.PANTRY}
    sys.argv = ["kitchen_agent.py", "--goal", goal] + list(argv)
    try:
        rc = ka.main()
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else str(e.code)

    runs = sorted((RUNS / name).glob("*/trace.json"))
    trace = json.loads(runs[-1].read_text()) if runs else None
    if expect_abort is not None:
        ok = isinstance(rc, str) and expect_abort in rc
        detail = "abort contains %r" % expect_abort
    else:
        ok = rc == expect_rc
        detail = "rc=%s (expected %s)" % (rc, expect_rc)
    print("[%s] %-28s %s" % ("PASS" if ok else "FAIL", name, detail))
    if not ok:
        fails.append("%s: unexpected %r" % (name, rc))
    return rc, trace


def check(cond, msg):
    if not cond: fails.append(msg)


# 1. Happy path, dry run: decides (camera off -> hardcoded pantry governs),
# checks for a pending order (a harmless GET), but never signs or tells
# buyer_app.py about an order that was never actually funded.
rc, tr = run_case("happy_dry_run")
check(tr and tr["decision"]["item"] == "milk", "happy_dry_run: wrong item decided")
check(tr and tr["outcome"] == "dry_run", "happy_dry_run: wrong outcome")
check(tr and tr["milk_check"]["source"] == "camera_disabled",
      "happy_dry_run: should record the camera as disabled, not consulted")
check(not any(c[0] == "create_delivery" for c in CALLS), "happy_dry_run: signed a transaction anyway")
check(not any(h[0] == "POST" for h in HTTP_LOG),
      "happy_dry_run: registered an order with buyer_app.py despite --dry-run")

# 2. Nothing missing (pantry fully stocked): valid outcome, exit code 2. The
# pending-order check still runs first (cheap, always done), but nothing
# past that - no camera, no wallet, no registration.
rc, tr = run_case("nothing_missing", full_pantry=True, expect_rc=2)
check(tr and tr["decision"] is None, "nothing_missing: should not have decided to order anything")
check(not CALLS, "nothing_missing: touched the wallet despite nothing being needed")
check(not any(h[0] == "POST" for h in HTTP_LOG),
      "nothing_missing: talked to buyer_app.py's ordering endpoint despite nothing being needed")

# 3. Real run (not dry-run): signs, then registers with buyer_app.py
rc, tr = run_case("real_run", argv=())
check(tr and tr["outcome"] == "ordered", "real_run: should have ordered")
check(any(c[0] == "create_delivery" for c in CALLS), "real_run: never called create_delivery")
check(any(h[0] == "POST" and h[1].endswith("/register_external_order") for h in HTTP_LOG),
      "real_run: never registered the order with buyer_app.py")
reg_body = next(h[2] for h in HTTP_LOG if h[0] == "POST")
check(reg_body["escrow_tx"] == "SIGcreate111", "real_run: wrong escrow_tx registered")
check(reg_body["buyer_mode"] == "kitchen_agent", "real_run: buyer_mode not tagged")

# 4. Price cap: recipe's next item costs more than the cap allows
rc, tr = run_case("price_cap", max_price=0.001, expect_abort="KITCHEN_AGENT_MAX_PRICE_SOL")
check(not any(c[0] == "create_delivery" for c in CALLS), "price_cap: signed despite exceeding the cap")

# 5. An order is already pending - refuse before touching the wallet at all
rc, tr = run_case("active_order_pending", active_order=True, expect_abort="already pending")
check(not any(c[0] == "init" for c in CALLS), "active_order_pending: loaded a wallet anyway")

# 6. No keypair path configured
rc, tr = run_case("no_keypath", keypath="", expect_abort="KITCHEN_AGENT_WALLET_KEYPAIR_PATH")

# 7. Keypair file does not exist
rc, tr = run_case("keyfile_missing", keypath="/nonexistent/kitchen-key.json", expect_abort="not found")

# 8. Unknown goal
rc, tr = run_case("unknown_goal", goal="souffle", expect_abort="Unknown goal")

# 9. On-chain send fails: exit 4, trace still written
rc, tr = run_case("send_failed", argv=(), behaviour="send_failed", expect_rc=4)
check(tr and tr["outcome"] == "error", "send_failed: wrong outcome recorded")

# 10. Registration fails after a successful on-chain send: exit 3, escrow_tx preserved
rc, tr = run_case("register_failed", argv=(), register="http_error", expect_rc=3)
check(tr and tr["outcome"] == "registration_failed", "register_failed: wrong outcome recorded")
check(tr and tr.get("escrow_tx") == "SIGcreate111",
      "register_failed: escrow_tx lost from the trace even though the money is already on-chain")

# 11. trigger_mode=agent selects the agent operator, not the fixed-QR one
rc, tr = run_case("agent_trigger_mode", argv=(), env={"KITCHEN_TRIGGER_MODE": "agent"})
create_call = next(c for c in CALLS if c[0] == "create_delivery")
check(create_call[1] == str(Pubkey.from_string(AGENT_OP)),
      "agent_trigger_mode: wrong operator pubkey used")

CAMERA_ON = {"KITCHEN_USE_CAMERA": "true"}

# 12. Camera says the milk is almost empty: orders it for real, photo saved,
# verdict recorded in the trace.
rc, tr = run_case("camera_almost_empty", argv=(), env=CAMERA_ON,
                  model_verdict=tool("report_milk_level", "t1",
                                     {"visible": True, "almost_empty": True,
                                      "reason": "carton is translucent, barely a film of milk left"}))
check(tr and tr["milk_check"]["source"] == "camera", "camera_almost_empty: wrong milk_check source")
check(tr and tr["milk_check"]["almost_empty"] is True, "camera_almost_empty: verdict not recorded")
check(tr and tr["decision"] and tr["decision"]["item"] == "milk",
      "camera_almost_empty: should have decided to order milk")
check(tr and tr["outcome"] == "ordered", "camera_almost_empty: should have ordered")
check(any(h[0] == "GET" and h[1].endswith("/debug/frame") for h in HTTP_LOG),
      "camera_almost_empty: never fetched a camera frame")

# 13. Camera says the milk is fine: does NOT order, even though the
# hardcoded pantry default (camera off) would have.
rc, tr = run_case("camera_fine", env=CAMERA_ON, expect_rc=2,
                  model_verdict=tool("report_milk_level", "t1",
                                     {"visible": True, "almost_empty": False,
                                      "reason": "still about half full"}))
check(tr and tr["milk_check"]["almost_empty"] is False, "camera_fine: wrong verdict recorded")
check(tr and tr["decision"] is None, "camera_fine: should not have ordered milk")

# 14. Camera unreachable: fails closed (treated as "fine"), does not order,
# and never even tries to call the model.
rc, tr = run_case("camera_unreachable", env=CAMERA_ON, camera="unreachable", expect_rc=2)
check(tr and tr["milk_check"]["source"] == "photo_unavailable",
      "camera_unreachable: wrong milk_check source")
check(tr and tr["decision"] is None, "camera_unreachable: should not have guessed and ordered")

# 15. Model looks at the photo but never calls the verdict tool: fails
# closed the same way as an unreachable camera.
rc, tr = run_case("camera_model_silent", env=CAMERA_ON, expect_rc=2,
                  model_verdict=text("I can see something but I'm not sure what it is."))
check(tr and tr["milk_check"]["source"] == "model_undetermined",
      "camera_model_silent: wrong milk_check source")
check(tr and tr["decision"] is None, "camera_model_silent: should not have guessed and ordered")

# 16. Model says no milk container is visible at all in the photo: fails
# closed rather than assuming it's missing just because it's not in frame.
rc, tr = run_case("camera_not_visible", env=CAMERA_ON, expect_rc=2,
                  model_verdict=tool("report_milk_level", "t1",
                                     {"visible": False, "almost_empty": False,
                                      "reason": "photo shows an empty counter, no container in frame"}))
check(tr and tr["milk_check"]["source"] == "not_visible", "camera_not_visible: wrong milk_check source")
check(tr and tr["decision"] is None, "camera_not_visible: should not have guessed and ordered")

# 17. --photo: a local image file replaces the live camera entirely - no
# /debug/frame request should happen at all.
PHOTO_FILE = RUNS / "milk.jpg"
PHOTO_FILE.write_bytes(JPEG)
rc, tr = run_case("photo_file", argv=("--photo", str(PHOTO_FILE)), env=CAMERA_ON,
                  model_verdict=tool("report_milk_level", "t1",
                                     {"visible": True, "almost_empty": True, "reason": "empty carton"}))
check(tr and tr["milk_check"]["source"] == "camera", "photo_file: wrong milk_check source")
check(not any(h[0] == "GET" and h[1].endswith("/debug/frame") for h in HTTP_LOG),
      "photo_file: fetched a live camera frame despite --photo")

# 18. --photo pointing at a file that doesn't exist: fails closed, same as
# an unreachable camera.
rc, tr = run_case("photo_missing", argv=("--photo", "/nonexistent/milk.jpg"), env=CAMERA_ON,
                  expect_rc=2)
check(tr and tr["milk_check"]["source"] == "photo_unavailable", "photo_missing: wrong milk_check source")

# 19. --check-milk-only: just the verdict, nothing else - no wallet loaded,
# no buyer app contacted at all (not even the pending-order check).
rc, tr = run_case("check_milk_only", argv=("--check-milk-only", "--photo", str(PHOTO_FILE)),
                  env=CAMERA_ON,
                  model_verdict=tool("report_milk_level", "t1",
                                     {"visible": True, "almost_empty": True, "reason": "empty carton"}))
check(tr and tr["outcome"] == "milk_check_only", "check_milk_only: wrong outcome")
check(tr and tr["milk_check"]["almost_empty"] is True, "check_milk_only: verdict not recorded")
check(not CALLS, "check_milk_only: touched the wallet despite --check-milk-only")
check(not HTTP_LOG, "check_milk_only: talked to buyer_app.py despite --check-milk-only")

print()
if fails:
    print("FAILED CHECKS:")
    for f in fails: print("  -", f)
    sys.exit(1)
print("All checks passed.")
