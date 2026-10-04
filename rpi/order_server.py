#!/usr/bin/env python3
import json
import logging
import subprocess
import sys
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from solders.keypair import Keypair

import config as cfg

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("order_server")

app = Flask(__name__)
ORDER_FILE = Path("/home/kevkaakvek/current_order.json")
VENV_PYTHON = "/home/kevkaakvek/drone-env/bin/python3"
MAIN_SCRIPT = "/home/kevkaakvek/main.py"
_drone_process = None

# Read drone_operator pubkey from keypair file at startup
_kp_data = json.loads(Path(cfg.WALLET_KEYPAIR_PATH).read_text())
DRONE_OPERATOR_PUBKEY = str(Keypair.from_bytes(bytes(_kp_data)).pubkey())


@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/config")
def get_config():
    """Browser fetches this to know how to build the escrow TX."""
    return jsonify({
        "seller_pubkey": cfg.SELLER_PUBKEY,
        "drone_operator_pubkey": DRONE_OPERATOR_PUBKEY,
        "program_id": cfg.DRONE_PROGRAM_ID,
        "delivery_amount_sol": cfg.DELIVERY_AMOUNT_SOL,
        "deadline_minutes": cfg.DEADLINE_MINUTES,
        "rpc_url": cfg.SOLANA_RPC_URL,
    })


@app.route("/order", methods=["POST"])
def place_order():
    data = request.get_json()
    if not data or "lat" not in data or "lon" not in data:
        return jsonify({"success": False, "error": "Missing lat/lon"}), 400

    lat = float(data["lat"])
    lon = float(data["lon"])
    buyer_pubkey = data.get("buyer_pubkey", "unknown")
    escrow_tx = data.get("escrow_tx")

    log.info("Order received: lat=%.6f lon=%.6f buyer=%s tx=%s", lat, lon, buyer_pubkey, escrow_tx)

    order = {
        "lat": lat,
        "lon": lon,
        "buyer_pubkey": buyer_pubkey,
        "escrow_tx": escrow_tx,
        "status": "drone_dispatched",
        "delivery_tx": None,
    }
    ORDER_FILE.write_text(json.dumps(order))
    _start_drone()
    return jsonify({"success": True, "tx": escrow_tx, "lat": lat, "lon": lon})


def _start_drone():
    global _drone_process
    if _drone_process and _drone_process.poll() is None:
        log.info("Drone process already running")
        return
    log.info("Starting main.py ...")
    _drone_process = subprocess.Popen(
        [VENV_PYTHON, MAIN_SCRIPT],
        stdout=sys.stdout, stderr=sys.stderr
    )


@app.route("/status")
def status():
    if ORDER_FILE.exists():
        return jsonify(json.loads(ORDER_FILE.read_text()))
    return jsonify({"status": "no_order"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
