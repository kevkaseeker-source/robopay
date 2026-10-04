#!/usr/bin/env python3
"""Periodic sensor-data capture during an active delivery, for Unit C.

Runs as a background thread inside seller_app.py (the StaexHosting side) -
never on car_main.py or anything on the RPi. Every SENSOR_LOG_INTERVAL_S
seconds, while a delivery is active, fetches a camera frame and the
ultrasonic reading from the RPi over the same mesh-proxy pattern
seller_app.py's existing /proxy/* routes already use, and saves both
locally under sensor_data/<escrow_tx>/.

See docs/superpowers/specs/2026-09-23-sensor-data-collection-design.md.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from pathlib import Path

import requests

import robopay_common as common

_SENSOR_LOG_INTERVAL_DEFAULT = 11.0
_SENSOR_LOG_INTERVAL_MAX = 3600.0  # 1 hour - generous for any real config, low
                                    # enough time.sleep() never gets an absurd value.

_MAX_CAPTURES_PER_DELIVERY_DEFAULT = 500


def _parse_interval_s(raw: str) -> float:
    """Parse SENSOR_LOG_INTERVAL_S defensively - this runs at import time, so
    a bad value here must never raise (a ValueError here would take down
    seller_app.py's whole import, i.e. every route including /proxy/drive,
    not just this feature). Falls back to the default on anything
    unparseable (including inf/nan, which parse fine via float() but are not
    usable as a sleep interval), and clamps to a sane [1.0, 3600.0] range so
    a non-positive value can't reach time.sleep() (which raises on negative)
    or spin _loop() in a tight unthrottled loop (0), and an absurdly large
    value (a typo, or literal 'inf') can't park the loop thread forever or
    raise OverflowError out of time.sleep()."""
    try:
        value = float(raw)
        if not math.isfinite(value):
            # nan compares False against everything, so it must be rejected
            # explicitly here rather than relying on min()/max() below.
            raise ValueError(f"non-finite value: {value}")
    except (TypeError, ValueError):
        print(f"[sensor_logger] invalid SENSOR_LOG_INTERVAL_S={raw!r}, falling back to {_SENSOR_LOG_INTERVAL_DEFAULT}")
        value = _SENSOR_LOG_INTERVAL_DEFAULT
    return min(max(1.0, value), _SENSOR_LOG_INTERVAL_MAX)


def _parse_max_captures(raw: str) -> int:
    """Parse MAX_CAPTURES_PER_DELIVERY defensively, same rationale as
    _parse_interval_s - runs at import time and must never raise. Falls back
    to the default on anything unparseable, and clamps to a minimum of 1 so
    the cap can't be configured to permanently block capture."""
    try:
        parsed = float(raw)
        if not math.isfinite(parsed):
            # int(float('inf')) raises OverflowError (not ValueError), so
            # finiteness must be checked before the int() conversion below.
            raise ValueError(f"non-finite value: {parsed}")
        value = int(parsed)
    except (TypeError, ValueError):
        print(f"[sensor_logger] invalid MAX_CAPTURES_PER_DELIVERY={raw!r}, falling back to {_MAX_CAPTURES_PER_DELIVERY_DEFAULT}")
        value = _MAX_CAPTURES_PER_DELIVERY_DEFAULT
    return max(1, value)


SENSOR_LOG_INTERVAL_S = _parse_interval_s(os.getenv("SENSOR_LOG_INTERVAL_S", str(_SENSOR_LOG_INTERVAL_DEFAULT)))
MAX_CAPTURES_PER_DELIVERY = _parse_max_captures(
    os.getenv("MAX_CAPTURES_PER_DELIVERY", str(_MAX_CAPTURES_PER_DELIVERY_DEFAULT))
)
PICAR_TIMEOUT = 5

SENSOR_DATA_DIR = Path(__file__).parent / "sensor_data"

# escrow_tx values for which we've already logged that the per-delivery
# capture cap was reached - so a stuck-pending delivery gets one log line,
# not one every SENSOR_LOG_INTERVAL_S for as long as it stays stuck.
_cap_logged_for: set[str] = set()


def _should_capture(active_order: dict | None) -> bool:
    return active_order is not None and active_order.get("status") == "pending"


def _capture_once(escrow_tx: str) -> None:
    """Fetch one frame+ultrasonic pair for escrow_tx and save it. Never
    raises - any failure just means one fewer data point, not a reason to
    stop the loop.

    Scoped, narrow guard against unbounded disk growth: a delivery stuck in
    'pending' forever (due to some unrelated bug) would otherwise accumulate
    captures indefinitely. If this delivery already has
    MAX_CAPTURES_PER_DELIVERY (or more) .json files on disk, skip this
    cycle's capture entirely - no delete/rotate, just stop adding more. This
    is not a retention/cleanup policy (out of scope for this PoC phase);
    existing captures are left untouched."""
    order_dir = SENSOR_DATA_DIR / escrow_tx
    try:
        existing_captures = len(list(order_dir.glob("*.json"))) if order_dir.exists() else 0
    except Exception as e:
        print(f"[sensor_logger] failed to count existing captures for {escrow_tx}: {e}")
        existing_captures = 0
    if existing_captures >= MAX_CAPTURES_PER_DELIVERY:
        if escrow_tx not in _cap_logged_for:
            print(
                f"[sensor_logger] {escrow_tx}: reached MAX_CAPTURES_PER_DELIVERY="
                f"{MAX_CAPTURES_PER_DELIVERY}, skipping further captures for this delivery"
            )
            _cap_logged_for.add(escrow_tx)
        return

    try:
        frame_resp = requests.get(
            f"{common.PICAR_SERVER_URL}:8080/debug/frame", timeout=PICAR_TIMEOUT
        )
    except Exception as e:
        print(f"[sensor_logger] frame fetch failed: {e}")
        return
    if frame_resp.status_code != 200:
        print(f"[sensor_logger] no frame available yet (status {frame_resp.status_code})")
        return

    try:
        ultrasonic_resp = requests.get(
            f"{common.PICAR_SERVER_URL}:8080/ultrasonic", timeout=PICAR_TIMEOUT
        )
        distance_cm = ultrasonic_resp.json().get("distance_cm")
    except Exception as e:
        print(f"[sensor_logger] ultrasonic fetch failed: {e}")
        return

    try:
        order_dir.mkdir(parents=True, exist_ok=True)
        captured_at = time.time()
        basename = f"{captured_at:.3f}"
        (order_dir / f"{basename}.jpg").write_bytes(frame_resp.content)
        (order_dir / f"{basename}.json").write_text(json.dumps({
            "distance_cm": distance_cm,
            "captured_at": captured_at,
            "escrow_tx": escrow_tx,
        }))
    except Exception as e:
        print(f"[sensor_logger] failed to write capture: {e}")
        return


def _run_one_cycle() -> None:
    """One iteration of the capture loop's body: load state and, if a
    delivery is active, capture once. Never raises - any exception here
    (including a torn read of order_state.json while buyer_app.py is
    concurrently writing it) is logged and swallowed so the caller's loop
    keeps running."""
    try:
        active_order, _ = common.load_state()
        if _should_capture(active_order):
            _capture_once(active_order["escrow_tx"])
    except Exception as e:
        print(f"[sensor_logger] loop iteration failed: {e}")


def _loop():
    while True:
        _run_one_cycle()
        time.sleep(SENSOR_LOG_INTERVAL_S)


def start() -> threading.Thread:
    """Start the capture loop in a background thread. Safe to call at
    module import time - not gated behind __main__."""
    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    return t
