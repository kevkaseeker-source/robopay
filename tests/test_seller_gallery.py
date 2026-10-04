#!/usr/bin/env python3
"""Offline test for seller_app.py's /gallery routes and /transactions -
covers the final whole-branch review's Finding 1 (gallery links/srcs must be
RELATIVE so they survive car/gateway_app.py's /seller/ mount), Finding 2
(a corrupted .json sidecar must not permanently 500 a delivery's gallery),
and the pre-existing path-traversal/XSS/content-type guards from earlier fix
rounds.

Run: python tests/test_seller_gallery.py
"""
import base64
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "car"))

os.environ.setdefault("SELLER_USERNAME", "test")
os.environ.setdefault("SELLER_PASSWORD", "test")
# buyer_app.py isn't imported here, but robopay_common.make_auth() is shared
# and only checks the env vars its own call site asks for - SELLER_* is all
# seller_app.py needs.

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


# seller_app.py calls sensor_logger.start() directly at its own module
# level (see car/seller_app.py: `sensor_logger.start()` right after
# `common.make_auth(...)`), so importing seller_app the normal way would
# spin up a real background thread that immediately starts trying to reach
# a nonexistent RPi over PICAR_SERVER_URL. Python caches modules in
# sys.modules on first import, and a later `import sensor_logger` (from
# inside seller_app.py) just rebinds the name to that same cached object -
# it does not re-execute sensor_logger.py or get a fresh function object.
# So: import sensor_logger ourselves first, monkeypatch its `start`
# attribute to a no-op, and only then import seller_app - when
# seller_app.py's module body calls `sensor_logger.start()`, that's an
# attribute lookup at call time, and it finds our no-op.
import sensor_logger
sensor_logger.start = lambda: None

import robopay_common as common
common.STATE_FILE = Path(tempfile.mktemp(suffix=".json"))

import seller_app

client = seller_app.app.test_client()

AUTH_HEADER = {
    "Authorization": "Basic " + base64.b64encode(b"test:test").decode(),
}


def _use_tmp_sensor_dir():
    tmp = Path(tempfile.mkdtemp(prefix="seller_gallery_test_"))
    sensor_logger.SENSOR_DATA_DIR = tmp
    return tmp


def _write_capture(order_dir, basename, distance_cm=42.0, captured_at=123.456, escrow_tx="X"):
    order_dir.mkdir(parents=True, exist_ok=True)
    (order_dir / f"{basename}.jpg").write_bytes(b"FAKEJPEGBYTES")
    (order_dir / f"{basename}.json").write_text(json.dumps({
        "distance_cm": distance_cm, "captured_at": captured_at, "escrow_tx": escrow_tx,
    }))


# ---------------------------------------------------------------------------
# 1. Gallery lists captured entries correctly.
# ---------------------------------------------------------------------------
data_dir = _use_tmp_sensor_dir()
order_dir = data_dir / "ESCROWTX_LIST"
_write_capture(order_dir, "1000.000", distance_cm=17.5, captured_at=1000.0)

resp = client.get("/gallery/ESCROWTX_LIST", headers=AUTH_HEADER)
check(resp.status_code == 200, "list: unexpected status %s" % resp.status_code)
body = resp.get_data(as_text=True)
check("1000.000.jpg" in body, "list: expected filename not found in rendered gallery")
check("17.5" in body, "list: expected distance_cm not found in rendered gallery")


# ---------------------------------------------------------------------------
# 2. escrow_tx is HTML-escaped in the rendered gallery page.
# ---------------------------------------------------------------------------
data_dir = _use_tmp_sensor_dir()
# No literal "/" in the payload - escrow_tx is matched by Werkzeug's default
# single-segment converter, and even a percent-encoded %2F gets treated as a
# real path separator by WSGI's PATH_INFO decoding (verified separately:
# GET /gallery/%2F 404s against a trivial single-route Flask app), so a
# payload containing "/" can never reach this route as a single escrow_tx
# value - it isn't a realistic attack shape for this endpoint. onload= on an
# <svg> proves the same thing a <script> tag would: raw "<"/">"/'"' reaching
# the page unescaped.
xss_payload = '"><svg onload=alert(1)>'
resp = client.get("/gallery/" + quote(xss_payload, safe=""), headers=AUTH_HEADER)
check(resp.status_code == 200, "xss: unexpected status %s" % resp.status_code)
body = resp.get_data(as_text=True)
check("<svg onload=alert(1)>" not in body,
      "xss: raw payload found unescaped in response - reflected XSS")
check("&lt;svg" in body, "xss: expected escaped payload not found in response")


# ---------------------------------------------------------------------------
# 3. Finding 1 (Critical): the rendered gallery page's img src and back link
# are RELATIVE (don't start with "/") - this is what makes the page work
# identically hit directly on :5002 (page URL "/gallery/<tx>") and through
# car/gateway_app.py's "/seller/" mount (page URL "/seller/gallery/<tx>"):
# a relative reference resolves against the page's OWN url, landing on
# ".../gallery/<tx>/<file>" and ".../" respectively in both cases, with no
# gateway rewrite involved (gateway_app.py only rewrites the index page).
# An absolute "/gallery/..." src, by contrast, would resolve to the
# gateway's ROOT and 404 once reached through "/seller/".
# ---------------------------------------------------------------------------
data_dir = _use_tmp_sensor_dir()
order_dir = data_dir / "ESCROWTX_REL"
_write_capture(order_dir, "2000.000")

resp = client.get("/gallery/ESCROWTX_REL", headers=AUTH_HEADER)
check(resp.status_code == 200, "relative: unexpected status %s" % resp.status_code)
body = resp.get_data(as_text=True)
check('src="/gallery/' not in body, "relative: img src is absolute (starts with /gallery/) - Finding 1 regression")
check('src="ESCROWTX_REL/2000.000.jpg"' in body,
      "relative: img src is not the expected relative reference")
check('href="/">' not in body, "relative: back link is absolute (starts with /) - Finding 1 regression")
check('href="../"' in body, "relative: back link is not the expected relative '../' reference")

# Same check on the index page's transaction-table gallery link (pollTx()'s
# JS template string) - also must be relative, same reasoning. Matched via
# regex rather than an exact source string so this survives the value being
# escaped/renamed (e.g. escapeHtml(tx.escrow_tx) as safeEscrowTx) as long as
# the href itself stays relative.
index_body = seller_app.INDEX_HTML
gallery_href = re.search(r'href="(/?gallery/\$\{[^}]*\})"', index_body)
check(gallery_href is not None,
      "relative: pollTx() gallery link template not found as a relative reference")
check(gallery_href and not gallery_href.group(1).startswith("/gallery/"),
      "relative: pollTx() gallery link is absolute - Finding 1 regression")


# ---------------------------------------------------------------------------
# 4. ".." in escrow_tx is rejected by both gallery() and gallery_file() - no
# directory listing / no file read from outside sensor_data. Confirmed
# separately that Werkzeug's default <escrow_tx> converter passes a literal
# ".." segment straight through to the view function (it does not collapse
# "/gallery/.." itself), so this exercises the app's own guard, not routing.
# ---------------------------------------------------------------------------
data_dir = _use_tmp_sensor_dir()
# A file that really exists just outside SENSOR_DATA_DIR, so a successful
# traversal would be able to prove it by finding/reading it.
(data_dir.parent / "outside_marker.txt").write_text("should never be reachable")

resp = client.get("/gallery/..", headers=AUTH_HEADER)
check(resp.status_code == 200, "traversal gallery(): unexpected status %s" % resp.status_code)
body = resp.get_data(as_text=True)
check("outside_marker" not in body, "traversal gallery(): leaked content from outside sensor_data")
check("keine Sensordaten" in body, "traversal gallery(): expected the empty-state placeholder, got listed entries")

resp = client.get("/gallery/../outside_marker.txt", headers=AUTH_HEADER)
check(resp.status_code == 404, "traversal gallery_file(): expected 404, got %s" % resp.status_code)
check(b"should never be reachable" not in resp.data,
      "traversal gallery_file(): leaked content from outside sensor_data")


# ---------------------------------------------------------------------------
# 5. A non-.jpg filename (e.g. the .json sidecar itself) is rejected by
# gallery_file().
# ---------------------------------------------------------------------------
data_dir = _use_tmp_sensor_dir()
order_dir = data_dir / "ESCROWTX_SUFFIX"
_write_capture(order_dir, "3000.000")

resp = client.get("/gallery/ESCROWTX_SUFFIX/3000.000.json", headers=AUTH_HEADER)
check(resp.status_code == 404, "suffix: .json sidecar should be rejected, got %s" % resp.status_code)

resp = client.get("/gallery/ESCROWTX_SUFFIX/3000.000.jpg", headers=AUTH_HEADER)
check(resp.status_code == 200, "suffix: legitimate .jpg should be served, got %s" % resp.status_code)
check(resp.data == b"FAKEJPEGBYTES", "suffix: served .jpg content mismatch")


# ---------------------------------------------------------------------------
# 6. Finding 2: a corrupted/unparseable .json file doesn't crash gallery() -
# the page still renders, just skips that entry.
# ---------------------------------------------------------------------------
data_dir = _use_tmp_sensor_dir()
order_dir = data_dir / "ESCROWTX_CORRUPT"
order_dir.mkdir(parents=True, exist_ok=True)
# A torn/partial write - valid start, cut off mid-object, exactly what a
# disk-full-mid-write or a read racing an in-flight capture would leave.
(order_dir / "4000.000.jpg").write_bytes(b"FAKEJPEGBYTES")
(order_dir / "4000.000.json").write_text('{"distance_cm": 12.0, "captured')
# A good entry alongside it, to confirm the corrupt one is skipped rather
# than the whole page bailing out early.
_write_capture(order_dir, "4001.000", distance_cm=99.0, captured_at=4001.0)

resp = client.get("/gallery/ESCROWTX_CORRUPT", headers=AUTH_HEADER)
check(resp.status_code == 200, "corrupt: expected 200 despite corrupted .json, got %s" % resp.status_code)
body = resp.get_data(as_text=True)
check("4001.000.jpg" in body, "corrupt: good entry alongside the corrupt one should still render")
check("99.0" in body, "corrupt: good entry's distance_cm should still render")


# ---------------------------------------------------------------------------
# 7. /transactions passes escrow_tx through once buyer_app.py's tag is
# present. /transactions itself wasn't modified by this branch - light
# check that the field survives common.load_state() -> jsonify() round-trip.
# ---------------------------------------------------------------------------
common.save_state(None, [
    {"type": "confirm_delivery", "sig": "SIGABC123", "escrow_tx": "ESCROWTX_TXLIST"},
    {"type": "create_delivery", "sig": "SIGOTHER456"},
])
resp = client.get("/transactions", headers=AUTH_HEADER)
check(resp.status_code == 200, "transactions: unexpected status %s" % resp.status_code)
data = resp.get_json()
check(isinstance(data, list) and len(data) == 1,
      "transactions: expected exactly one confirm_delivery entry, got %r" % (data,))
if data:
    check(data[0].get("escrow_tx") == "ESCROWTX_TXLIST",
          "transactions: escrow_tx did not survive the round-trip")


# ---------------------------------------------------------------------------
# Bonus: auth is still enforced (sanity check that AUTH_HEADER above is
# actually doing something, not that every route happens to be exempt).
# ---------------------------------------------------------------------------
resp = client.get("/gallery/ESCROWTX_LIST")
check(resp.status_code == 401, "auth: expected 401 without credentials, got %s" % resp.status_code)


print()
if fails:
    print("FAILED CHECKS:")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("All checks passed.")
