# Sensor Data Collection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture a camera frame + ultrasonic reading every 11 seconds while Unit C has an active delivery, store them centrally on StaexHosting, and make them reachable from the existing Seller app's transaction table via a Solana Explorer link + a new per-delivery gallery.

**Architecture:** A background thread inside `car/seller_app.py` (StaexHosting side, not the RPi) polls the shared order state and, while a delivery is active, fetches a frame + ultrasonic reading from the RPi over the same mesh-proxy pattern the app's existing `/proxy/*` routes already use, saving both to local disk under `car/sensor_data/<escrow_tx>/`. A one-line addition to `car/buyer_app.py` tags each confirmed delivery's existing transaction-history entry with its `escrow_tx`, which the Seller app's already-existing transaction table then uses to link to a new gallery page.

**Tech Stack:** Python 3, Flask, `requests` — all already in use in this part of the repo. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-23-sensor-data-collection-design.md`

## Global Constraints

- Do not modify `car/car_main.py`, `car/gateway_app.py`, or anything that runs on the RPi. This feature lives entirely on the StaexHosting side.
- No retention/cleanup policy for accumulated files — out of scope for this PoC.
- No change to the existing `/clear_history` endpoint's behavior — it will still wipe `tx_history` (and therefore the transaction table's gallery links); this is accepted, not fixed.
- Nothing for the AI-agent delivery path (`agent/delivery_agent.py`) — fixed-QR path only, via the shared state both `car_main.py` and `buyer_app.py`/`seller_app.py` already use (`car/robopay_common.py`'s `load_state()`/`save_state()`).
- Capture interval is 11 seconds, configurable via `SENSOR_LOG_INTERVAL_S`.
- This repo has no pytest setup. Its established offline-test convention (see `agent/selftest.py`) is a standalone script: plain `check(cond, msg)` calls collecting failures into a `fails` list, a final PASS/FAIL summary, `sys.exit(1)` on any failure, run via `python <path>` (no `python3` on this Windows dev box's PATH).

---

## Task 1: `car/sensor_logger.py` — the capture loop

**Files:**
- Create: `car/sensor_logger.py`
- Create: `tests/test_sensor_logger.py`
- Modify: `.gitignore`

**Interfaces:**
- Produces: `sensor_logger.SENSOR_DATA_DIR: Path` (module-level, reassignable by tests). `sensor_logger._should_capture(active_order: dict | None) -> bool`. `sensor_logger._capture_once(escrow_tx: str) -> None` — never raises; any failure is logged and swallowed. `sensor_logger.start() -> threading.Thread` — starts the background loop, safe to call at module import time (not gated behind `__main__`).
- Consumes: `robopay_common.load_state()` (existing, returns `(active_order: dict | None, tx_history: list)`), `robopay_common.PICAR_SERVER_URL` (existing config value).

- [ ] **Step 1: Write the failing test**

Create `tests/test_sensor_logger.py`:

```python
#!/usr/bin/env python3
"""Offline test for car/sensor_logger.py - no real RPi/network needed.

Run: python tests/test_sensor_logger.py
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "car"))

import sensor_logger

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


class _FakeResp:
    def __init__(self, status_code=200, content=b"", json_data=None):
        self.status_code = status_code
        self.content = content
        self._json_data = json_data

    def json(self):
        return self._json_data


class _FakeRequests:
    """Swaps in for the `requests` module sensor_logger.py imports."""

    def __init__(self, frame_resp=None, ultrasonic_resp=None,
                 raise_on_frame=None, raise_on_ultrasonic=None):
        self.frame_resp = frame_resp
        self.ultrasonic_resp = ultrasonic_resp
        self.raise_on_frame = raise_on_frame
        self.raise_on_ultrasonic = raise_on_ultrasonic

    def get(self, url, timeout=None):
        if url.endswith("/debug/frame"):
            if self.raise_on_frame:
                raise self.raise_on_frame
            return self.frame_resp
        if url.endswith("/ultrasonic"):
            if self.raise_on_ultrasonic:
                raise self.raise_on_ultrasonic
            return self.ultrasonic_resp
        raise AssertionError("unexpected URL " + url)


def _use_tmp_data_dir():
    tmp = Path(tempfile.mkdtemp(prefix="sensor_logger_test_"))
    sensor_logger.SENSOR_DATA_DIR = tmp
    return tmp


# 1. _should_capture: only true for a pending active order.
check(sensor_logger._should_capture({"status": "pending"}) is True,
      "_should_capture: should be True for a pending order")
check(sensor_logger._should_capture({"status": "delivered"}) is False,
      "_should_capture: should be False once delivered")
check(sensor_logger._should_capture(None) is False,
      "_should_capture: should be False with no active order")

# 2. Happy path: frame + ultrasonic both succeed, files written correctly.
data_dir = _use_tmp_data_dir()
sensor_logger.requests = _FakeRequests(
    frame_resp=_FakeResp(200, content=b"FAKEJPEGBYTES"),
    ultrasonic_resp=_FakeResp(200, json_data={"distance_cm": 18.5, "t": 123.0}),
)
sensor_logger._capture_once("ESCROWTX111")
order_dir = data_dir / "ESCROWTX111"
jpgs = list(order_dir.glob("*.jpg"))
jsons = list(order_dir.glob("*.json"))
check(len(jpgs) == 1, "happy: expected exactly one .jpg written")
check(len(jsons) == 1, "happy: expected exactly one .json written")
if jpgs:
    check(jpgs[0].read_bytes() == b"FAKEJPEGBYTES", "happy: jpg content mismatch")
if jsons:
    record = json.loads(jsons[0].read_text())
    check(record["distance_cm"] == 18.5, "happy: distance_cm not recorded correctly")
    check(record["escrow_tx"] == "ESCROWTX111", "happy: escrow_tx not recorded correctly")
    check("captured_at" in record, "happy: captured_at missing")

# 3. No frame available yet (503) - must skip cleanly, write nothing.
data_dir = _use_tmp_data_dir()
sensor_logger.requests = _FakeRequests(
    frame_resp=_FakeResp(503, json_data={"error": "no frame available yet"}),
)
sensor_logger._capture_once("ESCROWTX222")
check(not (data_dir / "ESCROWTX222").exists(),
      "no_frame_yet: should not have written anything")

# 4. RPi/mesh unreachable on the frame fetch - must not raise.
data_dir = _use_tmp_data_dir()
sensor_logger.requests = _FakeRequests(raise_on_frame=ConnectionError("mesh down"))
try:
    sensor_logger._capture_once("ESCROWTX333")
except Exception as e:
    check(False, "unreachable: _capture_once raised instead of swallowing: %r" % e)
check(not (data_dir / "ESCROWTX333").exists(),
      "unreachable: should not have written anything")

# 5. Frame OK but ultrasonic fetch fails - must not raise, must not write
# a half-complete pair.
data_dir = _use_tmp_data_dir()
sensor_logger.requests = _FakeRequests(
    frame_resp=_FakeResp(200, content=b"FAKEJPEGBYTES"),
    raise_on_ultrasonic=ConnectionError("mesh down"),
)
try:
    sensor_logger._capture_once("ESCROWTX444")
except Exception as e:
    check(False, "ultrasonic_fails: _capture_once raised: %r" % e)
check(not (data_dir / "ESCROWTX444").exists(),
      "ultrasonic_fails: should not have written a half-complete pair")

print()
if fails:
    print("FAILED CHECKS:")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("All checks passed.")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python tests/test_sensor_logger.py`
Expected: `ModuleNotFoundError: No module named 'sensor_logger'`

- [ ] **Step 3: Write the implementation**

Create `car/sensor_logger.py`:

```python
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
import os
import threading
import time
from pathlib import Path

import requests

import robopay_common as common

SENSOR_LOG_INTERVAL_S = float(os.getenv("SENSOR_LOG_INTERVAL_S", "11"))
PICAR_TIMEOUT = 5

SENSOR_DATA_DIR = Path(__file__).parent / "sensor_data"


def _should_capture(active_order: dict | None) -> bool:
    return active_order is not None and active_order.get("status") == "pending"


def _capture_once(escrow_tx: str) -> None:
    """Fetch one frame+ultrasonic pair for escrow_tx and save it. Never
    raises - any failure just means one fewer data point, not a reason to
    stop the loop."""
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

    order_dir = SENSOR_DATA_DIR / escrow_tx
    order_dir.mkdir(parents=True, exist_ok=True)
    captured_at = time.time()
    basename = f"{captured_at:.3f}"
    (order_dir / f"{basename}.jpg").write_bytes(frame_resp.content)
    (order_dir / f"{basename}.json").write_text(json.dumps({
        "distance_cm": distance_cm,
        "captured_at": captured_at,
        "escrow_tx": escrow_tx,
    }))


def _loop():
    while True:
        active_order, _ = common.load_state()
        if _should_capture(active_order):
            _capture_once(active_order["escrow_tx"])
        time.sleep(SENSOR_LOG_INTERVAL_S)


def start() -> threading.Thread:
    """Start the capture loop in a background thread. Safe to call at
    module import time - not gated behind __main__."""
    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    return t
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python tests/test_sensor_logger.py`
Expected: `All checks passed.`

- [ ] **Step 5: Add `car/sensor_data/` to `.gitignore`**

The existing `.gitignore` already excludes `*.json` globally (covers this
feature's paired records), but not `.jpg` files. Add a dedicated entry so
captured images are never accidentally committed - append to `.gitignore`:

```
# Sensor data captures (car/sensor_logger.py)
car/sensor_data/
```

- [ ] **Step 6: Commit**

```bash
git add car/sensor_logger.py tests/test_sensor_logger.py .gitignore
git commit -m "Add car/sensor_logger.py: periodic frame+ultrasonic capture during delivery"
```

---

## Task 2: `car/buyer_app.py` — tag confirmed deliveries with `escrow_tx`

**Files:**
- Modify: `car/buyer_app.py:140-152` (the `/delivered` route handler)
- Create: `tests/test_buyer_app_delivered.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `tx_history` entries of type `confirm_delivery` now include an `escrow_tx` key. Task 3 (the Seller app's transaction table) relies on this field being present to link to a delivery's gallery.

`buyer_app.py` loads a real Solana keypair from disk at import time
(`buyer_kp = _load_keypair(BUYER_KEYPAIR_PATH)`, `car/buyer_app.py:47`) and
requires `BUYER_USERNAME`/`BUYER_PASSWORD` env vars to be set (checked by
`robopay_common.make_auth`, which raises `RuntimeError` if either is
missing) - the test below sets up a throwaway keypair file and those two
env vars before importing the module, so nothing here touches a real
wallet or needs network access. `/delivered` itself is exempt from HTTP
auth (`car/buyer_app.py:92-93`, `MACHINE_PATHS`), so the test can call it
directly with no `Authorization` header.

- [ ] **Step 1: Write the failing test**

Create `tests/test_buyer_app_delivered.py`:

```python
#!/usr/bin/env python3
"""Offline test for buyer_app.py's /delivered handler - confirms the new
escrow_tx field lands in tx_history without changing existing behavior.

Run: python tests/test_buyer_app_delivered.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "car"))

os.environ.setdefault("BUYER_USERNAME", "test")
os.environ.setdefault("BUYER_PASSWORD", "test")
os.environ.setdefault("SELLER_USERNAME", "test")
os.environ.setdefault("SELLER_PASSWORD", "test")

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


# A throwaway keypair file - buyer_app.py loads one at import time.
from solders.keypair import Keypair
kp = Keypair()
keyfile = Path(tempfile.mktemp(suffix=".json"))
keyfile.write_text(json.dumps(list(bytes(kp))))
os.environ["BUYER_KEYPAIR_PATH"] = str(keyfile)

import robopay_common as common
common.STATE_FILE = Path(tempfile.mktemp(suffix=".json"))

import buyer_app

client = buyer_app.app.test_client()


def _order(escrow_tx):
    return {
        "lat": 52.3609, "lon": 14.06, "buyer_pubkey": "BUYERPK111",
        "escrow_tx": escrow_tx, "status": "pending", "delivery_tx": None,
        "ordered_at": 0.0, "trigger_mode": "fixed",
    }


# 1. Happy path: escrow_tx lands in the new tx_history entry, existing
# fields/behavior unchanged.
buyer_app._active_order = _order("ESCROWTX555")
buyer_app._tx_history = []
resp = client.post("/delivered", json={"delivery_tx": "SIGCONFIRM999"})
check(resp.status_code == 200, "delivered: unexpected status %s" % resp.status_code)
check(buyer_app._active_order["status"] == "delivered",
      "delivered: order status not updated (existing behavior)")
check(buyer_app._active_order["delivery_tx"] == "SIGCONFIRM999",
      "delivered: delivery_tx not recorded (existing behavior)")
check(len(buyer_app._tx_history) == 1, "delivered: expected exactly one tx_history entry")
if buyer_app._tx_history:
    entry = buyer_app._tx_history[0]
    check(entry["type"] == "confirm_delivery", "delivered: wrong entry type")
    check(entry["sig"] == "SIGCONFIRM999", "delivered: wrong sig")
    check(entry["escrow_tx"] == "ESCROWTX555", "delivered: escrow_tx not included (the new field)")

# 2. dry-run-tx must still be excluded from tx_history (existing behavior,
# unchanged by this task).
buyer_app._active_order = _order("ESCROWTX666")
buyer_app._tx_history = []
resp = client.post("/delivered", json={"delivery_tx": "dry-run-tx"})
check(resp.status_code == 200, "dry_run: unexpected status %s" % resp.status_code)
check(len(buyer_app._tx_history) == 0,
      "dry_run: dry-run-tx should still be excluded from tx_history (existing behavior)")

print()
if fails:
    print("FAILED CHECKS:")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("All checks passed.")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python tests/test_buyer_app_delivered.py`
Expected: `KeyError: 'escrow_tx'` (the check for `entry["escrow_tx"]`) - the field doesn't exist yet.

- [ ] **Step 3: Implement the minimal change**

In `car/buyer_app.py`, the `/delivered` handler currently reads (around
line 140-152):

```python
@app.route("/delivered", methods=["POST"])
def delivered():
    global _active_order, _force_delivery
    data = request.get_json() or {}
    delivery_tx = data.get("delivery_tx")
    if _active_order:
        _active_order["status"] = "delivered"
        _active_order["delivery_tx"] = delivery_tx
        if delivery_tx and delivery_tx != "dry-run-tx":
            _tx_history.append({"type": "confirm_delivery", "sig": delivery_tx, "t": time.time()})
    _force_delivery = False
    common.save_state(_active_order, _tx_history)
    return jsonify({"success": True})
```

Change the one `_tx_history.append(...)` line to:

```python
            _tx_history.append({"type": "confirm_delivery", "sig": delivery_tx, "t": time.time(),
                                 "escrow_tx": _active_order.get("escrow_tx")})
```

(Nothing else in the function changes - it's still inside the same
`if _active_order:` block, so `_active_order` is guaranteed non-`None`
there.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python tests/test_buyer_app_delivered.py`
Expected: `All checks passed.`

- [ ] **Step 5: Commit**

```bash
git add car/buyer_app.py tests/test_buyer_app_delivered.py
git commit -m "buyer_app.py: tag confirmed-delivery tx_history entries with escrow_tx"
```

---

## Task 3: `car/seller_app.py` — gallery route + transaction table link

**Files:**
- Modify: `car/seller_app.py`

**Interfaces:**
- Consumes: `sensor_logger.start()` (Task 1). `tx_history` entries now carrying `escrow_tx` (Task 2) - this task's own code needs no change to read it, since the existing `/transactions` route already serializes whatever's in each entry.
- Produces: `GET /gallery/<escrow_tx>` (HTML page). `GET /gallery/<escrow_tx>/<filename>` (serves one captured file's bytes). Both reachable at `/seller/gallery/...` through the existing gateway proxy with no gateway change (confirmed: `car/gateway_app.py`'s `/seller/<path:subpath>` route already forwards anything under `/seller/`).

No new test file for this task - it's a Flask app serving HTML/files
against the local filesystem, which the project's established stub-based
test style doesn't have a lightweight pattern for (unlike Task 2's pure
JSON-in/JSON-out route). Verified instead by direct code reading against
Task 1's and Task 2's already-tested behavior, plus the manual check in
Step 4 below.

- [ ] **Step 1: Start the capture loop at app init**

In `car/seller_app.py`, add the import near the top (after the existing
`import robopay_common as common` line):

```python
import sensor_logger
```

Add the startup call right after `common.make_auth(app, "SELLER_USERNAME", "SELLER_PASSWORD")`
(module level, not inside `if __name__ == "__main__":` - this file has no
other code gated behind `__main__` besides the final `app.run(...)` call,
and `common.make_auth(...)` itself already runs unconditionally at import
time, so this matches the file's existing pattern):

```python
sensor_logger.start()
```

- [ ] **Step 2: Add the gallery routes**

Add these two routes anywhere in the routes section (e.g. right after the
existing `transactions()` route, before the `# --- Proxy to picar_server.py`
comment block):

```python
@app.route("/gallery/<escrow_tx>")
def gallery(escrow_tx):
    order_dir = sensor_logger.SENSOR_DATA_DIR / escrow_tx
    entries = []
    if order_dir.exists():
        for json_path in sorted(order_dir.glob("*.json")):
            record = json.loads(json_path.read_text())
            entries.append({
                "filename": json_path.stem + ".jpg",
                "distance_cm": record.get("distance_cm"),
                "captured_at": record.get("captured_at"),
            })
    rows = "".join(
        f'<div class="panel" style="margin-bottom:12px;">'
        f'<img class="video" src="/gallery/{escrow_tx}/{e["filename"]}" style="max-width:240px;">'
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
<h1>Sensordaten — {escrow_tx}</h1>
<p><a href="/">&larr; zurück</a></p>
{rows}
</body></html>""", mimetype="text/html")


@app.route("/gallery/<escrow_tx>/<filename>")
def gallery_file(escrow_tx, filename):
    # escrow_tx/filename come straight from the URL - resolve and confirm
    # the final path is still inside SENSOR_DATA_DIR before reading, so a
    # crafted "../../" can't escape it and read arbitrary server files.
    base = sensor_logger.SENSOR_DATA_DIR.resolve()
    path = (base / escrow_tx / filename).resolve()
    if base not in path.parents or not path.exists():
        return jsonify({"error": "not found"}), 404
    return Response(path.read_bytes(), mimetype="image/jpeg")
```

This needs `json` importable in `seller_app.py` - add `import json` to the
top of the file, alongside the existing `import os` line.

- [ ] **Step 3: Link the gallery from the existing transaction table**

In `INDEX_HTML`'s `pollTx()` function (the `<script>` block near the
bottom of the file), the row template currently reads:

```javascript
async function pollTx() {
  try {
    const r = await fetch('/transactions');
    const d = await r.json();
    const tbody = document.querySelector('#txTable tbody');
    tbody.innerHTML = d.map(tx =>
      `<tr><td>Escrow-Release</td><td><a href="https://explorer.solana.com/tx/${tx.sig}?cluster=devnet" target="_blank">${tx.sig.slice(0,12)}...</a></td></tr>`
    ).join('') || '<tr><td>— noch keine Auszahlungen —</td></tr>';
  } catch (e) {}
}
```

Change the row template to add a gallery link when `tx.escrow_tx` is
present (older entries recorded before Task 2 shipped won't have it - the
row still renders, just without the extra link):

```javascript
async function pollTx() {
  try {
    const r = await fetch('/transactions');
    const d = await r.json();
    const tbody = document.querySelector('#txTable tbody');
    tbody.innerHTML = d.map(tx => {
      const galleryLink = tx.escrow_tx
        ? ` &middot; <a href="/gallery/${tx.escrow_tx}">Sensordaten</a>` : '';
      return `<tr><td>Escrow-Release</td><td><a href="https://explorer.solana.com/tx/${tx.sig}?cluster=devnet" target="_blank">${tx.sig.slice(0,12)}...</a>${galleryLink}</td></tr>`;
    }).join('') || '<tr><td>— noch keine Auszahlungen —</td></tr>';
  } catch (e) {}
}
```

- [ ] **Step 4: Manual verification**

`sensor_logger`/`buyer_app` changes aren't independently live-testable
without the physical robot (car is currently off - see the spec's Testing
section). Verify this task by direct reading instead:
1. Re-read the full modified `car/seller_app.py` top to bottom and confirm
   `import json` and `import sensor_logger` are both present, `sensor_logger.start()`
   is called at module level (not inside `if __name__ == "__main__":`),
   and both new routes are syntactically valid Python (e.g.
   `python -c "import ast; ast.parse(open('car/seller_app.py').read())"`
   parses without error).
2. Confirm the `pollTx()` change is valid JavaScript by eye (matching
   template-literal syntax, no unbalanced braces) - there is no JS test
   harness in this repo to run it against.
3. Confirm `gallery()`/`gallery_file()` correctly compose with Task 1's
   `sensor_logger.SENSOR_DATA_DIR` and the `.json`/`.jpg` file-naming
   convention `_capture_once()` writes (same basename, `.json` for the
   record and `.jpg` for the image) - re-read `car/sensor_logger.py`'s
   `_capture_once()` alongside this task's `gallery()` route side by side
   to confirm the field names (`distance_cm`, `captured_at`) match exactly.
4. Confirm `gallery_file()`'s path-traversal guard actually rejects an
   escaping path: manually trace `escrow_tx="../../etc"`,
   `filename="passwd"` through `(base / escrow_tx / filename).resolve()`
   and confirm the resolved path does NOT have `base` among its parents,
   so the `404` branch is taken rather than reading the file.

- [ ] **Step 5: Commit**

```bash
git add car/seller_app.py
git commit -m "seller_app.py: start sensor capture loop, add gallery view, link it from the transaction table"
```
