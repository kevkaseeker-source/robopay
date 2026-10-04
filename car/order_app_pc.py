#!/usr/bin/env python3
"""Buyer-facing order app + monitoring dashboard for Unit C — runs on the PC
(a laptop/desktop on the same network as the car for now; swap in ngrok or a
public host later, same as the drone project's order_server_pc.py).

Flow:
  1. Buyer opens this page, clicks "Bestellen" -> POST /order signs and sends
     create_delivery (locks DELIVERY_AMOUNT_SOL into escrow), using the fixed
     buyer keypair held by THIS server (not the RPi - buyer is a separate
     role from the car, see SETUP_AND_ARCHITECTURE.md).
  2. Page shows the box QR code (static, same every time - see car_main.py
     BOX_QR_CODE) for the buyer to hold up to the car's camera.
  3. car_main.py (on the RPi) polls GET /active_order here exactly like the
     drone's main.py polls order_server_pc.py - same API shape, so no
     changes needed there beyond pointing PC_SERVER_URL at this server.
  4. Once the car confirms (QR + distance match), it calls confirm_delivery
     itself (its own operator wallet signs - this server never touches that
     key) and POSTs the result back to /delivered here.
  5. This page also shows: who ordered, live camera/ultrasonic (embeds the
     car's own /dashboard once it's reachable), both tx signatures with
     Solana Explorer links, and both wallets' current Devnet balances.

Run:
    venv\\Scripts\\python.exe order_app_pc.py
"""

import hashlib
import json
import logging
import os
import struct
import time
from pathlib import Path

import qrcode
from flask import Flask, Response, jsonify, request, send_file
from io import BytesIO

from solana.rpc.api import Client
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.system_program import ID as SYSTEM_PROGRAM_ID
from solders.transaction import Transaction

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.devnet.solana.com")
PROGRAM_ID = os.getenv("DRONE_PROGRAM_ID", "3NmsWVX39uvzG3PBNPdSe4FTgudqSeLphJSbMDhV5F8Y")
OPERATOR_PUBKEY = os.getenv("OPERATOR_PUBKEY", "7VizNvqBSnHnP8ySnsjxxyUnBQCybVnJHBDRyvaThXia")
SELLER_PUBKEY = os.getenv("SELLER_PUBKEY", "7uoFeSG546UvK5HYyA97GVmJUTvrXWgGgkTgxspH4d1C")
BUYER_KEYPAIR_PATH = os.getenv("BUYER_KEYPAIR_PATH", str(Path(__file__).parent / "buyer.json"))

DELIVERY_AMOUNT_SOL = float(os.getenv("DELIVERY_AMOUNT_SOL", "0.20"))
DEADLINE_MINUTES = int(os.getenv("DEADLINE_MINUTES", "60"))
TARGET_LAT = float(os.getenv("TARGET_LAT", "52.3609"))
TARGET_LON = float(os.getenv("TARGET_LON", "14.0600"))

BOX_QR_CODE = os.getenv("BOX_QR_CODE", "ROBOPAY-BOX-C")
PICAR_SERVER_URL = os.getenv("PICAR_SERVER_URL", "")  # e.g. http://192.168.178.84:8080, set once the car's on the same network

DEVNET_EXPLORER = "https://explorer.solana.com/tx/{}?cluster=devnet"
PORT = int(os.getenv("ORDER_APP_PORT", "5000"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("order_app_pc")

# ---------------------------------------------------------------------------
# Solana client (buyer side)
# ---------------------------------------------------------------------------
rpc = Client(SOLANA_RPC_URL)
program_id = Pubkey.from_string(PROGRAM_ID)
operator_pubkey = Pubkey.from_string(OPERATOR_PUBKEY)
seller_pubkey = Pubkey.from_string(SELLER_PUBKEY)


def _disc(name: str) -> bytes:
    return hashlib.sha256(f"global:{name}".encode()).digest()[:8]


CREATE_DELIVERY_DISC = _disc("create_delivery")
CANCEL_DELIVERY_DISC = _disc("cancel_delivery")


def _load_keypair(path: str) -> Keypair:
    data = json.loads(Path(path).read_text())
    return Keypair.from_bytes(bytes(data))


buyer_kp = _load_keypair(BUYER_KEYPAIR_PATH)
log.info("Buyer wallet: %s", buyer_kp.pubkey())


def derive_escrow_pda() -> Pubkey:
    pda, _ = Pubkey.find_program_address([b"escrow", bytes(operator_pubkey)], program_id)
    return pda


def create_delivery(lat: float, lon: float) -> str:
    """Buyer locks DELIVERY_AMOUNT_SOL into the escrow PDA for this operator."""
    amount_lamports = int(DELIVERY_AMOUNT_SOL * 1_000_000_000)
    deadline = int(time.time()) + DEADLINE_MINUTES * 60
    data = (CREATE_DELIVERY_DISC
            + struct.pack("<Qqqq", amount_lamports, int(lat * 1e7), int(lon * 1e7), deadline))

    escrow_pda = derive_escrow_pda()
    accounts = [
        AccountMeta(pubkey=escrow_pda, is_signer=False, is_writable=True),
        AccountMeta(pubkey=buyer_kp.pubkey(), is_signer=True, is_writable=True),
        AccountMeta(pubkey=seller_pubkey, is_signer=False, is_writable=False),
        AccountMeta(pubkey=operator_pubkey, is_signer=False, is_writable=False),
        AccountMeta(pubkey=SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
    ]
    ix = Instruction(program_id=program_id, accounts=accounts, data=data)

    blockhash = rpc.get_latest_blockhash().value.blockhash
    msg = Message.new_with_blockhash([ix], buyer_kp.pubkey(), blockhash)
    tx = Transaction([buyer_kp], msg, blockhash)
    sig = str(rpc.send_transaction(tx).value)
    log.info("create_delivery sent: %s", sig)
    return sig


def cancel_delivery() -> str:
    """Buyer reclaims escrow lamports after the deadline has passed (on-chain checks this)."""
    escrow_pda = derive_escrow_pda()
    accounts = [
        AccountMeta(pubkey=escrow_pda, is_signer=False, is_writable=True),
        AccountMeta(pubkey=buyer_kp.pubkey(), is_signer=True, is_writable=True),
    ]
    ix = Instruction(program_id=program_id, accounts=accounts, data=CANCEL_DELIVERY_DISC)
    blockhash = rpc.get_latest_blockhash().value.blockhash
    msg = Message.new_with_blockhash([ix], buyer_kp.pubkey(), blockhash)
    tx = Transaction([buyer_kp], msg, blockhash)
    sig = str(rpc.send_transaction(tx).value)
    log.info("cancel_delivery sent: %s", sig)
    return sig


def get_balance_sol(pubkey_str: str) -> float:
    try:
        bal = rpc.get_balance(Pubkey.from_string(pubkey_str))
        return round(bal.value / 1e9, 4)
    except Exception as e:
        log.warning("balance check failed for %s: %s", pubkey_str, e)
        return None


# ---------------------------------------------------------------------------
# State (single active order, same shape as the drone's PC server) - persisted
# to disk, since the on-chain escrow PDA outlives any restart of this process
# and we can only ever have one order per operator anyway (see lib.rs).
# ---------------------------------------------------------------------------
STATE_FILE = Path(__file__).parent / "order_state.json"


def _load_state():
    if STATE_FILE.exists():
        d = json.loads(STATE_FILE.read_text())
        return d.get("active_order"), d.get("tx_history", [])
    return None, []


def _save_state():
    STATE_FILE.write_text(json.dumps({"active_order": _active_order, "tx_history": _tx_history}))


_active_order, _tx_history = _load_state()
_force_delivery = False
_rpi_logs = []

app = Flask(__name__)


@app.route("/")
def index():
    html = INDEX_HTML.replace("__BOX_QR_CODE__", BOX_QR_CODE).replace("__PICAR_SERVER_URL__", PICAR_SERVER_URL)
    return Response(html, mimetype="text/html")


@app.route("/order", methods=["POST"])
def place_order():
    global _active_order, _force_delivery
    if _active_order is not None:
        return jsonify({"success": False, "error": "Es läuft bereits eine Bestellung"}), 400

    body = request.get_json(silent=True) or {}
    lat = float(body.get("lat", TARGET_LAT))
    lon = float(body.get("lon", TARGET_LON))

    try:
        sig = create_delivery(lat, lon)
    except Exception as e:
        log.error("create_delivery failed: %s", e)
        return jsonify({"success": False, "error": str(e)}), 500

    _active_order = {
        "lat": lat, "lon": lon,
        "buyer_pubkey": str(buyer_kp.pubkey()),
        "escrow_tx": sig,
        "status": "pending",
        "delivery_tx": None,
        "ordered_at": time.time(),
    }
    _force_delivery = False
    _tx_history.append({"type": "create_delivery", "sig": sig, "t": time.time()})
    _save_state()
    return jsonify({"success": True, "tx": sig, "lat": lat, "lon": lon})


@app.route("/active_order")
def active_order():
    """car_main.py polls this - same shape as the drone's PC server."""
    if _active_order is None or _active_order["status"] != "pending":
        return jsonify({"status": "no_order"}), 204
    return jsonify(_active_order)


@app.route("/delivered", methods=["POST"])
def delivered():
    global _active_order, _force_delivery
    data = request.get_json() or {}
    delivery_tx = data.get("delivery_tx")
    log.info("DELIVERED! tx=%s", delivery_tx)
    if _active_order:
        _active_order["status"] = "delivered"
        _active_order["delivery_tx"] = delivery_tx
        if delivery_tx and delivery_tx != "dry-run-tx":
            _tx_history.append({"type": "confirm_delivery", "sig": delivery_tx, "t": time.time()})
    _force_delivery = False
    _save_state()
    return jsonify({"success": True})


@app.route("/force_delivery")
def force_delivery_status():
    return jsonify({"force": _force_delivery})


@app.route("/rpi_log", methods=["POST"])
def rpi_log():
    data = request.get_json() or {}
    msg = data.get("msg", "").strip()
    if msg:
        _rpi_logs.append({"msg": msg, "t": time.time()})
        if len(_rpi_logs) > 100:
            _rpi_logs.pop(0)
    return jsonify({"ok": True})


@app.route("/status")
def status():
    return jsonify(_active_order or {"status": "no_order"})


@app.route("/reset", methods=["POST"])
def reset_order():
    """Clear the current order (e.g. after a demo run) so a new one can start."""
    global _active_order
    _active_order = None
    _save_state()
    return jsonify({"ok": True})


@app.route("/cancel", methods=["POST"])
def cancel_order():
    """Reclaim escrow lamports on-chain (only works once the deadline has passed) and clear local state."""
    global _active_order
    try:
        sig = cancel_delivery()
    except Exception as e:
        log.error("cancel_delivery failed: %s", e)
        return jsonify({"success": False, "error": str(e)}), 500
    _tx_history.append({"type": "cancel_delivery", "sig": sig, "t": time.time()})
    _active_order = None
    _save_state()
    return jsonify({"success": True, "tx": sig})


@app.route("/clear_history", methods=["POST"])
def clear_history():
    """Wipe the displayed transaction list only - does NOT touch the wallet
    (buyer_kp is always loaded fresh from BUYER_KEYPAIR_PATH, unaffected by this)."""
    global _tx_history
    _tx_history = []
    _save_state()
    return jsonify({"ok": True})


@app.route("/wallets")
def wallets():
    return jsonify({
        "buyer": {"pubkey": str(buyer_kp.pubkey()), "sol": get_balance_sol(str(buyer_kp.pubkey()))},
        "owner": {"pubkey": SELLER_PUBKEY, "sol": get_balance_sol(SELLER_PUBKEY)},
    })


@app.route("/transactions")
def transactions():
    return jsonify(list(reversed(_tx_history)))


@app.route("/qrcode.png")
def qrcode_png():
    img = qrcode.make(BOX_QR_CODE)
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------
INDEX_HTML = """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RoboPay — Unit C Bestellung</title>
<style>
  body { margin:0; padding:16px; background:#111; color:#eee; font-family:system-ui,sans-serif; }
  h1 { font-size:1.1rem; margin:0 0 16px; }
  .grid { display:flex; flex-wrap:wrap; gap:16px; }
  .panel { background:#1b1b1b; border:1px solid #333; border-radius:8px; padding:14px 16px; flex:1; min-width:260px; }
  .label { color:#888; font-size:0.75rem; text-transform:uppercase; letter-spacing:0.04em; margin-bottom:4px; }
  button { background:#4ade80; color:#111; border:none; border-radius:6px; padding:10px 20px; font-weight:600; font-size:1rem; cursor:pointer; }
  button:disabled { background:#555; color:#999; cursor:not-allowed; }
  a { color:#60a5fa; }
  .qr { background:#fff; padding:12px; border-radius:8px; display:inline-block; margin-top:10px; }
  .qr img { display:block; width:180px; height:180px; }
  .mono { font-family:ui-monospace,monospace; font-size:0.85rem; word-break:break-all; }
  .status-pending { color:#facc15; }
  .status-delivered { color:#4ade80; }
  .video { max-width:320px; width:100%; border-radius:6px; border:1px solid #333; display:block; }
  .car-row { display:flex; gap:16px; flex-wrap:wrap; align-items:flex-start; }
  .sensor-box { flex:1; min-width:180px; }
  .sensor-stat { font-size:1.4rem; font-weight:700; margin:4px 0; }
  table { width:100%; border-collapse:collapse; font-size:0.85rem; }
  td { padding:4px 0; border-bottom:1px solid #2a2a2a; }
  .btn-secondary { background:#333; color:#eee; }
  .dpad { display:grid; grid-template-columns:repeat(3,48px); grid-template-rows:repeat(3,48px); gap:6px; margin-top:8px; }
  .dpad button { background:#2a2a2a; color:#eee; border:1px solid #444; border-radius:6px; font-size:1.2rem; padding:0; }
  .dpad button:hover { background:#3a3a3a; }
  .ctrl-row { display:flex; gap:24px; flex-wrap:wrap; margin-top:10px; }
</style>
</head>
<body>
<h1>RoboPay — Unit C Bestellung</h1>
<div class="grid">

  <div class="panel">
    <div class="label">Bestellen</div>
    <button id="orderBtn" onclick="placeOrder()">SOL ins Escrow einzahlen (0.20 SOL)</button>
    <div id="orderResult" style="margin-top:12px;"></div>
  </div>

  <div class="panel">
    <div class="label">Aktuelle Order</div>
    <div id="orderStatus">— keine aktive Bestellung —</div>
  </div>

  <div class="panel">
    <div class="label">Käufer-Wallet</div>
    <div id="buyerWallet" class="mono">lädt...</div>
    <div class="label" style="margin-top:12px;">Owner-Wallet (Verkäufer)</div>
    <div id="ownerWallet" class="mono">lädt...</div>
  </div>

  <div class="panel">
    <div class="label">Transaktionen</div>
    <table id="txTable"><tbody></tbody></table>
    <button class="btn-secondary" style="margin-top:10px; font-size:0.8rem; padding:6px 12px;" onclick="clearHistory()">Verlauf leeren</button>
  </div>

  <div class="panel" style="flex-basis:100%;">
    <div class="label">Auto — Live-Kamera, Sensoren &amp; Steuerung</div>
    <div class="ctrl-row" style="align-items:flex-start;">
      <div id="carFeed">__PICAR_SERVER_URL__ nicht konfiguriert — Auto-Dashboard nicht eingebettet.</div>
      <div>
        <div class="label">Fahren</div>
        <div class="dpad">
          <button onmousedown="drive(30,-30)" onmouseup="driveStop()" ontouchstart="drive(30,-30)" ontouchend="driveStop()">↖</button><button onmousedown="drive(40,0)" onmouseup="driveStop()" ontouchstart="drive(40,0)" ontouchend="driveStop()">▲</button><button onmousedown="drive(30,30)" onmouseup="driveStop()" ontouchstart="drive(30,30)" ontouchend="driveStop()">↗</button>
          <button onmousedown="drive(30,-30)" onmouseup="driveStop()" ontouchstart="drive(30,-30)" ontouchend="driveStop()">◀</button>
          <button onclick="driveStop()">■</button>
          <button onmousedown="drive(30,30)" onmouseup="driveStop()" ontouchstart="drive(30,30)" ontouchend="driveStop()">▶</button>
          <button onmousedown="drive(-30,-30)" onmouseup="driveStop()" ontouchstart="drive(-30,-30)" ontouchend="driveStop()">↙</button><button onmousedown="drive(-40,0)" onmouseup="driveStop()" ontouchstart="drive(-40,0)" ontouchend="driveStop()">▼</button><button onmousedown="drive(-30,30)" onmouseup="driveStop()" ontouchstart="drive(-30,30)" ontouchend="driveStop()">↘</button>
        </div>
      </div>
      <div>
        <div class="label">Kamera</div>
        <div class="dpad">
          <div></div><button onclick="nudgeCam(0,10)">▲</button><div></div>
          <button onclick="nudgeCam(-10,0)">◀</button>
          <button onclick="setCam(20,0)">●</button>
          <button onclick="nudgeCam(10,0)">▶</button>
          <div></div><button onclick="nudgeCam(0,-10)">▼</button><div></div>
        </div>
      </div>
    </div>
  </div>

</div>

<script>
const picarUrl = "__PICAR_SERVER_URL__";

async function placeOrder() {
  const btn = document.getElementById('orderBtn');
  btn.disabled = true;
  btn.textContent = 'sende Transaktion...';
  try {
    const r = await fetch('/order', { method: 'POST', headers: {'Content-Type':'application/json'}, body: '{}' });
    const d = await r.json();
    if (d.success) {
      document.getElementById('orderResult').innerHTML =
        `<div>Bestellt! <a href="https://explorer.solana.com/tx/${d.tx}?cluster=devnet" target="_blank">TX ansehen</a></div>
         <div class="qr"><img src="/qrcode.png"></div>
         <div style="margin-top:6px; color:#888; font-size:0.85rem;">Diesen Code dem Auto zeigen.</div>`;
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
      el.innerHTML = `<span class="${cls}">${d.status}</span><br>Käufer: <span class="mono">${d.buyer_pubkey}</span>`;
      if (d.status === 'delivered') { btn.disabled = true; btn.textContent = 'Bestellung abgeschlossen'; }
    }
  } catch (e) {}
}

async function pollWallets() {
  try {
    const r = await fetch('/wallets');
    const d = await r.json();
    document.getElementById('buyerWallet').textContent = `${d.buyer.pubkey}\\n${d.buyer.sol} SOL`;
    document.getElementById('ownerWallet').textContent = `${d.owner.pubkey}\\n${d.owner.sol} SOL`;
  } catch (e) {}
}

const TX_LABELS = {
  create_delivery: 'Buyer TX (Escrow-Einzahlung)',
  confirm_delivery: 'Escrow-Release TX (Auszahlung)',
  cancel_delivery: 'Cancel TX (Rueckerstattung)',
};

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

async function clearHistory() {
  await fetch('/clear_history', { method: 'POST' });
  pollTx();
}

async function drive(speed, angle) {
  if (!picarUrl) return;
  try {
    await fetch(picarUrl + ':8080/drive', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ speed, angle })
    });
  } catch (e) {}
}

async function driveStop() {
  if (!picarUrl) return;
  try { await fetch(picarUrl + ':8080/stop'); } catch (e) {}
}

let camPan = 20, camTilt = 0;
async function setCam(pan, tilt) {
  if (!picarUrl) return;
  camPan = pan; camTilt = tilt;
  try {
    await fetch(picarUrl + ':8080/camera/angle', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ pan: camPan, tilt: camTilt })
    });
  } catch (e) {}
}
function nudgeCam(dPan, dTilt) { setCam(camPan + dPan, camTilt + dTilt); }

if (picarUrl) {
  document.getElementById('carFeed').innerHTML = `
    <div class="car-row">
      <img class="video" src="${picarUrl}:9000/mjpg">
      <div class="sensor-box">
        <div class="label">Ultraschall</div>
        <div class="sensor-stat" id="distStat">— cm</div>
        <div class="label" style="margin-top:10px;">QR-Code</div>
        <div class="sensor-stat" id="qrStat" style="font-size:1rem;">—</div>
      </div>
    </div>`;
  setInterval(async () => {
    try {
      const [d, q] = await Promise.all([
        fetch(picarUrl + ':8080/ultrasonic').then(r => r.json()),
        fetch(picarUrl + ':8080/camera/qr').then(r => r.json()),
      ]);
      document.getElementById('distStat').textContent = `${d.distance_cm} cm`;
      document.getElementById('qrStat').textContent = q.qr || '— kein QR erkannt —';
    } catch (e) {}
  }, 1000);
}

pollStatus(); pollWallets(); pollTx();
setInterval(pollStatus, 2000);
setInterval(pollWallets, 5000);
setInterval(pollTx, 5000);
</script>
</body>
</html>"""


if __name__ == "__main__":
    log.info("Starting order app on port %d", PORT)
    log.info("Buyer: %s  Owner: %s  Operator: %s", buyer_kp.pubkey(), SELLER_PUBKEY, OPERATOR_PUBKEY)
    app.run(host="0.0.0.0", port=PORT, threaded=True, debug=False, use_reloader=False)
