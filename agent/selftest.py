#!/usr/bin/env python3
"""Offline self-test for agent/delivery_agent.py.

Stubs the Anthropic SDK, Solana and the HTTP endpoints, so the tool loop, the
guardrails around the sign tool, the chain preflight, the cleanup and the trace
can be checked without hardware, without a chain and without an API key.

    python3 agent/selftest.py

Exits non-zero if any check fails. Each case scripts the model's responses, so
"the model does X" below means "a response containing X was injected".
"""
import base64, json, os, struct, sys, tempfile, time, types
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent
RUNS = Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="agent_selftest_"))
fails = []

# ------------------------------------------------------------------ Pubkey stub
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

SELLER   = "7uoFeSG546UvK5HYyA97GVmJUTvrXWgGgkTgxspH4d1C"
BUYER    = "HKSt5XDrvupqbkj8JG8wyX4jJnf3aXdbjGp6zXUckEXD"
AGENT_OP = "FCSTjYn6tKKVFCdaaA7khkiQGrQhAF7n2staJQ8bWnhA"
FIXED_OP = "7VizNvqBSnHnP8ySnsjxxyUnBQCybVnJHBDRyvaThXia"

LAT_E7, LON_E7 = int(52.3609 * 1e7), int(14.06 * 1e7)

def escrow_bytes(status=0, deadline_offset=3600, seller=SELLER, amount=200_000_000,
                 lat_e7=LAT_E7):
    return (b"D" * 8
            + b58dec(BUYER).rjust(32, b"\0")
            + b58dec(seller).rjust(32, b"\0")
            + b58dec(AGENT_OP).rjust(32, b"\0")
            + struct.pack("<Qqqq", amount, lat_e7, LON_E7, int(time.time()) + deadline_offset)
            + bytes([status, 255]))

# ------------------------------------------------------------------ Module stubs
# "reads" is a list of escrow states; every get_account_info takes the next one
# and the last one repeats. That is how "the escrow changed between preflight and
# signing" is reproduced.
CHAIN = {"reads": [escrow_bytes()], "n": 0}
def next_escrow():
    r = CHAIN["reads"]; i = min(CHAIN["n"], len(r) - 1); CHAIN["n"] += 1
    return r[i]

class _Val:
    def __init__(self, data): self.data = data
class _Resp:
    def __init__(self, value): self.value = value
class RpcClient:
    def __init__(self, url): pass
    def get_account_info(self, pda):
        d = next_escrow()
        return _Resp(_Val(d) if d is not None else None)

class KeypairStub:
    def __init__(self, pk): self._pk = pk
    def pubkey(self): return self._pk
class SolanaClientStub:
    behaviour = "ok"           # ok | send_failed | unconfirmed
    loaded_pubkey = AGENT_OP
    def __init__(self, rpc, keypair_path, program_id):
        self.keypair_path = keypair_path
        self._keypair = KeypairStub(Pubkey.from_string(SolanaClientStub.loaded_pubkey))
        self.calls = []
        CALLS.append(("init", keypair_path))
    def derive_escrow_pda(self): return Pubkey(b"E" * 32)
    def confirm_delivery(self, lat, lon, timestamp=None, seller=None):
        CALLS.append(("confirm", lat, lon, seller))
        if SolanaClientStub.behaviour == "send_failed":
            raise RuntimeError("confirm_delivery send failed: boom")
        if SolanaClientStub.behaviour == "unconfirmed":
            raise RuntimeError("confirm_delivery TX sent but not confirmed after 3 attempts: SIGmaybe")
        return "SIGconfirm111"
    def close_escrow(self):
        CALLS.append(("close",)); return "SIGclose111"
    def confirm_transaction(self, sig, timeout=60.0): return True
    pay_behaviour = "ok"          # ok | unconfirmed
    def pay_memo(self, to_pubkey, lamports, memo):
        CALLS.append(("pay_memo", to_pubkey, lamports, memo))
        if SolanaClientStub.pay_behaviour == "unconfirmed":
            raise RuntimeError("pay_memo TX sent but not confirmed: SIGpaymaybe")
        return "SIGpay111"

class PeaqOwnershipStub:
    behaviour = "ok"           # ok | lookup_fails
    owner_pubkey = "5W7EiKuNZTfj8f3sxQPUnyMPXpJj7NjjE1uYyRCwPWgi"

    @staticmethod
    def resolve(machine_id):
        CALLS.append(("owner_lookup", machine_id))
        if PeaqOwnershipStub.behaviour == "lookup_fails":
            import peaq_ownership
            raise peaq_ownership.OwnerLookupError("machine has no native Solana record yet")
        return Pubkey.from_string(PeaqOwnershipStub.owner_pubkey)

CALLS = []

def install_stubs():
    solders = types.ModuleType("solders")
    pk = types.ModuleType("solders.pubkey"); pk.Pubkey = Pubkey
    for name, attrs in [("solders.keypair", ["Keypair"]), ("solders.signature", ["Signature"]),
                        ("solders.instruction", ["AccountMeta", "Instruction"]),
                        ("solders.message", ["Message"]), ("solders.transaction", ["Transaction"])]:
        m = types.ModuleType(name)
        for a in attrs: setattr(m, a, type(a, (), {}))
        sys.modules[name] = m
    sys.modules["solders"] = solders; sys.modules["solders.pubkey"] = pk
    solana = types.ModuleType("solana"); rpc = types.ModuleType("solana.rpc")
    api = types.ModuleType("solana.rpc.api"); api.Client = RpcClient
    sys.modules["solana"] = solana; sys.modules["solana.rpc"] = rpc
    sys.modules["solana.rpc.api"] = api
    sc = types.ModuleType("solana_client"); sc.SolanaClient = SolanaClientStub
    sys.modules["solana_client"] = sc
    po = types.ModuleType("peaq_ownership")
    class _OwnerLookupErrorStub(RuntimeError):
        pass
    po.OwnerLookupError = _OwnerLookupErrorStub
    po.build_client = lambda peaq_rpc_url: PeaqOwnershipStub()
    po.current_owner = lambda client, machine_id: PeaqOwnershipStub.resolve(machine_id)
    sys.modules["peaq_ownership"] = po

# ------------------------------------------------------------------ HTTP stub
JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
    "HBwcJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPDIzM//AABEIAAEAAQMBIgACEQEDEQH/xAAfAAAB"
    "BQEBAQEBAQAAAAAAAAAAAQIDBAUGBwgJCgv/2gAMAwEAAhADEAAAAT8A/9k=")
HTTP_LOG = []

class FakeResp:
    def __init__(self, code=200, payload=None, content=b""):
        self.status_code = code; self._payload = payload; self.content = content
    def json(self): return self._payload

def fake_get(url, timeout=None, **kw):
    HTTP_LOG.append(("GET", url))
    if url.endswith("/active_order"):
        o = ORDERS[min(len(HTTP_LOG_ORDER), len(ORDERS) - 1)]
        HTTP_LOG_ORDER.append(1)
        return FakeResp(200, dict(o)) if o is not None else FakeResp(204, None)
    if url.endswith("/debug/frame"):
        if CAMERA["mode"] == "error":
            return FakeResp(503, None, b"")
        return FakeResp(200, None, JPEG)
    if url.endswith("/ultrasonic"):
        return FakeResp(200, {"distance_cm": DISTANCE[0]})
    raise AssertionError("unexpected GET " + url)

VERIFIER_PUBKEY = "GgGBSqTZbTF4VJjoB6DDpJmoeuzQDjZMzU8xruNLR6Nt"
SECOND_OPINION = {"approved": True, "reason": "Box and QR code visible, distance matches."}

def fake_post(url, json=None, timeout=None, **kw):
    HTTP_LOG.append(("POST", url, json))
    if url.endswith("/verify"):
        if not json.get("payment_signature"):
            return FakeResp(402, {"amount_lamports": 1_000_000, "pay_to": VERIFIER_PUBKEY,
                                  "reference": "refABC123"})
        return FakeResp(200, {"reference": json.get("reference"), "paid_lamports": 1_000_000,
                              **SECOND_OPINION})
    return FakeResp(200, {"ok": True})

# ------------------------------------------------------------------ Model stub
class Blk:
    def __init__(self, **kw): self.__dict__.update(kw)
    def model_dump(self, mode=None, exclude_none=False):
        d = dict(self.__dict__)
        return {k: v for k, v in d.items() if not (exclude_none and v is None)}
def tool(name, tid, inp=None): return Blk(type="tool_use", name=name, id=tid, input=inp or {})
def text(t): return Blk(type="text", text=t)
class FakeResponse:
    def __init__(self, content): self.content = content
class FakeMessages:
    def __init__(self, script): self.script = script; self.n = 0
    def create(self, **kw):
        assert kw["model"] and kw["tools"] and kw["system"]
        if MODEL["raise_on_turn"] == self.n + 1:
            raise RuntimeError("API is down")
        turn = self.script[min(self.n, len(self.script) - 1)]; self.n += 1
        return FakeResponse(turn)
class FakeAnthropic:
    script = []
    def __init__(self, *a, **kw):
        assert "timeout" in kw and "max_retries" in kw, "API timeout/retries not set"
        self.messages = FakeMessages(list(FakeAnthropic.script))

def install_anthropic():
    m = types.ModuleType("anthropic"); m.Anthropic = FakeAnthropic
    sys.modules["anthropic"] = m

# ------------------------------------------------------------------ Cases
BASE_ORDER = {"lat": 52.3609, "lon": 14.06, "escrow_tx": "TXcreate", "status": "pending",
              "trigger_mode": "agent", "buyer_pubkey": BUYER}
ORDERS = [dict(BASE_ORDER)]
HTTP_LOG_ORDER = []
DISTANCE = [18.5]
CAMERA = {"mode": "ok"}
MODEL = {"raise_on_turn": 0}

KEYFILE = Path(tempfile.gettempdir()) / "agent_selftest_key.json"
KEYFILE.write_text("[1,2,3]")


def run_case(name, script, argv=("--dry-run",), chain=None, orders=None, distance=18.5,
             camera="ok", behaviour="ok", loaded_pubkey=AGENT_OP, keypath=str(KEYFILE),
             raise_on_turn=0, env=None, expect_rc=0, expect_abort=None,
             second_opinion=None, pay_behaviour="ok", peaq_owner_behaviour="ok"):
    global ORDERS, DISTANCE, SECOND_OPINION
    CHAIN["reads"] = chain if chain is not None else [escrow_bytes()]
    CHAIN["n"] = 0
    ORDERS = orders if orders is not None else [dict(BASE_ORDER)]
    del HTTP_LOG_ORDER[:]
    DISTANCE = [distance]
    CAMERA["mode"] = camera
    MODEL["raise_on_turn"] = raise_on_turn
    SolanaClientStub.behaviour = behaviour
    SolanaClientStub.pay_behaviour = pay_behaviour
    SolanaClientStub.loaded_pubkey = loaded_pubkey
    PeaqOwnershipStub.behaviour = peaq_owner_behaviour
    SECOND_OPINION = second_opinion or {"approved": True, "reason": "Looks correct."}
    del HTTP_LOG[:]; del CALLS[:]
    FakeAnthropic.script = script
    for mod in [m for m in list(sys.modules) if m.startswith("delivery_agent") or m == "config"]:
        del sys.modules[mod]
    install_stubs(); install_anthropic()
    sys.path.insert(0, str(AGENT_DIR))
    import requests as rq
    rq.get, rq.post = fake_get, fake_post
    os.environ.update({
        "ANTHROPIC_API_KEY": "test", "SELLER_PUBKEY": SELLER,
        "PICAR_SERVER_URL": "http://picar.test", "PC_SERVER_URL": "http://buyer.test",
        "AGENT_WALLET_KEYPAIR_PATH": keypath, "REQUIRE_DISTANCE": "true",
    })
    os.environ.pop("MACHINE_ID", None)
    for k, v in (env or {}).items():
        os.environ[k] = v
    import importlib
    da = importlib.import_module("delivery_agent")
    da.DECISIONS_DIR = RUNS / name
    sys.argv = ["delivery_agent.py"] + list(argv)
    try:
        rc = da.main()
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else str(e.code)
    runs = sorted((RUNS / name).glob("*/trace.json"))
    trace = json.loads(runs[-1].read_text()) if runs else None
    if expect_abort is not None:
        ok = isinstance(rc, str) and expect_abort in rc
        detail = "abort contains '%s'" % expect_abort
    else:
        ok = rc == expect_rc
        detail = "rc=%s (expected %s)" % (rc, expect_rc)
    print("[%s] %-32s %s" % ("PASS" if ok else "FAIL", name, detail))
    if not ok:
        fails.append("%s: unexpected %r" % (name, rc))
    return rc, trace, (RUNS / name)


def check(cond, msg):
    if not cond: fails.append(msg)


LOOK = tool("look_at_camera", "t1")
MEASURE = tool("measure_distance", "t2")
SECOND_OPINION_TOOL = tool("request_second_opinion", "t2b")
def CONFIRM(reason="The robot is right in front of the marked box, distance 18.5 cm.", tid="t3"):
    return tool("confirm_delivery", tid, {"reason": reason})

# 1. Happy path
rc, tr, d = run_case("happy", [[text("Let me look first."), LOOK], [MEASURE],
                                [SECOND_OPINION_TOOL], [CONFIRM()]])
check(tr and tr["decision"]["confirmed"], "happy: did not confirm")
check(tr and tr["observations"]["frames"] == 1, "happy: frame not counted")
check(tr and tr["decision"]["confirm_signature"] == "dry-run-tx", "happy: dry run sent a transaction")
check(d and list(d.glob("*/frame_01.jpg")), "happy: frame not saved")
check(tr and '"data"' not in json.dumps(tr["messages"]), "happy: base64 image left in the trace")
check(not any(h[0] == "POST" and "/delivered" in h[1] for h in HTTP_LOG),
      "happy: dry run reported /delivered")
check(tr and len(tr["second_opinions"]) == 1 and tr["second_opinions"][0]["approved"],
      "happy: second opinion not recorded as approved")
check(any(c[0] == "pay_memo" for c in CALLS), "happy: verifier was not paid")

# 2. Photo and confirmation in the same response
rc, tr, _ = run_case("same_turn_confirm", [[LOOK, CONFIRM()], [text("Understood, I will wait.")]],
                     expect_rc=2)
check(tr and not tr["decision"]["confirmed"], "same_turn_confirm: should have refused")
check(tr and any("not seen a photo" in r for r in tr["decision"]["refusals"]),
      "same_turn_confirm: wrong refusal reason")

# 3. Confirming without ever looking
rc, tr, _ = run_case("no_look", [[CONFIRM()], [text("Understood.")]], expect_rc=2)
check(tr and not tr["decision"]["confirmed"], "no_look: should have refused")

# 4. Empty reason
rc, tr, _ = run_case("empty_reason", [[LOOK], [CONFIRM("   ")], [text("Understood.")]], expect_rc=2)
check(tr and not tr["decision"]["confirmed"], "empty_reason: should have refused")

# 5. Distance outside the window
rc, tr, _ = run_case("distance_far", [[LOOK], [SECOND_OPINION_TOOL], [CONFIRM()], [text("Understood.")]],
                     distance=120.0, expect_rc=2)
check(tr and any("outside the" in r for r in tr["decision"]["refusals"]),
      "distance_far: distance gate did not fire")

# 6. Ultrasonic sensor error
rc, tr, _ = run_case("distance_error", [[LOOK], [SECOND_OPINION_TOOL], [CONFIRM()], [text("Understood.")]],
                     distance=-2.0, expect_rc=2)
check(tr and any("no usable distance" in r for r in tr["decision"]["refusals"]),
      "distance_error: sensor error did not block")

# 7. Distance gate switched off
rc, tr, _ = run_case("distance_gate_off", [[LOOK], [SECOND_OPINION_TOOL], [CONFIRM()]],
                     distance=120.0, env={"REQUIRE_DISTANCE": "false"}, expect_rc=0)
check(tr and tr["decision"]["confirmed"], "distance_gate_off: should have confirmed")
check(tr and tr["config"]["distance_gate"] == "off", "distance_gate_off: trace does not record the gate as off")

# 8. Escrow changes between preflight and signing (a different order)
rc, tr, _ = run_case("escrow_swapped", [[LOOK], [SECOND_OPINION_TOOL], [CONFIRM()], [text("Understood.")]],
                     chain=[escrow_bytes(), escrow_bytes(amount=500_000_000)], expect_rc=2)
check(tr and any("different order" in r for r in tr["decision"]["refusals"]),
      "escrow_swapped: identity check did not fire")

# 9. The buyer app now has a different order
rc, tr, _ = run_case("order_swapped", [[LOOK], [SECOND_OPINION_TOOL], [CONFIRM()], [text("Understood.")]],
                     orders=[dict(BASE_ORDER), dict(BASE_ORDER, escrow_tx="TXneu")],
                     expect_rc=2)
check(tr and any("different active order" in r for r in tr["decision"]["refusals"]),
      "order_swapped: buyer-app cross-check did not fire")

# 10. Real run: signature, /delivered, close with confirmation
rc, tr, _ = run_case("real_send", [[LOOK], [SECOND_OPINION_TOOL], [CONFIRM()]], argv=(),
                     chain=[escrow_bytes(), escrow_bytes(), escrow_bytes(status=1)],
                     expect_rc=0)
check(tr and tr["decision"]["confirm_signature"] == "SIGconfirm111", "real_send: no signature")
check(tr and tr["decision"]["close_signature"] == "SIGclose111", "real_send: escrow not closed")
check(any(h[0] == "POST" and "/delivered" in h[1] for h in HTTP_LOG),
      "real_send: /delivered not reported")
check(any(h[0] == "POST" and "/delivered" in h[1] and h[2].get("escrow_tx") == "TXcreate"
          for h in HTTP_LOG), "real_send: escrow_tx not included in the report")

# 11. Close is skipped when a new order now sits at that address
rc, tr, _ = run_case("close_skipped", [[LOOK], [SECOND_OPINION_TOOL], [CONFIRM()]], argv=(),
                     chain=[escrow_bytes(), escrow_bytes(),
                            escrow_bytes(amount=500_000_000)], expect_rc=0)
check(tr and "skipped" in str(tr["decision"]["close_signature"]),
      "close_skipped: should have skipped the close")
check(("close",) not in CALLS, "close_skipped: close_escrow was sent anyway")

# 12. Send fails, the model must not get a second signing attempt
rc, tr, _ = run_case("send_failed_no_retry",
                     [[LOOK], [SECOND_OPINION_TOOL], [CONFIRM()], [CONFIRM(tid="t9")]],
                     argv=(), behaviour="send_failed", expect_rc=2)
check(len([c for c in CALLS if c[0] == "confirm"]) == 1,
      "send_failed_no_retry: more than one signing attempt")
check(tr and tr["decision"]["sign_attempted"] is True, "send_failed_no_retry: attempt not recorded")

# 13. Outcome unknown (sent but not confirmed) means exit code 3
rc, tr, _ = run_case("outcome_unknown", [[LOOK], [SECOND_OPINION_TOOL], [CONFIRM()]], argv=(),
                     behaviour="unconfirmed", expect_rc=3)
check(tr and tr["decision"]["outcome_unknown"] is True, "outcome_unknown: not flagged as unknown")
check(not any(h[0] == "POST" and "/delivered" in h[1] for h in HTTP_LOG),
      "outcome_unknown: reported a delivery")

# 14. Camera down
rc, tr, _ = run_case("camera_down", [[LOOK], [CONFIRM()], [text("Understood.")]],
                     camera="error", expect_rc=2)
check(tr and tr["observations"]["frames"] == 0, "camera_down: frame counted despite the error")

# 15. API exception mid-run: the trace must still be written
rc, tr, _ = run_case("api_error", [[LOOK], [CONFIRM()]], raise_on_turn=2, expect_rc=4)
check(tr is not None and tr["run"]["stop_reason"] == "error", "api_error: no trace written for the error")

# 16. The agent declines
rc, tr, _ = run_case("agent_declines", [[text("The photo is too dark, I am not confirming.")]],
                     expect_rc=2)
check(tr and tr["run"]["stop_reason"] == "agent_stopped_without_tool_call",
      "agent_declines: wrong stop_reason")

# 16b. Confirming without ever requesting a second opinion
rc, tr, _ = run_case("no_second_opinion", [[LOOK], [CONFIRM()], [text("Understood.")]],
                     expect_rc=2)
check(tr and any("no approved second opinion" in r for r in tr["decision"]["refusals"]),
      "no_second_opinion: guardrail did not fire")
check(tr and not tr["second_opinions"], "no_second_opinion: a second opinion was somehow recorded")

# 16c. The verifier rejects - confirm must refuse, citing the rejection specifically
rc, tr, _ = run_case("second_opinion_rejected",
                     [[LOOK], [SECOND_OPINION_TOOL], [CONFIRM()], [text("Understood.")]],
                     second_opinion={"approved": False, "reason": "The box is not visible."},
                     expect_rc=2)
check(tr and len(tr["second_opinions"]) == 1 and not tr["second_opinions"][0]["approved"],
      "second_opinion_rejected: rejection not recorded")
check(tr and any("did not approve" in r for r in tr["decision"]["refusals"]),
      "second_opinion_rejected: guardrail did not cite the rejection")

# 16d. Requesting a second opinion before any photo was taken
rc, tr, _ = run_case("second_opinion_no_photo", [[SECOND_OPINION_TOOL], [text("x")]], expect_rc=2)
check(tr and not tr["second_opinions"], "second_opinion_no_photo: should not have called the verifier")
check(not any(h[0] == "POST" and h[1].endswith("/verify") for h in HTTP_LOG),
      "second_opinion_no_photo: verifier was contacted anyway")

# 16e. Paying the verifier fails - no verdict, no confirm, payment not retried blindly
rc, tr, _ = run_case("second_opinion_payment_fails", [[LOOK], [SECOND_OPINION_TOOL], [text("x")]],
                     pay_behaviour="unconfirmed", expect_rc=2)
check(tr and not tr["second_opinions"], "second_opinion_payment_fails: a verdict was recorded despite the failed payment")
check(any(c[0] == "pay_memo" for c in CALLS), "second_opinion_payment_fails: payment was never attempted")
check(not any(h[0] == "POST" and h[1].endswith("/verify") and h[2].get("payment_signature")
              for h in HTTP_LOG), "second_opinion_payment_fails: verifier was called with a payment proof anyway")

# 16f. REQUIRE_SECOND_OPINION=false: the plain "AI agent" leg, no x402 involved
rc, tr, _ = run_case("second_opinion_disabled", [[LOOK], [CONFIRM()]],
                     env={"REQUIRE_SECOND_OPINION": "false"}, expect_rc=0)
check(tr and tr["decision"]["confirmed"], "second_opinion_disabled: should have confirmed")
check(not tr["second_opinions"], "second_opinion_disabled: a second opinion was requested anyway")
check(not any(h[0] == "POST" and h[1].endswith("/verify") for h in HTTP_LOG),
      "second_opinion_disabled: verifier was contacted despite being disabled")

# 17. Preflight aborts
run_case("wrong_seller", [[text("x")]], chain=[escrow_bytes(seller=BUYER)],
         expect_abort="WrongSeller")
run_case("expired", [[text("x")]], chain=[escrow_bytes(deadline_offset=-10)],
         expect_abort="DeliveryExpired")
run_case("already_delivered", [[text("x")]], chain=[escrow_bytes(status=1)],
         expect_abort="DELIVERED")
run_case("no_escrow", [[text("x")]], chain=[None], expect_abort="does not exist")
run_case("target_mismatch", [[text("x")]], chain=[escrow_bytes(lat_e7=1234)],
         expect_abort="two different orders")
run_case("fixed_mode_ignored", [[text("x")]], orders=[dict(BASE_ORDER, trigger_mode="fixed")],
         expect_abort="trigger_mode")

# 18. Wallet guardrails
run_case("no_keypath", [[text("x")]], keypath="", expect_abort="AGENT_WALLET_KEYPAIR_PATH")
run_case("keyfile_missing", [[text("x")]], keypath="/nonexistent/agent-key.json",
         expect_abort="not found")
run_case("fixed_operator_key", [[text("x")]], loaded_pubkey=FIXED_OP,
         expect_abort="fixed-QR operator")
run_case("wrong_agent_key", [[text("x")]], loaded_pubkey=BUYER,
         expect_abort="buyer app creates agent-mode escrows")

# 19. Tokenized ownership: lookup succeeds, resolved owner is paid instead
# of the default seller.
rc, tr, _ = run_case("tokenized_owner", [[LOOK], [SECOND_OPINION_TOOL], [CONFIRM()]], argv=(),
                     chain=[escrow_bytes(), escrow_bytes(), escrow_bytes(status=1)],
                     env={"MACHINE_ID": "12345"}, expect_rc=0)
check(tr and tr["decision"]["confirmed"], "tokenized_owner: should have confirmed")
check(any(c[0] == "owner_lookup" and c[1] == 12345 for c in CALLS),
      "tokenized_owner: owner lookup was not attempted")
check(any(c[0] == "confirm" and c[3] == Pubkey.from_string(PeaqOwnershipStub.owner_pubkey)
          for c in CALLS), "tokenized_owner: resolved owner was not passed to confirm_delivery")

# 20. Tokenized ownership: lookup fails - refuse, never sign. Real mode
# (argv=()), not dry-run: the owner lookup sits before the dry_run branch in
# confirm_delivery, so a dry-run run would "pass" this check even with the
# guardrail deleted (dry-run never calls the real SolanaClient.confirm_delivery
# regardless of why). argv=() is required to prove the guardrail itself blocks
# a real signing attempt, not just that dry-run is a no-op. The default
# single-item chain (PENDING, repeats) is enough: the refusal fires before any
# second escrow read past the one inside confirm_delivery, so nothing here
# ever reaches close_escrow_safely's own read.
rc, tr, _ = run_case("tokenized_owner_lookup_fails",
                     [[LOOK], [SECOND_OPINION_TOOL], [CONFIRM()], [text("Understood.")]],
                     argv=(), env={"MACHINE_ID": "12345"}, peaq_owner_behaviour="lookup_fails",
                     expect_rc=2)
check(tr and not tr["decision"]["confirmed"],
      "tokenized_owner_lookup_fails: should have refused")
check(tr and any("could not resolve the current Machine-NFT owner" in r
                 for r in tr["decision"]["refusals"]),
      "tokenized_owner_lookup_fails: wrong or missing refusal reason")
check(not any(c[0] == "confirm" for c in CALLS),
      "tokenized_owner_lookup_fails: confirm_delivery was attempted anyway")

print()
if fails:
    print("FAILED CHECKS:")
    for f in fails: print("  -", f)
    sys.exit(1)
print("All checks passed.")
