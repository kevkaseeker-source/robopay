#!/usr/bin/env python3
"""Shared config/helpers for buyer_app.py and seller_app.py — the two apps
split out of the original order_app_pc.py so the buyer and the car
owner/seller each get their own app with their own login, matching their
actual separate roles (see SETUP_AND_ARCHITECTURE.md).
"""

import functools
import hashlib
import json
import os
import secrets
import struct
import time
from pathlib import Path

import requests
from flask import Response, request
from solana.rpc.api import Client
from solders.pubkey import Pubkey

# ---------------------------------------------------------------------------
# Config (shared)
# ---------------------------------------------------------------------------
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.devnet.solana.com")
PROGRAM_ID = os.getenv("DRONE_PROGRAM_ID", "3NmsWVX39uvzG3PBNPdSe4FTgudqSeLphJSbMDhV5F8Y")
OPERATOR_PUBKEY = os.getenv("OPERATOR_PUBKEY", "7VizNvqBSnHnP8ySnsjxxyUnBQCybVnJHBDRyvaThXia")
SELLER_PUBKEY = os.getenv("SELLER_PUBKEY", "7uoFeSG546UvK5HYyA97GVmJUTvrXWgGgkTgxspH4d1C")
# AI delivery-confirmation agent's own operator wallet (separate from the
# RPi's fixed-QR OPERATOR_PUBKEY above) - see
# docs/superpowers/specs/2026-09-17-delivery-agent-design.md. Wallet only
# exists for now (visibility in buyer/seller apps); the agent itself isn't
# built yet.
AGENT_OPERATOR_PUBKEY = os.getenv("AGENT_OPERATOR_PUBKEY", "FCSTjYn6tKKVFCdaaA7khkiQGrQhAF7n2staJQ8bWnhA")

# peaq-chain identity for the cross-chain wallet panel (read-only - see
# rpi/peaq_ownership.py for the full ownership-resolution logic this app
# deliberately does NOT replicate; this is just balance/address display).
PEAQ_RPC_URL = os.getenv("PEAQ_RPC_URL", "https://peaq.api.onfinality.io/public")
PEAQ_OPERATOR_ADDRESS = os.getenv("PEAQ_OPERATOR_ADDRESS", "0x4d99BeAD5A4CCE20a7F93CB2CF62f1847263Ea8f")
PEAQ_MACHINE_ID = os.getenv(
    "PEAQ_MACHINE_ID",
    "5149596011477982620423871887556457753159696362422273317835964893685329625592",
)

DELIVERY_AMOUNT_SOL = float(os.getenv("DELIVERY_AMOUNT_SOL", "0.20"))
DEADLINE_MINUTES = int(os.getenv("DEADLINE_MINUTES", "60"))
TARGET_LAT = float(os.getenv("TARGET_LAT", "52.3609"))
TARGET_LON = float(os.getenv("TARGET_LON", "14.0600"))

BOX_QR_CODE = os.getenv("BOX_QR_CODE", "ROBOPAY-BOX-C")
PICAR_SERVER_URL = os.getenv("PICAR_SERVER_URL", "")
# vilib's video stream (port 9000) is a SEPARATE process from picar_server.py
# (port 8080) - each needed its own MCC tunnel, and each tunnel gets its own
# DNS name (set via --name at creation), so these are two different
# hostnames even though they're the same physical device. Defaults to
# PICAR_VIDEO_URL if set, else falls back to PICAR_SERVER_URL (correct for
# same-LAN testing, where both ports are on the one reachable IP).
PICAR_VIDEO_URL = os.getenv("PICAR_VIDEO_URL", PICAR_SERVER_URL)

DEVNET_EXPLORER = "https://explorer.solana.com/tx/{}?cluster=devnet"

STATE_FILE = Path(__file__).parent / "order_state.json"

TX_LABELS = {
    "create_delivery": "Buyer TX (Escrow-Einzahlung)",
    "confirm_delivery": "Escrow-Release TX (Auszahlung)",
    "cancel_delivery": "Cancel TX (Rueckerstattung)",
}

rpc = Client(SOLANA_RPC_URL)
program_id = Pubkey.from_string(PROGRAM_ID)
operator_pubkey = Pubkey.from_string(OPERATOR_PUBKEY)
agent_operator_pubkey = Pubkey.from_string(AGENT_OPERATOR_PUBKEY)


def disc(name: str) -> bytes:
    return hashlib.sha256(f"global:{name}".encode()).digest()[:8]


def derive_escrow_pda(operator: Pubkey = None) -> Pubkey:
    """The escrow PDA is per-operator (seeds include the operator pubkey) -
    defaults to the fixed-QR operator for backward compatibility, but pass
    agent_operator_pubkey to get the AI agent's separate escrow slot."""
    op = operator or operator_pubkey
    pda, _ = Pubkey.find_program_address([b"escrow", bytes(op)], program_id)
    return pda


def get_balance_sol(pubkey_str: str):
    try:
        bal = rpc.get_balance(Pubkey.from_string(pubkey_str))
        return round(bal.value / 1e9, 4)
    except Exception:
        return None


def get_balance_peaq(address: str):
    """Read-only native PEAQ balance via a plain eth_getBalance JSON-RPC
    call - deliberately NOT using peaq_os_sdk here (that's a much heavier
    dependency, only actually needed for the ownership-resolution logic in
    rpi/peaq_ownership.py). PEAQ, like any EVM native token, uses 18
    decimals."""
    try:
        resp = requests.post(
            PEAQ_RPC_URL,
            json={"jsonrpc": "2.0", "id": 1, "method": "eth_getBalance", "params": [address, "latest"]},
            timeout=5,
        )
        resp.raise_for_status()
        result = resp.json().get("result")
        if result is None:
            return None
        return round(int(result, 16) / 1e18, 4)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Shared state file (order_state.json) - both apps read it; only buyer_app
# writes to active_order/tx_history for create/cancel, car_main.py's
# /delivered call (proxied through buyer_app) appends confirm_delivery.
# ---------------------------------------------------------------------------
def load_state():
    if STATE_FILE.exists():
        d = json.loads(STATE_FILE.read_text())
        return d.get("active_order"), d.get("tx_history", [])
    return None, []


def save_state(active_order, tx_history):
    STATE_FILE.write_text(json.dumps({"active_order": active_order, "tx_history": tx_history}))


# ---------------------------------------------------------------------------
# HTTP Basic Auth - username/password set via env vars, exempts the
# machine-to-machine endpoints car_main.py calls directly (it has no
# concept of HTTP auth, and these aren't sensitive on their own - they
# only expose/accept order status, not wallet actions).
# ---------------------------------------------------------------------------
MACHINE_TOKEN_HEADER = "X-RoboPay-Machine-Token"


def require_machine_token(app, paths, token_env="MACHINE_TOKEN"):
    """Protect the machine-to-machine endpoints (the ones make_auth exempts
    because the car can't do a browser login) with a shared secret header.

    Without this, anyone who reads the public repo could POST a fake
    /delivered or /register_external_order and disrupt a live demo. They
    could never move funds - the escrow program checks every release on
    chain - but they could block the order queue or fake the payout history.
    Fails closed like make_auth: the app refuses to start without a token.
    """
    token = os.environ.get(token_env)
    if not token:
        raise RuntimeError(f"Set {token_env} before starting this app (shared with the car).")

    @app.before_request
    def _check_machine_token():
        if request.path not in paths:
            return None
        sent = request.headers.get(MACHINE_TOKEN_HEADER, "")
        if not secrets.compare_digest(sent, token):
            return Response('{"error": "machine token required"}', 401, {"Content-Type": "application/json"})
        return None


def make_auth(app, username_env, password_env, exempt_paths=()):
    username = os.environ.get(username_env)
    password = os.environ.get(password_env)
    if not username or not password:
        raise RuntimeError(f"Set {username_env} and {password_env} before starting this app.")

    @app.before_request
    def _check_auth():
        if request.path in exempt_paths:
            return None
        auth = request.authorization
        ok = auth and secrets.compare_digest(auth.username, username) and secrets.compare_digest(auth.password, password)
        if not ok:
            return Response(
                "Login erforderlich", 401,
                {"WWW-Authenticate": 'Basic realm="RoboPay"'},
            )
        return None
