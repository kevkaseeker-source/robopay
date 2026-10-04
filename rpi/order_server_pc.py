#!/usr/bin/env python3
"""
PC-side Flask server. Runs on Windows, receives orders from browser,
serves /active_order for RPi4 to poll via 4G/pppd.

Start:
  python order_server_pc.py
  ngrok http --domain=starless-morality-cranium.ngrok-free.dev 5000
"""
import json
import logging
import os
from flask import Flask, jsonify, request, send_from_directory
import config as cfg

INTERNAL_TOKEN = os.getenv("INTERNAL_TOKEN", "robopay-dev-2026")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("order_server_pc")

app = Flask(__name__)

_active_order = None  # dict or None
_force_delivery = False
_rpi_logs = []  # last 100 log lines from RPi4


@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/config")
def get_config():
    return jsonify({
        "seller_pubkey": cfg.SELLER_PUBKEY,
        "drone_operator_pubkey": cfg.DRONE_OPERATOR_PUBKEY,
        "program_id": cfg.DRONE_PROGRAM_ID,
        "delivery_amount_sol": cfg.DELIVERY_AMOUNT_SOL,
        "deadline_minutes": cfg.DEADLINE_MINUTES,
        "rpc_url": cfg.SOLANA_RPC_URL,
    })


@app.route("/order", methods=["POST"])
def place_order():
    global _active_order
    data = request.get_json()
    if not data or "lat" not in data or "lon" not in data:
        return jsonify({"success": False, "error": "Missing lat/lon"}), 400

    lat = float(data["lat"])
    lon = float(data["lon"])
    buyer_pubkey = data.get("buyer_pubkey", "unknown")
    escrow_tx = data.get("escrow_tx")

    log.info("Order received: lat=%.6f lon=%.6f buyer=%s tx=%s", lat, lon, buyer_pubkey, escrow_tx)

    _active_order = {
        "lat": lat,
        "lon": lon,
        "buyer_pubkey": buyer_pubkey,
        "escrow_tx": escrow_tx,
        "status": "drone_dispatched",
        "delivery_tx": None,
    }
    global _force_delivery
    _force_delivery = False
    return jsonify({"success": True, "tx": escrow_tx, "lat": lat, "lon": lon})


@app.route("/active_order")
def active_order():
    """RPi4 polls this endpoint to get current target coordinates."""
    if _active_order is None:
        return jsonify({"status": "no_order"}), 204
    return jsonify(_active_order)


@app.route("/delivered", methods=["POST"])
def delivered():
    """RPi4 calls this when confirm_delivery TX is confirmed."""
    global _active_order, _force_delivery
    data = request.get_json() or {}
    delivery_tx = data.get("delivery_tx")
    lat = data.get("lat")
    lon = data.get("lon")
    log.info("DELIVERED! tx=%s lat=%s lon=%s", delivery_tx, lat, lon)
    if _active_order:
        _active_order["status"] = "delivered"
        _active_order["delivery_tx"] = delivery_tx
    _force_delivery = False
    return jsonify({"success": True})


@app.route("/force_delivery")
def force_delivery_status():
    return jsonify({"force": _force_delivery})


@app.route("/simulate_arrival", methods=["POST"])
def simulate_arrival():
    global _force_delivery, _active_order
    if request.headers.get("X-Token") != INTERNAL_TOKEN:
        log.warning("simulate_arrival: unauthorized attempt from %s", request.remote_addr)
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json() or {}
    lat = data.get("lat")
    lon = data.get("lon")
    if lat and lon and _active_order is None:
        _active_order = {
            "lat": lat, "lon": lon,
            "buyer_pubkey": "restored",
            "escrow_tx": "restored",
            "status": "drone_dispatched",
            "delivery_tx": None,
        }
        log.info("Order restored via simulate_arrival: lat=%.6f lon=%.6f", lat, lon)
    _force_delivery = True
    log.info("Simulate arrival triggered")
    return jsonify({"success": True})


@app.route("/status")
def status():
    if _active_order:
        return jsonify(_active_order)
    return jsonify({"status": "no_order"})


@app.route("/rpi_log", methods=["POST"])
def rpi_log():
    global _rpi_logs
    data = request.get_json() or {}
    msg = data.get("msg", "").strip()
    if msg:
        _rpi_logs.append(msg)
        if len(_rpi_logs) > 100:
            _rpi_logs.pop(0)
    return jsonify({"ok": True})


@app.route("/rpi_logs")
def get_rpi_logs():
    return jsonify({"logs": _rpi_logs})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
