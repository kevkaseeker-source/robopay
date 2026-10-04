#!/usr/bin/env python3
import argparse
import json
import logging
import sys
import time

import requests

import config as cfg
from pixhawk_bridge import HATGPSBridge, haversine_distance
from solana_client import SolanaClient

PC_SERVER_URL = "https://starless-morality-cranium.ngrok-free.dev"
POLL_INTERVAL_S = 5.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("main")

DEVNET_EXPLORER = "https://explorer.solana.com/tx/{}?cluster=devnet"


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


def fetch_order():
    """Poll PC server for active order. Returns (lat, lon) or None."""
    try:
        r = requests.get(f"{PC_SERVER_URL}/active_order", timeout=10)
        if r.status_code == 204:
            return None
        data = r.json()
        if data.get("status") == "no_order":
            return None
        return data["lat"], data["lon"]
    except Exception as e:
        log.warning("Could not reach PC server: %s", e)
        return None


def send_log(msg: str):
    try:
        requests.post(f"{PC_SERVER_URL}/rpi_log", json={"msg": msg}, timeout=3)
    except:
        pass


def check_force_delivery():
    try:
        r = requests.get(f"{PC_SERVER_URL}/force_delivery", timeout=5)
        return r.json().get("force", False)
    except:
        return False


def report_delivered(lat, lon, tx_sig):
    try:
        requests.post(
            f"{PC_SERVER_URL}/delivered",
            json={"delivery_tx": tx_sig, "lat": lat, "lon": lon},
            timeout=10,
        )
    except Exception as e:
        log.warning("Could not report delivery to PC: %s", e)


def main():
    args = parse_args()

    gps = HATGPSBridge(port=cfg.HAT_GPS_PORT)
    solana = None if args.dry_run else SolanaClient(
        cfg.SOLANA_RPC_URL, cfg.WALLET_KEYPAIR_PATH, cfg.DRONE_PROGRAM_ID
    )

    try:
        gps.connect()
        send_log("HAT GPS verbunden")

        log.info("Waiting for GPS fix (max 10 min)...")
        send_log("Warte auf GPS Fix (max 10 Min)...")
        try:
            gps.wait_for_gps(timeout=600.0)
            send_log("GPS Fix erhalten!")
        except TimeoutError:
            log.warning("GPS fix timeout — continuing without fix (demo mode possible)")
            send_log("Kein GPS Fix — weiter ohne (Demo-Button verfügbar)")

        log.info("Waiting for order from PC server (%s) ...", PC_SERVER_URL)
        send_log("Warte auf Order vom Server...")
        target = None
        while target is None:
            target = fetch_order()
            if target is None:
                log.info("No active order yet, retrying in %.0fs ...", POLL_INTERVAL_S)
                time.sleep(POLL_INTERVAL_S)

        lat, lon = target
        log.info("Order received: target lat=%.6f lon=%.6f (radius %.1fm)", lat, lon, cfg.ARRIVAL_RADIUS_M)
        send_log(f"Order empfangen! Ziel: lat={lat:.6f} lon={lon:.6f}")

        log.info("Monitoring position ...")
        send_log("GPS-Monitoring gestartet...")
        while True:
            if check_force_delivery():
                log.info("Force delivery triggered from server!")
                send_log("Demo-Button gedrückt — sende TX...")
                if args.dry_run:
                    log.info("[dry-run] Would send confirm_delivery TX here.")
                    report_delivered(lat, lon, "dry-run-tx")
                    break
                try:
                    sig = solana.confirm_delivery(lat, lon)
                    log.info("Explorer: %s", DEVNET_EXPLORER.format(sig))
                    send_log(f"TX bestätigt! {sig[:20]}...")
                    report_delivered(lat, lon, sig)
                except RuntimeError as e:
                    log.error("TX fehlgeschlagen (force): %s", e)
                    send_log(f"TX FEHLER: {e}")
                break

            fix = gps.get_gps()
            if fix is None:
                time.sleep(cfg.TELEMETRY_POLL_INTERVAL)
                continue

            dist = haversine_distance(fix.lat, fix.lon, lat, lon)
            log.info("lat=%.6f lon=%.6f alt=%.1fm dist=%.1fm", fix.lat, fix.lon, fix.alt_rel, dist)
            send_log(f"lat={fix.lat:.6f} lon={fix.lon:.6f} — Abstand: {dist:.1f}m")

            if dist <= cfg.ARRIVAL_RADIUS_M:
                log.info("ARRIVED at target!")
                send_log(f"ANGEKOMMEN! ({dist:.1f}m) — sende Proof of Delivery TX...")
                if args.dry_run:
                    log.info("[dry-run] Would send confirm_delivery TX here.")
                    report_delivered(fix.lat, fix.lon, "dry-run-tx")
                    break
                try:
                    sig = solana.confirm_delivery(fix.lat, fix.lon, fix.timestamp)
                    log.info("Explorer: %s", DEVNET_EXPLORER.format(sig))
                    send_log(f"TX bestätigt! {sig[:20]}...")
                    report_delivered(fix.lat, fix.lon, sig)
                except RuntimeError as e:
                    log.error("TX fehlgeschlagen (GPS): %s", e)
                    send_log(f"TX FEHLER: {e}")
                break

            time.sleep(cfg.TELEMETRY_POLL_INTERVAL)

    except KeyboardInterrupt:
        log.info("Stopped.")
    except Exception as e:
        log.error("Error: %s", e, exc_info=True)
        sys.exit(1)
    finally:
        gps.disconnect()


if __name__ == "__main__":
    main()
