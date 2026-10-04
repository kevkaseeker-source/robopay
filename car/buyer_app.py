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
import os
import struct
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

import robopay_common as common

BUYER_KEYPAIR_PATH = os.getenv("BUYER_KEYPAIR_PATH", str(Path(__file__).parent / "buyer.json"))
PORT = int(os.getenv("BUYER_APP_PORT", "5001"))

CREATE_DELIVERY_DISC = common.disc("create_delivery")
CANCEL_DELIVERY_DISC = common.disc("cancel_delivery")


def _load_keypair(path: str) -> Keypair:
    data = json.loads(Path(path).read_text())
    return Keypair.from_bytes(bytes(data))


buyer_kp = _load_keypair(BUYER_KEYPAIR_PATH)


def create_delivery(lat: float, lon: float, operator_pubkey) -> str:
    amount_lamports = int(common.DELIVERY_AMOUNT_SOL * 1_000_000_000)
    deadline = int(time.time()) + common.DEADLINE_MINUTES * 60
    data = (CREATE_DELIVERY_DISC
            + struct.pack("<Qqqq", amount_lamports, int(lat * 1e7), int(lon * 1e7), deadline))
    escrow_pda = common.derive_escrow_pda(operator_pubkey)
    seller_pubkey = common.Pubkey.from_string(common.SELLER_PUBKEY)
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
    body = request.get_json(silent=True) or {}
    lat = float(body.get("lat", common.TARGET_LAT))
    lon = float(body.get("lon", common.TARGET_LON))
    trigger_mode = body.get("trigger_mode") if body.get("trigger_mode") in ("fixed", "agent") else "fixed"
    try:
        sig = create_delivery(lat, lon, _operator_for_mode(trigger_mode))
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    _active_order = {
        "lat": lat, "lon": lon, "buyer_pubkey": str(buyer_kp.pubkey()),
        "escrow_tx": sig, "status": "pending", "delivery_tx": None, "ordered_at": time.time(),
        "trigger_mode": trigger_mode,
    }
    _force_delivery = False
    _tx_history.append({"type": "create_delivery", "sig": sig, "t": time.time()})
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
    _tx_history.append({"type": "create_delivery", "sig": body["escrow_tx"], "t": time.time()})
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
    return jsonify(_active_order or {"status": "no_order"})


@app.route("/reset", methods=["POST"])
def reset_order():
    global _active_order
    _active_order = None
    common.save_state(_active_order, _tx_history)
    return jsonify({"ok": True})


@app.route("/cancel", methods=["POST"])
def cancel_order():
    global _active_order
    trigger_mode = (_active_order or {}).get("trigger_mode", "fixed")
    try:
        sig = cancel_delivery(_operator_for_mode(trigger_mode))
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    _tx_history.append({"type": "cancel_delivery", "sig": sig, "t": time.time()})
    _active_order = None
    common.save_state(_active_order, _tx_history)
    return jsonify({"success": True, "tx": sig})


@app.route("/clear_history", methods=["POST"])
def clear_history():
    global _tx_history
    _tx_history = []
    common.save_state(_active_order, _tx_history)
    return jsonify({"ok": True})


@app.route("/wallet")
def wallet():
    return jsonify({"pubkey": str(buyer_kp.pubkey()), "sol": common.get_balance_sol(str(buyer_kp.pubkey()))})


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
<body>
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
    <div class="label">Mein Wallet</div>
    <div id="myWallet" class="mono">lädt...</div>
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

  <div class="panel">
    <div class="label">Meine Transaktionen</div>
    <table id="txTable"><tbody></tbody></table>
    <button class="btn-secondary" onclick="clearHistory()">Verlauf leeren</button>
  </div>

</div>

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
      btn.disabled = false; btn.textContent = 'SOL ins Escrow einzahlen (0.20 SOL)';
    } else {
      const cls = d.status === 'delivered' ? 'status-delivered' : 'status-pending';
      const modeLabel = d.trigger_mode === 'agent' ? 'KI-Agent' : 'Fester QR-Code';
      el.innerHTML = `<span class="${cls}">${d.status}</span> <span style="color:#888;">(${modeLabel})</span>`;
      if (d.status === 'delivered') {
        // Delivered doesn't block a new order (see place_order() server-side) -
        // only keep the button disabled while something is actually pending.
        btn.disabled = false; btn.textContent = 'SOL ins Escrow einzahlen (0.20 SOL)';
        if (d.delivery_tx && d.delivery_tx !== 'dry-run-tx') {
          el.innerHTML += `<br><a href="https://explorer.solana.com/tx/${d.delivery_tx}?cluster=devnet" target="_blank">Escrow-Release TX ansehen</a>`;
        }
      } else {
        btn.disabled = true; btn.textContent = 'Bestellung läuft...';
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

async function pollWallet() {
  try {
    const r = await fetch('/wallet');
    const d = await r.json();
    document.getElementById('myWallet').textContent = `${d.pubkey}\\n${d.sol} SOL`;
  } catch (e) {}
}

async function pollOperatorWallets() {
  try {
    const r = await fetch('/operator_wallets');
    const d = await r.json();
    document.getElementById('fixedOperatorWallet').textContent = `${d.fixed.pubkey}\\n${d.fixed.sol} SOL`;
    document.getElementById('agentOperatorWallet').textContent = `${d.agent.pubkey}\\n${d.agent.sol} SOL`;
  } catch (e) {}
}

async function clearHistory() {
  await fetch('/clear_history', { method: 'POST' });
  pollTx();
}

async function pollTx() {
  try {
    const r = await fetch('/transactions');
    const d = await r.json();
    const tbody = document.querySelector('#txTable tbody');
    tbody.innerHTML = d.map(tx =>
      `<tr><td>${TX_LABELS[tx.type] || tx.type}</td><td><a href="https://explorer.solana.com/tx/${tx.sig}?cluster=devnet" target="_blank">${tx.sig.slice(0,12)}...</a></td></tr>`
    ).join('') || '<tr><td>— noch keine Transaktionen —</td></tr>';
  } catch (e) {}
}

pollStatus(); pollWallet(); pollOperatorWallets(); pollTx();
setInterval(pollStatus, 2000);
setInterval(pollWallet, 5000);
setInterval(pollOperatorWallets, 5000);
setInterval(pollTx, 5000);
</script>
</body>
</html>"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True, debug=False, use_reloader=False)
