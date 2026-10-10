#!/usr/bin/env python3
"""Buyer app for RoboPay Unit C — places an order (locks Devnet SOL into
escrow), shows the box QR code to hold up to the car's camera, and the
buyer's own Devnet TX/wallet. Split out of the old order_app_pc.py so the
buyer and the car owner/seller each get their own app (see seller_app.py)
with their own login, matching their actual separate roles.

This app also stays the one car_main.py talks to (PC_SERVER_URL) - it owns
the order state (order_state.json), since it's the one creating orders.
The machine-facing endpoints (/active_order, /delivered, /force_delivery,
/rpi_log, /register_external_order) skip the browser login - car_main.py
has no concept of HTTP auth - and instead require the shared MACHINE_TOKEN
in the X-RoboPay-Machine-Token header.

Env vars required: BUYER_USERNAME, BUYER_PASSWORD
Run:
    venv\\Scripts\\python.exe buyer_app.py
"""

import json
import re
import os
import struct
import threading
import time
from io import BytesIO
from pathlib import Path

import qrcode
from flask import Flask, Response, jsonify, request, send_file
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.system_program import ID as SYSTEM_PROGRAM_ID
from solders.transaction import Transaction

import activity
import robopay_common as common
import wallet_ops

BUYER_KEYPAIR_PATH = os.getenv("BUYER_KEYPAIR_PATH", str(Path(__file__).parent / "buyer.json"))
PORT = int(os.getenv("BUYER_APP_PORT", "5001"))

CREATE_DELIVERY_DISC = common.disc("create_delivery")
CANCEL_DELIVERY_DISC = common.disc("cancel_delivery")


def _load_keypair(path: str) -> Keypair:
    data = json.loads(Path(path).read_text())
    return Keypair.from_bytes(bytes(data))


buyer_kp = _load_keypair(BUYER_KEYPAIR_PATH)


def create_delivery(lat: float, lon: float, operator_pubkey, seller_pubkey, deadline: int) -> str:
    """Fund the escrow. seller_pubkey is the only wallet the program will ever
    release it to - the car's owner at order time (see place_order)."""
    amount_lamports = int(common.DELIVERY_AMOUNT_SOL * 1_000_000_000)
    data = (CREATE_DELIVERY_DISC
            + struct.pack("<Qqqq", amount_lamports, int(lat * 1e7), int(lon * 1e7), deadline))
    escrow_pda = common.derive_escrow_pda(operator_pubkey)
    accounts = [
        AccountMeta(pubkey=escrow_pda, is_signer=False, is_writable=True),
        AccountMeta(pubkey=buyer_kp.pubkey(), is_signer=True, is_writable=True),
        AccountMeta(pubkey=seller_pubkey, is_signer=False, is_writable=False),
        AccountMeta(pubkey=operator_pubkey, is_signer=False, is_writable=False),
        AccountMeta(pubkey=SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
    ]
    ix = Instruction(program_id=common.program_id, accounts=accounts, data=data)
    blockhash = common.rpc.get_latest_blockhash().value.blockhash
    msg = Message.new_with_blockhash([ix], buyer_kp.pubkey(), blockhash)
    tx = Transaction([buyer_kp], msg, blockhash)
    return str(common.rpc.send_transaction(tx).value)


def cancel_delivery(operator_pubkey) -> str:
    escrow_pda = common.derive_escrow_pda(operator_pubkey)
    accounts = [
        AccountMeta(pubkey=escrow_pda, is_signer=False, is_writable=True),
        AccountMeta(pubkey=buyer_kp.pubkey(), is_signer=True, is_writable=True),
    ]
    ix = Instruction(program_id=common.program_id, accounts=accounts, data=CANCEL_DELIVERY_DISC)
    blockhash = common.rpc.get_latest_blockhash().value.blockhash
    msg = Message.new_with_blockhash([ix], buyer_kp.pubkey(), blockhash)
    tx = Transaction([buyer_kp], msg, blockhash)
    return str(common.rpc.send_transaction(tx).value)


def _operator_for_mode(trigger_mode: str):
    return common.agent_operator_pubkey if trigger_mode == "agent" else common.operator_pubkey


_active_order, _tx_history = common.load_state()
_force_delivery = False

app = Flask(__name__)
MACHINE_PATHS = {"/active_order", "/delivered", "/force_delivery", "/rpi_log", "/register_external_order"}
common.make_auth(app, "BUYER_USERNAME", "BUYER_PASSWORD", exempt_paths=MACHINE_PATHS)
activity.register_routes(app, lambda: (_active_order, _tx_history), common.OPERATOR_PUBKEY,
                         lambda active: str(buyer_kp.pubkey()))
common.require_machine_token(app, MACHINE_PATHS)


@app.route("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")


@app.route("/order", methods=["POST"])
def place_order():
    global _active_order, _force_delivery
    # Only a still-pending order blocks a new one - a "delivered" record just
    # sits here for the buyer's own confirmation display (see pollStatus() in
    # INDEX_HTML) and gets overwritten by the fresh order below. Blocking on
    # "any _active_order at all" meant every single order after the very
    # first one failed here forever, since /delivered (below) never clears
    # it - found 2026-09-17 during the first live end-to-end test, matches
    # the same "needs to be reset after completion" bug fixed on the car
    # side (car_main.py now auto-closes the escrow after confirm_delivery).
    if _active_order is not None and _active_order.get("status") == "pending":
        return jsonify({"success": False, "error": "Es läuft bereits eine Bestellung"}), 400
    if common.is_paused():
        return jsonify({"success": False,
                        "error": "Das Auto ist pausiert (Besitzerwechsel laeuft) - bitte spaeter bestellen"}), 409
    body = request.get_json(silent=True) or {}
    lat = float(body.get("lat", common.TARGET_LAT))
    lon = float(body.get("lon", common.TARGET_LON))
    trigger_mode = body.get("trigger_mode") if body.get("trigger_mode") in ("fixed", "agent") else "fixed"
    # The escrow pays whoever owns the car right now (peaq Machine-NFT -> DID
    # -> Solana wallet). If that can't be determined, no escrow is created -
    # there is deliberately no fallback to a configured wallet.
    try:
        seller, owner_evm = common._resolve_owner_details()
    except common.payout_policy.PayoutRefused as e:
        return jsonify({"success": False, "error": f"Besitzer des Autos nicht ermittelbar: {e}"}), 503
    if common.handoff_pending((seller, owner_evm)):
        return jsonify({"success": False, "error": "Das Auto hat einen neuen Besitzer - Bestellungen sind moeglich, "
                        "sobald er sich einmal in der CarOwnerApp angemeldet hat"}), 409
    deadline = int(time.time()) + common.DEADLINE_MINUTES * 60
    try:
        sig = create_delivery(lat, lon, _operator_for_mode(trigger_mode), seller, deadline)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    _active_order = {
        "lat": lat, "lon": lon, "buyer_pubkey": str(buyer_kp.pubkey()),
        "escrow_tx": sig, "status": "pending", "delivery_tx": None, "ordered_at": time.time(),
        "trigger_mode": trigger_mode, "seller": str(seller), "deadline": deadline,
    }
    _force_delivery = False
    _tx_history.append({"type": "create_delivery", "sig": sig, "t": time.time(),
                        "buyer": str(buyer_kp.pubkey()), "seller": str(seller)})
    common.save_state(_active_order, _tx_history)
    return jsonify({"success": True, "tx": sig, "lat": lat, "lon": lon})


@app.route("/register_external_order", methods=["POST"])
def register_external_order():
    """Record an order that was funded OUTSIDE this process — used by
    agent/kitchen_agent.py, which signs create_delivery itself with its own
    keypair (never this app's buyer_kp) and then calls this endpoint so
    car_main.py / delivery_agent.py, which only ever poll /active_order on
    THIS app, actually learn the escrow exists. This endpoint does not touch
    the chain or any keypair — it only mirrors what the normal /order handler
    above already writes to _active_order after a successful create_delivery,
    so the two paths converge on the exact same state car_main.py already
    knows how to consume; trigger_mode still selects fixed vs. agent exactly
    as before, unrelated to who the buyer was.

    Exempted from HTTP Basic Auth (see MACHINE_PATHS) for the same reason as
    /active_order and /rpi_log: this is a machine-to-machine call, and the
    escrow_tx it reports is independently verifiable on-chain — a forged call
    here cannot invent funds, only point car_main.py at an order that either
    is or isn't actually funded, which confirm_delivery's own on-chain check
    would reject either way.
    """
    global _active_order, _force_delivery
    if _active_order is not None and _active_order.get("status") == "pending":
        return jsonify({"success": False, "error": "Es läuft bereits eine Bestellung"}), 400
    body = request.get_json(silent=True) or {}
    required = ("lat", "lon", "escrow_tx", "buyer_pubkey")
    missing = [k for k in required if k not in body]
    if missing:
        return jsonify({"success": False, "error": f"missing fields: {missing}"}), 400
    trigger_mode = body.get("trigger_mode") if body.get("trigger_mode") in ("fixed", "agent") else "fixed"
    _active_order = {
        "lat": float(body["lat"]), "lon": float(body["lon"]),
        "buyer_pubkey": body["buyer_pubkey"],
        "escrow_tx": body["escrow_tx"], "status": "pending", "delivery_tx": None,
        "ordered_at": time.time(), "trigger_mode": trigger_mode,
        "buyer_mode": body.get("buyer_mode", "external"),
    }
    _force_delivery = False
    _tx_history.append({"type": "create_delivery", "sig": body["escrow_tx"], "t": time.time(),
                        "buyer": body["buyer_pubkey"]})
    common.save_state(_active_order, _tx_history)
    return jsonify({"success": True})


@app.route("/active_order")
def active_order():
    if _active_order is None or _active_order["status"] != "pending":
        return jsonify({"status": "no_order"}), 204
    return jsonify(_active_order)


@app.route("/delivered", methods=["POST"])
def delivered():
    global _active_order, _force_delivery
    data = request.get_json() or {}
    delivery_tx = data.get("delivery_tx")
    if _active_order:
        _active_order["status"] = "delivered"
        _active_order["delivery_tx"] = delivery_tx
        if delivery_tx and delivery_tx != "dry-run-tx":
            _tx_history.append({"type": "confirm_delivery", "sig": delivery_tx, "t": time.time(),
                                 "escrow_tx": _active_order.get("escrow_tx")})
    _force_delivery = False
    common.save_state(_active_order, _tx_history)
    return jsonify({"success": True})


@app.route("/force_delivery")
def force_delivery_status():
    return jsonify({"force": _force_delivery})


@app.route("/rpi_log", methods=["POST"])
def rpi_log():
    return jsonify({"ok": True})


@app.route("/status")
def status():
    d = dict(_active_order or {"status": "no_order"})
    d.update(paused=common.is_paused(), now=time.time())
    return jsonify(d)


@app.route("/reset", methods=["POST"])
def reset_order():
    global _active_order
    _active_order = None
    common.save_state(_active_order, _tx_history)
    return jsonify({"ok": True})


def auto_refund(now=None):
    """Refund a pending order once its deadline has passed: the program then
    no longer lets the car release it (DeliveryExpired), so the money would
    otherwise sit in the escrow. Returns the refund signature, or None."""
    global _active_order
    now = time.time() if now is None else now
    o = _active_order
    if not o or o.get("status") != "pending" or not o.get("deadline") or now <= o["deadline"]:
        return None
    try:
        sig = cancel_delivery(_operator_for_mode(o.get("trigger_mode", "fixed")))
    except Exception as e:
        o["refund_error"] = str(e)
        common.save_state(_active_order, _tx_history)
        return None
    o.update(status="refunded", refund_tx=sig, refund_error=None)
    _tx_history.append({"type": "cancel_delivery", "sig": sig, "t": now, "auto": True,
                        "escrow_tx": o.get("escrow_tx")})
    common.save_state(_active_order, _tx_history)
    return sig


def _auto_refund_loop():
    while True:
        time.sleep(30)
        try:
            auto_refund()
        except Exception as e:  # never let the background thread die
            print("auto_refund failed:", e)


@app.route("/cancel", methods=["POST"])
def cancel_order():
    global _active_order
    trigger_mode = (_active_order or {}).get("trigger_mode", "fixed")
    try:
        sig = cancel_delivery(_operator_for_mode(trigger_mode))
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    _tx_history.append({"type": "cancel_delivery", "sig": sig, "t": time.time(),
                        "escrow_tx": (_active_order or {}).get("escrow_tx")})
    _active_order = None
    common.save_state(_active_order, _tx_history)
    return jsonify({"success": True, "tx": sig})


@app.route("/clear_history", methods=["POST"])
def clear_history():
    global _tx_history
    _tx_history = []
    common.save_state(_active_order, _tx_history)
    return jsonify({"ok": True})


def _buyer_peaq():
    """The buyer's peaq wallet (key file from BUYER_PEAQ_KEY_PATH), or None."""
    path = os.getenv("BUYER_PEAQ_KEY_PATH")
    if not path:
        return None
    return wallet_ops.PeaqWallet(Path(path).read_text().strip(), common.PEAQ_RPC_URL)


@app.route("/wallet")
def wallet():
    peaq = None
    try:
        pw = _buyer_peaq()
        if pw is not None:
            peaq = {"address": pw.address, "balance": pw.balance()}
    except Exception as e:
        peaq = {"error": str(e)}
    return jsonify({"pubkey": str(buyer_kp.pubkey()), "sol": common.get_balance_sol(str(buyer_kp.pubkey())),
                    "peaq": peaq})


def _amount(raw):
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return v if 0 < v < float("inf") else None


@app.route("/wallet/send", methods=["POST"])
def wallet_send():
    """Send from the buyer's own wallet: SOL (devnet), PEAQ or a Machine-NFT (peaq mainnet)."""
    body = request.get_json(silent=True) or {}
    asset, to = body.get("asset"), str(body.get("to", "")).strip()
    try:
        if asset == "sol":
            try:
                common.Pubkey.from_string(to)
            except Exception:
                return jsonify({"success": False, "error": "keine gueltige Solana-Adresse"}), 400
            amount = _amount(body.get("amount"))
            if amount is None:
                return jsonify({"success": False, "error": "Betrag ungueltig"}), 400
            sig = wallet_ops.send_sol(common.rpc, buyer_kp, to, amount)
            activity.add_event({"chain": "solana", "kind": "sol", "tx": sig, "t": time.time(),
                                "from": str(buyer_kp.pubkey()), "to": to, "amount": amount})
            return jsonify({"success": True, "tx": sig, "link": common.DEVNET_EXPLORER.format(sig)})
        if asset not in ("peaq", "nft"):
            return jsonify({"success": False, "error": "asset muss sol, peaq oder nft sein"}), 400
        if not re.fullmatch(r"0x[0-9a-fA-F]{40}", to):
            return jsonify({"success": False, "error": "keine gueltige peaq-Adresse (0x...)"}), 400
        amount = machine_id = None
        if asset == "peaq":
            amount = _amount(body.get("amount"))
            if amount is None:
                return jsonify({"success": False, "error": "Betrag ungueltig"}), 400
        else:
            try:
                machine_id = int(str(body.get("machine_id", "")).strip())
            except ValueError:
                machine_id = 0
            if machine_id <= 0:
                return jsonify({"success": False, "error": "Machine-ID ungueltig"}), 400
            busy = _active_order is not None and _active_order.get("status") == "pending"
            if str(machine_id) == str(common.PEAQ_MACHINE_ID) and busy:
                return jsonify({"success": False, "error": "Waehrend einer Lieferung kann das Auto-NFT nicht "
                                "uebertragen werden"}), 409
        pw = _buyer_peaq()
        if pw is None:
            return jsonify({"success": False, "error": "Der Buyer hat noch keine peaq-Wallet "
                            "(BUYER_PEAQ_KEY_PATH nicht gesetzt)"}), 503
        tx = pw.send_peaq(to, amount) if asset == "peaq" else pw.transfer_nft(machine_id, to)
        ev = {"chain": "peaq", "kind": asset, "tx": tx, "t": time.time(), "from": pw.address.lower(), "to": to.lower()}
        ev.update({"amount": amount} if asset == "peaq" else {"token_id": str(machine_id)})
        activity.add_event(ev)
        return jsonify({"success": True, "tx": tx, "link": f"https://peaq.subscan.io/tx/{tx}"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500



@app.route("/operator_wallets")
def operator_wallets():
    """Read-only visibility into both delivery-confirmation operator wallets -
    the RPi's existing fixed-QR one (car_main.py) and the AI agent's (not
    built yet, see docs/superpowers/specs/2026-09-17-delivery-agent-design.md
    - the wallet already exists so its balance is visible ahead of the agent
    itself). Same public-data read as /wallet, no private keys involved."""
    return jsonify({
        "fixed": {"pubkey": common.OPERATOR_PUBKEY, "sol": common.get_balance_sol(common.OPERATOR_PUBKEY)},
        "agent": {"pubkey": common.AGENT_OPERATOR_PUBKEY, "sol": common.get_balance_sol(common.AGENT_OPERATOR_PUBKEY)},
    })


@app.route("/transactions")
def transactions():
    mine = [t for t in _tx_history if t["type"] in ("create_delivery", "cancel_delivery", "confirm_delivery")]
    return jsonify(list(reversed(mine)))


@app.route("/qrcode.png")
def qrcode_png():
    img = qrcode.make(common.BOX_QR_CODE)
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


INDEX_HTML = """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RoboPay — Bestellen</title>
<style>
  body { margin:0; padding:16px; background:#111; color:#eee; font-family:system-ui,sans-serif; }
  h1 { font-size:1.1rem; margin:0 0 16px; }
  .grid { display:flex; flex-wrap:wrap; gap:16px; }
  .panel { background:#1b1b1b; border:1px solid #333; border-radius:8px; padding:14px 16px; flex:1; min-width:260px; }
  .label { color:#888; font-size:0.75rem; text-transform:uppercase; letter-spacing:0.04em; margin-bottom:4px; }
  button { background:#4ade80; color:#111; border:none; border-radius:6px; padding:14px 24px; font-weight:600; font-size:1.05rem; cursor:pointer; width:100%; }
  button:disabled { background:#555; color:#999; }
  .btn-secondary { background:#333; color:#eee; width:auto; padding:6px 12px; font-size:0.8rem; font-weight:400; margin-top:10px; }
  a { color:#60a5fa; }
  .qr { background:#fff; padding:12px; border-radius:8px; display:inline-block; margin-top:10px; }
  .qr img { display:block; width:200px; height:200px; max-width:100%; }
  .mono { font-family:ui-monospace,monospace; font-size:0.85rem; word-break:break-all; }
  .status-pending { color:#facc15; }
  .status-delivered { color:#4ade80; }
  table { width:100%; border-collapse:collapse; font-size:0.85rem; }
  td { padding:4px 0; border-bottom:1px solid #2a2a2a; }
</style>
</head>
<body data-role="buyer">
<h1>RoboPay — Auto bestellen</h1>
<div class="grid">

  <div class="panel">
    <div class="label">Bestellen</div>
    <div style="margin-bottom:12px;">
      <label style="display:block; margin-bottom:6px; cursor:pointer;">
        <input type="radio" name="triggerMode" value="fixed" checked onchange="updateModeHint()"> Fester QR-Code
      </label>
      <label style="display:block; cursor:pointer;">
        <input type="radio" name="triggerMode" value="agent" onchange="updateModeHint()"> KI-Agent
      </label>
      <div id="modeHint" style="margin-top:6px; color:#888; font-size:0.8rem;"></div>
    </div>
    <button id="orderBtn" onclick="placeOrder()">SOL ins Escrow einzahlen (0.20 SOL)</button>
    <div id="orderResult" style="margin-top:12px;"></div>
  </div>

  <div class="panel">
    <div class="label">Meine Order</div>
    <div id="orderStatus">— keine aktive Bestellung —</div>
  </div>

  <div class="panel">
    <div class="label">Mein Wallet · Solana Devnet</div>
    <div id="myWallet" class="mono">lädt...</div>
    <div class="label" style="margin-top:12px;">Mein Wallet · peaq Mainnet</div>
    <div id="myPeaqWallet" class="mono">lädt...</div>
  </div>

  <div class="panel">
    <div class="label">Senden</div>
    <select id="sendAsset" onchange="updateSendForm()" style="width:100%; padding:8px; background:#111; color:#eee; border:1px solid #333; border-radius:6px;">
      <option value="sol">SOL (Solana Devnet)</option>
      <option value="peaq">PEAQ (peaq Mainnet)</option>
      <option value="nft">Machine-NFT (peaq Mainnet)</option>
    </select>
    <input id="sendTo" placeholder="Empfänger (Solana-Adresse)" style="width:100%; margin-top:8px; padding:8px; background:#111; color:#eee; border:1px solid #333; border-radius:6px; box-sizing:border-box;">
    <input id="sendAmount" placeholder="Betrag SOL" inputmode="decimal" style="width:100%; margin-top:8px; padding:8px; background:#111; color:#eee; border:1px solid #333; border-radius:6px; box-sizing:border-box;">
    <button style="margin-top:10px;" onclick="sendFromWallet()">Senden</button>
    <div id="sendResult" style="margin-top:8px; font-size:0.85rem;"></div>
  </div>

  <div class="panel">
    <div class="label">Operator-Wallets (Escrow-Release)</div>
    <div style="margin-bottom:8px;">
      <div style="color:#888; font-size:0.75rem;">RPi (fester QR-Code)</div>
      <div id="fixedOperatorWallet" class="mono">lädt...</div>
    </div>
    <div>
      <div style="color:#888; font-size:0.75rem;">KI-Agent</div>
      <div id="agentOperatorWallet" class="mono">lädt...</div>
    </div>
  </div>

  <div class="panel" style="flex-basis:100%;" id="rpActivity"><div class="label">Transaktionen</div>lädt…</div>

</div>

<script src="static/robopay_activity.js"></script>
<script>
const TX_LABELS = {
  create_delivery: 'Buyer TX (Escrow-Einzahlung)',
  cancel_delivery: 'Cancel TX (Rueckerstattung)',
  confirm_delivery: 'Escrow-Release TX (Auszahlung)',
};

function updateModeHint() {
  const mode = document.querySelector('input[name="triggerMode"]:checked').value;
  const hint = document.getElementById('modeHint');
  hint.textContent = mode === 'agent'
    ? 'Hinweis: Der KI-Agent ist noch nicht gebaut - diese Bestellung wird aktuell von niemandem automatisch bestätigt. Nach Ablauf der Frist per "Stornieren" rückerstattbar.'
    : '';
}

async function placeOrder() {
  const btn = document.getElementById('orderBtn');
  const mode = document.querySelector('input[name="triggerMode"]:checked').value;
  btn.disabled = true;
  btn.textContent = 'sende Transaktion...';
  try {
    const r = await fetch('/order', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({trigger_mode: mode}) });
    const d = await r.json();
    if (d.success) {
      document.getElementById('orderResult').innerHTML =
        `<div>Bestellt! <a href="https://explorer.solana.com/tx/${d.tx}?cluster=devnet" target="_blank">TX auf Solana Explorer ansehen</a></div>
         <div class="qr"><img src="/qrcode.png"></div>
         <div style="margin-top:6px; color:#888; font-size:0.85rem;">Diesen Code dem Auto zeigen, um die Zahlung freizugeben.</div>`;
      document.getElementById('orderResult').dataset.qrShown = '1';
    } else {
      document.getElementById('orderResult').textContent = 'Fehler: ' + d.error;
      btn.disabled = false; btn.textContent = 'SOL ins Escrow einzahlen (0.20 SOL)';
    }
  } catch (e) {
    document.getElementById('orderResult').textContent = 'Fehler: ' + e;
    btn.disabled = false; btn.textContent = 'SOL ins Escrow einzahlen (0.20 SOL)';
  }
}

async function pollStatus() {
  try {
    const r = await fetch('/status');
    const d = await r.json();
    const el = document.getElementById('orderStatus');
    const btn = document.getElementById('orderBtn');
    if (d.status === 'no_order') {
      el.textContent = '— keine aktive Bestellung —';
      if (d.paused) { btn.disabled = true; btn.textContent = 'Auto pausiert (Besitzerwechsel)'; }
      else { btn.disabled = false; btn.textContent = 'SOL ins Escrow einzahlen (0.20 SOL)'; }
    } else {
      if (d.status === 'refunded') {
        el.innerHTML = `<span class="status-delivered">zurückgebucht</span><br>
          <span style="font-size:0.85rem;">Das Auto hat die Lieferung nicht innerhalb der Frist bestätigt. Deine 0.20 SOL wurden automatisch zurück in dein Wallet gebucht.</span>
          <br><a href="https://explorer.solana.com/tx/${d.refund_tx}?cluster=devnet" target="_blank">Rückbuchung ansehen</a>`;
        btn.disabled = !!d.paused; btn.textContent = d.paused ? 'Auto pausiert (Besitzerwechsel)' : 'SOL ins Escrow einzahlen (0.20 SOL)';
        document.getElementById('orderResult').dataset.qrShown = '';
        return;
      }
      const cls = d.status === 'delivered' ? 'status-delivered' : 'status-pending';
      const modeLabel = d.trigger_mode === 'agent' ? 'KI-Agent' : 'Fester QR-Code';
      el.innerHTML = `<span class="${cls}">${d.status}</span> <span style="color:#888;">(${modeLabel})</span>`;
      if (d.seller) {
        el.innerHTML += `<br><span style="color:#888; font-size:0.8rem;">Auszahlung an den Besitzer:</span> <span class="mono">${d.seller.slice(0,4)}…${d.seller.slice(-4)}</span>`;
      }
      if (d.status === 'delivered') {
        // Delivered doesn't block a new order (see place_order() server-side) -
        // only keep the button disabled while something is actually pending.
        btn.disabled = false; btn.textContent = 'SOL ins Escrow einzahlen (0.20 SOL)';
        if (d.delivery_tx && d.delivery_tx !== 'dry-run-tx') {
          el.innerHTML += `<br><a href="https://explorer.solana.com/tx/${d.delivery_tx}?cluster=devnet" target="_blank">Escrow-Release TX ansehen</a>`;
        }
      } else {
        btn.disabled = true; btn.textContent = 'Bestellung läuft...';
        // Refund: the program only lets the buyer reclaim the escrow after
        // its deadline (cancel_delivery), e.g. when the car is off or the
        // ownership changed during the delivery and the car refused to pay.
        if (d.deadline) {
          const left = d.deadline - d.now;
          el.innerHTML += left > 0
            ? `<br><span style="color:#888; font-size:0.8rem;">Wird bis ${new Date(d.deadline*1000).toLocaleTimeString()} nicht geliefert, buchen wir deine SOL automatisch zurück.</span>`
            : `<br><span style="font-size:0.85rem;">Frist abgelaufen - Rückbuchung läuft automatisch.</span>`
              + (d.refund_error ? ` <button class="btn-secondary" onclick="refund()">Rückerstattung jetzt anfordern</button>` : '');
        }
        // The QR only used to appear right after clicking the button itself
        // (placeOrder()'s own success handler) - reloading the page, or an
        // order placed some other way, showed nothing at all even though the
        // order was genuinely pending and the box QR is always valid. Show
        // it any time there's a pending order, not just right after placing it.
        if (!document.getElementById('orderResult').dataset.qrShown) {
          document.getElementById('orderResult').innerHTML =
            `<div class="qr"><img src="/qrcode.png"></div>
             <div style="margin-top:6px; color:#888; font-size:0.85rem;">Diesen Code dem Auto zeigen, um die Zahlung freizugeben.</div>`;
          document.getElementById('orderResult').dataset.qrShown = '1';
        }
      }
    }
    if (d.status !== 'pending') {
      document.getElementById('orderResult').dataset.qrShown = '';
    }
  } catch (e) {}
}

async function refund() {
  try {
    const r = await fetch('/cancel', { method: 'POST' });
    const d = await r.json();
    document.getElementById('orderResult').textContent = d.success
      ? 'Rückerstattung gesendet: ' + d.tx
      : 'Rückerstattung fehlgeschlagen: ' + d.error;
    pollStatus(); if (window.RoboPayUI) RoboPayUI.refresh();
  } catch (e) {
    document.getElementById('orderResult').textContent = 'Fehler: ' + e;
  }
}

// Address + copy button, then the balance on its own line.
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const addrLine = (a) => `${esc(a)}${window.RoboPayUI ? RoboPayUI.copyButton(a) : ''}`;

async function pollWallet() {
  try {
    const r = await fetch('/wallet');
    const d = await r.json();
    document.getElementById('myWallet').innerHTML = `${addrLine(d.pubkey)}<br>${esc(d.sol)} SOL`;
    const pe = document.getElementById('myPeaqWallet');
    if (!d.peaq) pe.textContent = '— noch keine peaq-Wallet eingerichtet —';
    else if (d.peaq.error) pe.textContent = 'Fehler: ' + d.peaq.error;
    else pe.innerHTML = `${addrLine(d.peaq.address)}<br>${d.peaq.balance.toFixed(4)} PEAQ`;
  } catch (e) {}
}

function updateSendForm() {
  const a = document.getElementById('sendAsset').value;
  document.getElementById('sendTo').placeholder = a === 'sol' ? 'Empfänger (Solana-Adresse)' : 'Empfänger (0x…)';
  document.getElementById('sendAmount').placeholder = a === 'nft' ? 'Machine-ID des NFT' : (a === 'sol' ? 'Betrag SOL' : 'Betrag PEAQ');
}

async function sendFromWallet() {
  const asset = document.getElementById('sendAsset').value;
  const to = document.getElementById('sendTo').value.trim();
  const val = document.getElementById('sendAmount').value.trim();
  const what = asset === 'nft' ? `das Machine-NFT ${val}` : `${val} ${asset.toUpperCase()}`;
  if (!confirm(`${what} an ${to} senden?` + (asset === 'sol' ? '' : ' (peaq Mainnet, echtes Geld)'))) return;
  const out = document.getElementById('sendResult');
  out.textContent = 'sende…';
  try {
    const body = asset === 'nft' ? { asset, to, machine_id: val } : { asset, to, amount: val };
    const r = await fetch('/wallet/send', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
    const d = await r.json();
    out.innerHTML = d.success ? `Gesendet ✓ <a href="${d.link}" target="_blank">Transaktion ansehen</a>` : 'Fehler: ' + d.error;
    pollWallet();
  } catch (e) { out.textContent = 'Fehler: ' + e; }
}

async function pollOperatorWallets() {
  try {
    const r = await fetch('/operator_wallets');
    const d = await r.json();
    document.getElementById('fixedOperatorWallet').innerHTML = `${addrLine(d.fixed.pubkey)}<br>${esc(d.fixed.sol)} SOL`;
    document.getElementById('agentOperatorWallet').innerHTML = `${addrLine(d.agent.pubkey)}<br>${esc(d.agent.sol)} SOL`;
  } catch (e) {}
}

pollStatus(); pollWallet(); pollOperatorWallets();
setInterval(pollStatus, 2000);
setInterval(pollWallet, 5000);
setInterval(pollOperatorWallets, 5000);
</script>
</body>
</html>"""


if __name__ == "__main__":
    threading.Thread(target=_auto_refund_loop, daemon=True, name="auto-refund").start()
    app.run(host="0.0.0.0", port=PORT, threaded=True, debug=False, use_reloader=False)
