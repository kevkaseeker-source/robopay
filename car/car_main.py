#!/usr/bin/env python3
"""Delivery-confirmation trigger loop for Unit C (PiCar-X) — runs on the RPi.

Same escrow settlement as the drone (Unit B, see ../rpi/main.py), but arrival
is detected differently: instead of a GPS geofence, Unit C reads a QR code
(via its camera) and an ultrasonic distance to decide it has reached the
delivery box ("Wuerfel"). The QR code is a FIXED, static marker on the box
itself (BOX_QR_CODE below) — the same one every time, not generated per
order (a phone just showing it is enough for the PoC; no physical cube
needed). It only proves "the car found the box" — which order is being
fulfilled is tracked separately via the single active order from the PC
server, so the QR content doesn't need to carry the order/escrow identity.
This also sidesteps a real camera limitation found in testing: dense QR
codes (e.g. one encoding a long URL) reliably failed to decode on this
5MP fixed-focus camera at any distance/lighting tried, while a short QR
decoded fine on the first try — so BOX_QR_CODE must stay short.

The on-chain program does not verify GPS truth
independently either way (see anchor/programs/drone-delivery/src/lib.rs) —
it only checks that the lat/lon we submit match the order's target lat/lon
within tolerance. So this loop simply re-submits the order's own lat/lon
once the physical QR+distance check passes; see paper chapter 12 (Security
Analysis) for the known "RPi as sole signer of truth" trust limitation this
carries over from the drone design.

Talks to two HTTP services:
  - picar_server.py, on this same RPi (http://localhost:8080)
        GET /camera/qr    -> {"qr": "<string or null>"}
        GET /ultrasonic   -> {"distance_cm": <float>}
  - order_server_pc.py, on the PC server (PC_SERVER_URL)
        GET  /active_order    -> current order (lat, lon, escrow_tx, ...)
        GET  /force_delivery  -> demo-button override
        POST /delivered       -> report a completed delivery
        POST /rpi_log         -> live log line for the PC-side UI

Run manually (like the indoor PoC was tested before automating it):
    ~/robopay-research-group/venv/bin/python3 car_main.py --demo
    ~/robopay-research-group/venv/bin/python3 car_main.py --demo --dry-run

--demo bypasses the PC server and uses a fixed local order (see DEMO_ORDER
below) — useful for testing the QR/ultrasonic trigger logic on its own,
since the phone-ordering flow for Unit C isn't built yet (only the drone's
order_server_pc.py exists so far).

Env vars (same names as ../rpi/config.py, since it's the same SolanaClient):
    WALLET_KEYPAIR_PATH   Unit C's own operator keypair (signs confirm_delivery)
                           default here: ~/.config/solana/picarx_operator.json
    SELLER_PUBKEY          who receives the SOL — referred to as "PiCarOwner"
                           in all logging/docs for Unit C, same field on-chain
    SOLANA_RPC_URL, DRONE_PROGRAM_ID  same contract, reused from the drone
"""

import argparse
import logging
import os
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "rpi"))
import config as cfg  # noqa: E402  (path insert must happen first)
from solana_client import SolanaClient  # noqa: E402
import payout_policy  # noqa: E402

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
PICAR_SERVER_URL = os.getenv("PICAR_SERVER_URL", "http://localhost:8080")
PC_SERVER_URL = os.getenv("PC_SERVER_URL", "https://starless-morality-cranium.ngrok-free.dev")

# Fixed marker printed/displayed on the delivery box - same every time, kept
# short because dense QR codes don't decode reliably on this camera (see
# module docstring).
BOX_QR_CODE = os.getenv("BOX_QR_CODE", "ROBOPAY-BOX-C")

REQUIRE_DISTANCE = os.getenv("REQUIRE_DISTANCE", "true").lower() not in ("false", "0", "no")
DIST_MIN_CM = float(os.getenv("DIST_MIN_CM", "15"))
DIST_MAX_CM = float(os.getenv("DIST_MAX_CM", "25"))

POLL_INTERVAL_S = float(os.getenv("TRIGGER_POLL_INTERVAL", "1.0"))
ORDER_POLL_INTERVAL_S = 5.0

DEMO_ORDER = {
    "lat": cfg.TARGET_LAT,
    "lon": cfg.TARGET_LON,
    "escrow_tx": os.getenv("DEMO_ORDER_ID", "demo-order-unit-c"),
}

DEVNET_EXPLORER = "https://explorer.solana.com/tx/{}?cluster=devnet"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("car_main")


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="skip the real confirm_delivery TX")
    p.add_argument("--demo", action="store_true", help="use DEMO_ORDER instead of polling the PC server")
    return p.parse_args()


# ---------------------------------------------------------------------------
# picar_server.py (local sensors)
# ---------------------------------------------------------------------------
def get_qr():
    try:
        r = requests.get(f"{PICAR_SERVER_URL}/camera/qr", timeout=3)
        return r.json().get("qr")
    except Exception as e:
        log.warning("Could not reach picar_server /camera/qr: %s", e)
        return None


def get_distance_cm():
    try:
        r = requests.get(f"{PICAR_SERVER_URL}/ultrasonic", timeout=3)
        return r.json().get("distance_cm")
    except Exception as e:
        log.warning("Could not reach picar_server /ultrasonic: %s", e)
        return None


# ---------------------------------------------------------------------------
# order_server_pc.py (PC server)
# ---------------------------------------------------------------------------
def fetch_order():
    """Poll PC server for active order. Returns dict with lat/lon/escrow_tx or None."""
    try:
        r = requests.get(f"{PC_SERVER_URL}/active_order", timeout=10, headers=cfg.MACHINE_HEADERS)
        if r.status_code == 204:
            return None
        if r.status_code != 200:
            log.warning("Buyer app answered HTTP %d to /active_order (wrong MACHINE_TOKEN?)", r.status_code)
            return None
        data = r.json()
        if data.get("status") == "no_order":
            return None
        # Buyer app now lets the buyer pick "fixed" (this script) or "agent"
        # (delivery_agent.py, see docs/superpowers/specs/2026-09-17-delivery-
        # agent-design.md) per order - both poll the same /active_order, so
        # this script must ignore anything not meant for it and keep waiting,
        # exactly like "no order yet". Missing field = pre-toggle order = ours.
        if data.get("trigger_mode", "fixed") != "fixed":
            return None
        return data
    except Exception as e:
        log.warning("Could not reach PC server: %s", e)
        return None


def send_log(msg: str):
    try:
        requests.post(f"{PC_SERVER_URL}/rpi_log", json={"msg": msg}, timeout=3, headers=cfg.MACHINE_HEADERS)
    except Exception:
        pass


def check_force_delivery():
    try:
        r = requests.get(f"{PC_SERVER_URL}/force_delivery", timeout=5, headers=cfg.MACHINE_HEADERS)
        return r.json().get("force", False)
    except Exception:
        return False


def report_delivered(lat, lon, tx_sig):
    try:
        requests.post(
            f"{PC_SERVER_URL}/delivered",
            json={"delivery_tx": tx_sig, "lat": lat, "lon": lon},
            timeout=10,
            headers=cfg.MACHINE_HEADERS,
        )
    except Exception as e:
        log.warning("Could not report delivery to PC: %s", e)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    args = parse_args()

    solana = None if args.dry_run else SolanaClient(
        cfg.SOLANA_RPC_URL, cfg.WALLET_KEYPAIR_PATH, cfg.DRONE_PROGRAM_ID
    )
    check_payout_config()
    if solana:
        log.info("Payout mode: %s (machine %s)", cfg.PAYOUT_MODE, cfg.MACHINE_ID)
    if not cfg.MACHINE_TOKEN:
        log.warning("MACHINE_TOKEN is not set - the buyer app will reject this car's "
                    "polls with HTTP 401 and no order will ever arrive.")

    escrow = None  # demo mode: _confirm() runs the preflight itself before paying
    if args.demo:
        order = DEMO_ORDER
        log.info("Demo mode: using fixed local order (no PC server)")
    else:
        log.info("Waiting for order from PC server (%s) ...", PC_SERVER_URL)
        send_log("Warte auf Order vom Server...")
        order, escrow, rejected = None, None, set()
        while order is None:
            order = fetch_order()
            if order is not None and order.get("escrow_tx") in rejected:
                order = None  # already refused this one; wait for the buyer to refund / reorder
            elif order is not None and solana is not None:
                try:
                    escrow = preflight(solana)
                except payout_policy.PayoutRefused as e:
                    log.error("Refusing order %s: %s", order.get("escrow_tx"), e)
                    send_log(f"Order abgelehnt: {e}")
                    rejected.add(order.get("escrow_tx"))
                    order = None
            if order is None:
                log.info("No active order yet, retrying in %.0fs ...", ORDER_POLL_INTERVAL_S)
                time.sleep(ORDER_POLL_INTERVAL_S)

    lat, lon = order["lat"], order["lon"]
    order_id = order.get("escrow_tx") or DEMO_ORDER["escrow_tx"]  # for logging/reporting only, not matched against the QR
    log.info("Order received: order_id=%s lat=%.6f lon=%.6f", order_id, lat, lon)
    if REQUIRE_DISTANCE:
        log.info("Waiting for box QR ('%s') + distance %.0f-%.0fcm ...", BOX_QR_CODE, DIST_MIN_CM, DIST_MAX_CM)
        send_log(f"Order empfangen! Warte auf Box-QR + Abstand ({DIST_MIN_CM:.0f}-{DIST_MAX_CM:.0f}cm)...")
    else:
        log.info("Waiting for box QR ('%s') - distance check disabled (REQUIRE_DISTANCE=false) ...", BOX_QR_CODE)
        send_log("Order empfangen! Warte auf Box-QR (Abstandspruefung deaktiviert)...")

    try:
        while True:
            if check_force_delivery():
                log.info("Force delivery triggered from server!")
                send_log("Demo-Button gedrueckt - sende TX...")
                _confirm(solana, args.dry_run, lat, lon, order_id, escrow)
                break

            qr = get_qr()
            dist = get_distance_cm()
            log.info("qr=%s  distance=%s cm", qr, dist)

            qr_match = qr == BOX_QR_CODE
            dist_match = (not REQUIRE_DISTANCE) or (dist is not None and DIST_MIN_CM <= dist <= DIST_MAX_CM)

            if qr_match and dist_match:
                log.info("ARRIVED at delivery box! (qr matched, distance=%s)", dist)
                dist_str = f"{dist:.1f}cm" if dist is not None else "n/a"
                send_log(f"ANGEKOMMEN! QR erkannt, Abstand {dist_str} - sende Proof of Delivery TX...")
                _confirm(solana, args.dry_run, lat, lon, order_id, escrow)
                break

            time.sleep(POLL_INTERVAL_S)

    except KeyboardInterrupt:
        log.info("Stopped.")


def check_payout_config():
    """Refuse to start with a payout setup that could pay the wrong wallet:
    DID mode needs MACHINE_ID, and the fixed-wallet mode must be chosen
    explicitly (PAYOUT_MODE=static) - no silent fallback."""
    if cfg.PAYOUT_MODE not in ("did", "static"):
        log.error("PAYOUT_MODE=%r is invalid (use 'did' or 'static') - not starting", cfg.PAYOUT_MODE)
        raise SystemExit(2)
    if cfg.PAYOUT_MODE == "did" and cfg.MACHINE_ID is None:
        log.error("PAYOUT_MODE=did but MACHINE_ID is not set - not starting "
                  "(set MACHINE_ID, or PAYOUT_MODE=static to pay SELLER_PUBKEY)")
        raise SystemExit(2)
    if cfg.PAYOUT_MODE == "static":
        log.warning("PAYOUT_MODE=static: paying the fixed SELLER_PUBKEY %s, not the Machine-NFT owner",
                    cfg.SELLER_PUBKEY)


def resolve_owner():
    """Solana wallet of the car's current owner (raises PayoutRefused)."""
    return payout_policy.resolve_payout_owner(cfg.PAYOUT_MODE, cfg.MACHINE_ID, cfg.PEAQ_RPC_URL,
                                              cfg.SELLER_PUBKEY)


def preflight(solana):
    """Before driving: the order's escrow must be live and pay the car's
    current owner. Returns the escrow; raises PayoutRefused otherwise."""
    escrow = solana.read_escrow()
    payout_policy.check_order(escrow, resolve_owner(), time.time())
    return escrow


def _confirm(solana, dry_run, lat, lon, order_id, escrow=None):
    if dry_run:
        log.info("[dry-run] Would send confirm_delivery TX here.")
        report_delivered(lat, lon, "dry-run-tx")
        return

    # Kevin's rule: ownership must not change during a delivery. Pay the
    # escrow's own seller (the only wallet the program accepts) - but only
    # if that is still the car's owner right now. If the NFT moved, nobody
    # is paid and the escrow stays PENDING so the buyer can reclaim it.
    try:
        if escrow is None:
            escrow = preflight(solana)
        seller = payout_policy.check_payout(escrow, resolve_owner())
        log.info("Paying the car's owner (escrow seller, confirmed via %s): %s", cfg.PAYOUT_MODE, seller)
    except payout_policy.PayoutRefused as e:
        log.error("Not paying: %s", e)
        send_log(f"Keine Auszahlung: {e}. Besitzer muss waehrend der Lieferung gleich bleiben - "
                 f"der Kaeufer kann nach Fristablauf zurueckbuchen.")
        return

    try:
        sig = solana.confirm_delivery(lat, lon, seller=seller)
        log.info("Explorer: %s", DEVNET_EXPLORER.format(sig))
        send_log(f"TX bestaetigt! {sig[:20]}...")
        report_delivered(lat, lon, sig)
    except RuntimeError as e:
        log.error("TX fehlgeschlagen: %s", e)
        send_log(f"TX FEHLER: {e}")
        return

    # The escrow PDA is one-per-operator and stays allocated (status=DELIVERED)
    # until explicitly closed - without this, the next create_delivery fails
    # with "already in use" (hit this 2026-09-17 during live testing: had to
    # close it by hand twice before realizing it needs to happen every time).
    try:
        close_sig = solana.close_escrow()
        log.info("Escrow closed, ready for next order: %s", close_sig)
    except Exception as e:
        log.warning("close_escrow failed (payment already succeeded, just cleanup): %s", e)


if __name__ == "__main__":
    main()
