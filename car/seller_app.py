#!/usr/bin/env python3
"""Seller / car-operator app for RoboPay Unit C — live camera + ultrasonic
(reads picar_server.py directly, works over an MCC tunnel once
PICAR_SERVER_URL points at the tunnel's DNS name), drive + camera-angle
controls, the owner's Devnet wallet balance, and a link to the
Escrow-Release TX once the car has confirmed a delivery. Split out of the
old order_app_pc.py - this app never touches the buyer's or the car's
private keys, it only calls picar_server.py's HTTP API and reads the
shared order_state.json (written by buyer_app.py / car_main.py) for the
transaction history.

This is the foundation for the future Car Owner Solana Mobile dApp - same
picar_server.py endpoints, same read-only relationship to the wallets.

Env vars required: SELLER_USERNAME, SELLER_PASSWORD
Also needs PICAR_SERVER_URL (e.g. http://backend-server or http://192.168.178.84)
Run:
    venv\\Scripts\\python.exe seller_app.py
"""

import html
import json
import os

import requests
from flask import Flask, Response, jsonify, request, stream_with_context

import mobile_ownership
import robopay_common as common
import sensor_logger

PORT = int(os.getenv("SELLER_APP_PORT", "5002"))
PICAR_TIMEOUT = 5

app = Flask(__name__)
# /mobile/* is exempt from the static password - it has its own,
# wallet-signature-based ownership gate instead (see mobile_ownership.py).
# A static password would be extractable from the CarOwnerApp's public
# APK, which the /mobile/* endpoints are built to avoid needing at all.
common.make_auth(app, "SELLER_USERNAME", "SELLER_PASSWORD", exempt_paths=("/mobile/challenge", "/mobile/verify"))
sensor_logger.start()


@app.route("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")


@app.route("/wallet")
def wallet():
    return jsonify({"pubkey": common.SELLER_PUBKEY, "sol": common.get_balance_sol(common.SELLER_PUBKEY)})


@app.route("/operator_wallets")
def operator_wallets():
    """Read-only visibility into the delivery-confirmation operator wallet
    (the RPi's fixed-QR one, car_main.py). The AI-agent operator wallet
    (docs/superpowers/specs/2026-09-17-delivery-agent-design.md) is
    deliberately not shown here - x402/agent payments aren't in scope for
    the car right now. Same public-data read as /wallet, no private keys
    involved."""
    return jsonify({
        "fixed": {"pubkey": common.OPERATOR_PUBKEY, "sol": common.get_balance_sol(common.OPERATOR_PUBKEY)},
    })


@app.route("/crosschain_wallet")
def crosschain_wallet():
    """Read-only cross-chain wallet view (Phantom/Trust-Wallet style) of
    the car's two on-chain identities - its Solana operator wallet and its
    peaq-chain machine identity. No private keys involved, nothing here
    can sign anything. Ownership is NOT live-verified against peaq's DID
    registry (that needs the full peaq_os_sdk + rpi/peaq_ownership.py's
    resolution logic) - the Machine-ID is shown as-is, deliberately simple
    for this first version."""
    return jsonify({
        "solana": {
            "address": common.OPERATOR_PUBKEY,
            "balance": common.get_balance_sol(common.OPERATOR_PUBKEY),
            "symbol": "SOL",
            "network": "Devnet",
        },
        "peaq": {
            "address": common.PEAQ_OPERATOR_ADDRESS,
            "balance": common.get_balance_peaq(common.PEAQ_OPERATOR_ADDRESS),
            "symbol": "PEAQ",
            "network": "Mainnet",
        },
        "nft": {
            "machine_id": common.PEAQ_MACHINE_ID,
            "network": "Mainnet",
        },
    })


# ---------------------------------------------------------------------------
# CarOwnerApp (mobile) API - ownership-gated via wallet signature instead of
# a static password (see mobile_ownership.py's module docstring for why: a
# password embedded in a public APK is trivially extractable). No session,
# no cookie - every /mobile/verify call re-proves ownership from scratch.
# ---------------------------------------------------------------------------
@app.route("/mobile/challenge")
def mobile_challenge():
    return jsonify({"message": mobile_ownership.build_challenge()})


@app.route("/mobile/verify", methods=["POST"])
def mobile_verify():
    body = request.get_json(force=True, silent=True) or {}
    pubkey = body.get("pubkey")
    message = body.get("message")
    signature = body.get("signature")
    if not pubkey or not message or not signature:
        return jsonify({"error": "pubkey, message, and signature are all required"}), 400

    try:
        mobile_ownership.verify_ownership(
            pubkey, message, signature, common.PEAQ_RPC_URL, int(common.PEAQ_MACHINE_ID)
        )
    except mobile_ownership.OwnershipCheckError as e:
        return jsonify({"error": str(e)}), 403

    try:
        r = requests.get(f"{common.PICAR_SERVER_URL}:8080/debug/frame", timeout=PICAR_TIMEOUT)
        snapshot_ok = r.status_code == 200
    except Exception:
        snapshot_ok = False

    try:
        u = requests.get(f"{common.PICAR_SERVER_URL}:8080/ultrasonic", timeout=PICAR_TIMEOUT)
        distance_cm = u.json().get("distance_cm") if u.status_code == 200 else None
    except Exception:
        distance_cm = None

    return jsonify({
        "solana": {
            "address": common.OPERATOR_PUBKEY,
            "balance": common.get_balance_sol(common.OPERATOR_PUBKEY),
            "symbol": "SOL",
            "network": "Devnet",
        },
        "peaq": {
            "address": common.PEAQ_OPERATOR_ADDRESS,
            "balance": common.get_balance_peaq(common.PEAQ_OPERATOR_ADDRESS),
            "symbol": "PEAQ",
            "network": "Mainnet",
        },
        "nft": {
            "machine_id": common.PEAQ_MACHINE_ID,
            "network": "Mainnet",
        },
        "sensors": {
            "distance_cm": distance_cm,
            "snapshot_available": snapshot_ok,
        },
    })


@app.route("/mobile/snapshot.jpg")
def mobile_snapshot():
    """Separate from /mobile/verify's JSON payload since a JPEG can't ride
    inside JSON - the app calls this right after a successful /mobile/verify
    to actually fetch the image. Deliberately NOT gated by signature itself
    (a snapshot alone, without the wallet/peaq context, isn't worth
    re-deriving the whole ownership check for) - acceptable for a hackathon
    MVP showing Devnet/test data, same trade-off as CHALLENGE_TTL_SECONDS."""
    try:
        r = requests.get(f"{common.PICAR_SERVER_URL}:8080/debug/frame", timeout=PICAR_TIMEOUT)
        return Response(r.content, status=r.status_code, mimetype="image/jpeg")
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/order_status")
def order_status():
    active_order, _ = common.load_state()
    return jsonify(active_order or {"status": "no_order"})


@app.route("/transactions")
def transactions():
    _, tx_history = common.load_state()
    mine = [t for t in tx_history if t["type"] == "confirm_delivery"]
    return jsonify(list(reversed(mine)))


@app.route("/gallery/<escrow_tx>")
def gallery(escrow_tx):
    # escrow_tx comes straight from the URL - resolve and confirm order_dir
    # is still inside SENSOR_DATA_DIR before listing/reading from it, same
    # guard as gallery_file() below, so a crafted "../../" can't make this
    # glob()/read_text() touch anything outside sensor_data. A rejected path
    # is treated the same as "no sensor data for this delivery" (order_dir
    # simply not existing) rather than a 404, since this route always
    # renders the same HTML page either way - no new response shape needed.
    base = sensor_logger.SENSOR_DATA_DIR.resolve()
    order_dir = (base / escrow_tx).resolve()
    entries = []
    if base in order_dir.parents and order_dir.exists():
        for json_path in sorted(order_dir.glob("*.json")):
            # A torn/partial write (disk-full mid-write, or this request
            # racing an in-flight capture) can leave an unparseable .json
            # behind - without this guard that one bad record would 500 the
            # whole gallery page forever, for every future visit to this
            # delivery. Skip it and keep rendering the rest instead. This
            # also makes an orphaned .jpg with no matching/valid .json
            # harmless by construction - it's simply never listed.
            try:
                record = json.loads(json_path.read_text())
            except (OSError, ValueError):
                continue
            entries.append({
                "filename": json_path.stem + ".jpg",
                "distance_cm": record.get("distance_cm"),
                "captured_at": record.get("captured_at"),
            })
    # escrow_tx comes straight from the URL path and is interpolated into raw
    # HTML below at two sites (img src attribute + h1) - this is an f-string
    # Response, not a Jinja render_template call, so there is no autoescape
    # safety net. Escape it once here and use the escaped value at both
    # sites, so a crafted "><script>...</script> segment can't inject
    # markup/script into an already-authenticated operator's browser
    # (reflected XSS).
    safe_escrow_tx = html.escape(escrow_tx)
    # Relative paths throughout (img src and the back link below) - this page
    # is served both directly (as /gallery/<escrow_tx> on :5002) and through
    # car/gateway_app.py's production reverse proxy (as
    # /seller/gallery/<escrow_tx>, mounted at /seller/). gateway_app.py only
    # rewrites root-relative paths on the INDEX page's HTML, not this page,
    # so an absolute "/gallery/..." src would 404 every image once the
    # gateway prefix is in play - the browser would request
    # /gallery/<escrow_tx>/<file> instead of /seller/gallery/<escrow_tx>/<file>.
    # A relative "{escrow_tx}/{filename}" resolves against this page's own
    # URL directory (".../gallery/" either way, since the browser drops the
    # last path segment - the escrow_tx - before appending), landing on
    # ".../gallery/<escrow_tx>/<file>" correctly in both cases, with no
    # gateway change needed.
    rows = "".join(
        f'<div class="panel" style="margin-bottom:12px;">'
        f'<img class="video" src="{safe_escrow_tx}/{e["filename"]}" style="max-width:240px;">'
        f'<div class="label" style="margin-top:6px;">Ultraschall: {e["distance_cm"]} cm</div>'
        f'<div class="label">{e["captured_at"]}</div></div>'
        for e in entries
    ) or '<div class="label">— keine Sensordaten für diese Lieferung —</div>'
    return Response(f"""<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RoboPay — Sensordaten</title>
<style>
  body {{ margin:0; padding:16px; background:#111; color:#eee; font-family:system-ui,sans-serif; }}
  .label {{ color:#888; font-size:0.75rem; text-transform:uppercase; letter-spacing:0.04em; margin-bottom:4px; }}
  .panel {{ background:#1b1b1b; border:1px solid #333; border-radius:8px; padding:14px 16px; }}
  .video {{ border-radius:6px; border:1px solid #333; display:block; }}
  a {{ color:#60a5fa; }}
</style></head>
<body>
<h1>Sensordaten — {safe_escrow_tx}</h1>
<p><a href="../">&larr; zurück</a></p>
{rows}
</body></html>""", mimetype="text/html")


@app.route("/gallery/<escrow_tx>/<filename>")
def gallery_file(escrow_tx, filename):
    # escrow_tx/filename come straight from the URL - resolve and confirm
    # the final path is still inside SENSOR_DATA_DIR before reading, so a
    # crafted "../../" can't escape it and read arbitrary server files.
    base = sensor_logger.SENSOR_DATA_DIR.resolve()
    path = (base / escrow_tx / filename).resolve()
    # This route only ever exists to serve the captured .jpg images linked
    # from gallery() above (the .json record files that sit alongside each
    # .jpg in the same directory were never meant to be fetched through
    # here) - so reject anything that isn't a .jpg rather than serving other
    # files back mislabeled as "image/jpeg".
    if base not in path.parents or path.suffix.lower() != ".jpg" or not path.exists():
        return jsonify({"error": "not found"}), 404
    return Response(path.read_bytes(), mimetype="image/jpeg")


# ---------------------------------------------------------------------------
# Proxy to picar_server.py — the browser (on a phone, anywhere on the public
# internet) can only reach THIS app's own domain. PICAR_SERVER_URL (e.g.
# http://backend-server, the MCC tunnel's DNS name) only resolves/routes
# from inside the MCC network - i.e. from this server itself, not from an
# arbitrary browser. So this server fetches picar_server.py's API
# server-side and relays it, rather than sending the car's address to the
# browser directly (found 2026-09-16: drive buttons did nothing from a
# phone because the browser was trying and failing to reach an
# MCC-internal hostname on its own).
# ---------------------------------------------------------------------------
@app.route("/proxy/ultrasonic")
def proxy_ultrasonic():
    try:
        r = requests.get(f"{common.PICAR_SERVER_URL}:8080/ultrasonic", timeout=PICAR_TIMEOUT)
        return Response(r.content, status=r.status_code, mimetype="application/json")
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/proxy/camera/qr")
def proxy_qr():
    try:
        r = requests.get(f"{common.PICAR_SERVER_URL}:8080/camera/qr", timeout=PICAR_TIMEOUT)
        return Response(r.content, status=r.status_code, mimetype="application/json")
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/proxy/drive", methods=["POST"])
def proxy_drive():
    try:
        r = requests.post(f"{common.PICAR_SERVER_URL}:8080/drive", json=request.get_json(force=True), timeout=PICAR_TIMEOUT)
        return Response(r.content, status=r.status_code, mimetype="application/json")
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/proxy/stop", methods=["POST", "GET"])
def proxy_stop():
    try:
        r = requests.get(f"{common.PICAR_SERVER_URL}:8080/stop", timeout=PICAR_TIMEOUT)
        return Response(r.content, status=r.status_code, mimetype="application/json")
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/proxy/camera/angle", methods=["POST"])
def proxy_camera_angle():
    try:
        r = requests.post(f"{common.PICAR_SERVER_URL}:8080/camera/angle", json=request.get_json(force=True), timeout=PICAR_TIMEOUT)
        return Response(r.content, status=r.status_code, mimetype="application/json")
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/proxy/mjpg")
def proxy_mjpg():
    try:
        upstream = requests.get(f"{common.PICAR_VIDEO_URL}:9000/mjpg", stream=True, timeout=10)
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    return Response(
        stream_with_context(upstream.iter_content(chunk_size=4096)),
        content_type=upstream.headers.get("Content-Type", "multipart/x-mixed-replace"),
    )


INDEX_HTML = """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RoboPay — Auto steuern</title>
<style>
  body { margin:0; padding:16px; background:#111; color:#eee; font-family:system-ui,sans-serif; }
  h1 { font-size:1.1rem; margin:0 0 16px; }
  .grid { display:flex; flex-wrap:wrap; gap:16px; }
  .panel { background:#1b1b1b; border:1px solid #333; border-radius:8px; padding:14px 16px; flex:1; min-width:260px; }
  .label { color:#888; font-size:0.75rem; text-transform:uppercase; letter-spacing:0.04em; margin-bottom:4px; }
  a { color:#60a5fa; }
  .mono { font-family:ui-monospace,monospace; font-size:0.85rem; word-break:break-all; }
  .status-pending { color:#facc15; }
  .status-delivered { color:#4ade80; }
  .video { max-width:320px; width:100%; border-radius:6px; border:1px solid #333; display:block; }
  .car-row { display:flex; gap:16px; flex-wrap:wrap; align-items:flex-start; }
  .sensor-box { flex:1; min-width:160px; }
  .sensor-stat { font-size:1.4rem; font-weight:700; margin:4px 0; }
  table { width:100%; border-collapse:collapse; font-size:0.85rem; }
  td { padding:4px 0; border-bottom:1px solid #2a2a2a; }
  .dpad { display:grid; grid-template-columns:repeat(3,48px); grid-template-rows:repeat(3,48px); gap:6px; margin-top:8px; }
  .dpad button { background:#2a2a2a; color:#eee; border:1px solid #444; border-radius:6px; font-size:1.2rem; padding:0; }
  .dpad button:hover { background:#3a3a3a; }
  .ctrl-row { display:flex; gap:24px; flex-wrap:wrap; margin-top:10px; }
  .wallet-row { display:flex; align-items:center; gap:10px; padding:8px 0; border-bottom:1px solid #2a2a2a; }
  .wallet-row:last-child { border-bottom:none; }
  .wallet-icon { width:28px; height:28px; border-radius:50%; flex:none; object-fit:cover; }
  .wallet-main { flex:1; min-width:0; }
  .wallet-amount { font-size:1rem; font-weight:700; }
  .wallet-addr { font-family:ui-monospace,monospace; font-size:0.72rem; color:#888; word-break:break-all; }
  .net-badge { font-size:0.65rem; text-transform:uppercase; letter-spacing:0.03em; padding:2px 6px; border-radius:4px; background:#2a2a2a; color:#aaa; flex:none; }
</style>
</head>
<body>
<h1>RoboPay — Auto steuern (Seller/Operator)</h1>
<div class="grid">

  <div class="panel">
    <div class="label">Owner-Wallet</div>
    <div id="ownerWallet" class="mono">lädt...</div>
  </div>

  <div class="panel">
    <div class="label">Operator-Wallet (Escrow-Release)</div>
    <div style="color:#888; font-size:0.75rem;">RPi (fester QR-Code)</div>
    <div id="fixedOperatorWallet" class="mono">lädt...</div>
  </div>

  <div class="panel">
    <div class="label">Wallet</div>
    <div class="wallet-row">
      <img class="wallet-icon" src="/static/solana-logo.png" alt="Solana">
      <div class="wallet-main">
        <div class="wallet-amount" id="walletSolAmount">lädt...</div>
        <div class="wallet-addr" id="walletSolAddr"></div>
      </div>
      <div class="net-badge" id="walletSolNet">—</div>
    </div>
    <div class="wallet-row">
      <img class="wallet-icon" src="/static/peaq-logo.png" alt="peaq">
      <div class="wallet-main">
        <div class="wallet-amount" id="walletPeaqAmount">lädt...</div>
        <div class="wallet-addr" id="walletPeaqAddr"></div>
      </div>
      <div class="net-badge" id="walletPeaqNet">—</div>
    </div>
    <div class="wallet-row">
      <svg class="wallet-icon" viewBox="0 0 28 28" role="img" aria-label="Machine NFT">
        <rect width="28" height="28" rx="14" fill="#312e81"/>
        <rect x="7" y="7" width="14" height="14" rx="3" fill="none" stroke="#a5b4fc" stroke-width="1.6"/>
        <circle cx="10.5" cy="10.5" r="1.3" fill="#a5b4fc"/>
        <circle cx="17.5" cy="10.5" r="1.3" fill="#a5b4fc"/>
        <circle cx="10.5" cy="17.5" r="1.3" fill="#a5b4fc"/>
        <circle cx="17.5" cy="17.5" r="1.3" fill="#a5b4fc"/>
        <path d="M10.5 10.5 L17.5 17.5 M17.5 10.5 L10.5 17.5" stroke="#a5b4fc" stroke-width="1" opacity="0.6"/>
      </svg>
      <div class="wallet-main">
        <div class="wallet-amount">Machine-NFT</div>
        <div class="wallet-addr" id="walletNftId">lädt...</div>
      </div>
      <div class="net-badge" id="walletNftNet">—</div>
    </div>
  </div>

  <div class="panel">
    <div class="label">Aktuelle Order</div>
    <div id="orderStatus">— keine aktive Bestellung —</div>
  </div>

  <div class="panel">
    <div class="label">Escrow-Release-Transaktionen</div>
    <table id="txTable"><tbody></tbody></table>
  </div>

  <div class="panel" style="flex-basis:100%;">
    <div class="label">Auto — Live-Kamera, Sensoren &amp; Steuerung</div>
    <div class="ctrl-row" style="align-items:flex-start;">
      <div id="carFeed"></div>
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
async function pollWallet() {
  try {
    const r = await fetch('/wallet');
    const d = await r.json();
    document.getElementById('ownerWallet').textContent = `${d.pubkey}\\n${d.sol} SOL`;
  } catch (e) {}
}

async function pollOperatorWallets() {
  try {
    const r = await fetch('/operator_wallets');
    const d = await r.json();
    document.getElementById('fixedOperatorWallet').textContent = `${d.fixed.pubkey}\\n${d.fixed.sol} SOL`;
  } catch (e) {}
}

async function pollCrosschainWallet() {
  try {
    const r = await fetch('/crosschain_wallet');
    const d = await r.json();
    document.getElementById('walletSolAmount').textContent = `${d.solana.balance ?? '—'} ${d.solana.symbol}`;
    document.getElementById('walletSolAddr').textContent = d.solana.address;
    document.getElementById('walletSolNet').textContent = d.solana.network;
    document.getElementById('walletPeaqAmount').textContent = `${d.peaq.balance ?? '—'} ${d.peaq.symbol}`;
    document.getElementById('walletPeaqAddr').textContent = d.peaq.address;
    document.getElementById('walletPeaqNet').textContent = d.peaq.network;
    document.getElementById('walletNftId').textContent = d.nft.machine_id;
    document.getElementById('walletNftNet').textContent = d.nft.network;
  } catch (e) {}
}

async function pollOrder() {
  try {
    const r = await fetch('/order_status');
    const d = await r.json();
    const el = document.getElementById('orderStatus');
    if (d.status === 'no_order') { el.textContent = '— keine aktive Bestellung —'; return; }
    const cls = d.status === 'delivered' ? 'status-delivered' : 'status-pending';
    el.innerHTML = `<span class="${cls}">${d.status}</span><br>Käufer: <span class="mono">${d.buyer_pubkey}</span>`;
  } catch (e) {}
}

// Mirrors Python's html.escape() (used server-side in gallery() above) -
// tx.sig and tx.escrow_tx below come from /transactions, which is populated
// from order_state.json, which buyer_app.py's /delivered endpoint writes to
// without HTTP auth (the car itself has no concept of it) - so a value that
// isn't a well-formed Solana signature could otherwise inject markup/script
// into this authenticated operator page via the tbody.innerHTML assignment
// in pollTx() below (reflected/stored XSS via innerHTML).
function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

async function pollTx() {
  try {
    const r = await fetch('/transactions');
    const d = await r.json();
    const tbody = document.querySelector('#txTable tbody');
    tbody.innerHTML = d.map(tx => {
      const safeEscrowTx = escapeHtml(tx.escrow_tx);
      const galleryLink = tx.escrow_tx
        ? ` &middot; <a href="gallery/${safeEscrowTx}">Sensordaten</a>` : '';
      return `<tr><td>Escrow-Release</td><td><a href="https://explorer.solana.com/tx/${escapeHtml(tx.sig)}?cluster=devnet" target="_blank">${escapeHtml(tx.sig.slice(0,12))}...</a>${galleryLink}</td></tr>`;
    }).join('') || '<tr><td>— noch keine Auszahlungen —</td></tr>';
  } catch (e) {}
}

async function drive(speed, angle) {
  try {
    await fetch('/proxy/drive', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ speed, angle })
    });
  } catch (e) {}
}
async function driveStop() {
  try { await fetch('/proxy/stop', { method: 'POST' }); } catch (e) {}
}
let camPan = 20, camTilt = 0;
async function setCam(pan, tilt) {
  camPan = pan; camTilt = tilt;
  try {
    await fetch('/proxy/camera/angle', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ pan: camPan, tilt: camTilt })
    });
  } catch (e) {}
}
function nudgeCam(dPan, dTilt) { setCam(camPan + dPan, camTilt + dTilt); }

document.getElementById('carFeed').innerHTML = `
  <div class="car-row">
    <img class="video" src="/proxy/mjpg">
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
      fetch('/proxy/ultrasonic').then(r => r.json()),
      fetch('/proxy/camera/qr').then(r => r.json()),
    ]);
    document.getElementById('distStat').textContent = `${d.distance_cm} cm`;
    document.getElementById('qrStat').textContent = q.qr || '— kein QR erkannt —';
  } catch (e) {}
}, 1000);

pollWallet(); pollOperatorWallets(); pollCrosschainWallet(); pollOrder(); pollTx();
setInterval(pollWallet, 5000);
setInterval(pollOperatorWallets, 5000);
setInterval(pollCrosschainWallet, 10000);
setInterval(pollOrder, 2000);
setInterval(pollTx, 5000);
</script>
</body>
</html>"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True, debug=False, use_reloader=False)
