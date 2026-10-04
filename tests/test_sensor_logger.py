#!/usr/bin/env python3
"""Offline test for car/sensor_logger.py - no real RPi/network needed.

Run: python tests/test_sensor_logger.py
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "car"))

import sensor_logger
import robopay_common as common

fails = []
_tmp_dirs = []


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
        self.ultrasonic_calls = 0

    def get(self, url, timeout=None):
        if url.endswith("/debug/frame"):
            if self.raise_on_frame:
                raise self.raise_on_frame
            return self.frame_resp
        if url.endswith("/ultrasonic"):
            self.ultrasonic_calls += 1
            if self.raise_on_ultrasonic:
                raise self.raise_on_ultrasonic
            return self.ultrasonic_resp
        raise AssertionError("unexpected URL " + url)


def _use_tmp_data_dir():
    tmp = Path(tempfile.mkdtemp(prefix="sensor_logger_test_"))
    _tmp_dirs.append(tmp)
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

# 3. No frame available yet (503) - must skip cleanly, write nothing, and
# must not even attempt the ultrasonic fetch (proves the 503 short-circuit,
# not just that the ultrasonic fake happens to default to None).
data_dir = _use_tmp_data_dir()
fake_503 = _FakeRequests(
    frame_resp=_FakeResp(503, json_data={"error": "no frame available yet"}),
)
sensor_logger.requests = fake_503
sensor_logger._capture_once("ESCROWTX222")
check(not (data_dir / "ESCROWTX222").exists(),
      "no_frame_yet: should not have written anything")
check(fake_503.ultrasonic_calls == 0,
      "no_frame_yet: ultrasonic endpoint should not have been called")

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

# 6. Both network calls succeed but the filesystem write step fails (e.g.
# disk/permissions error) - must not raise, and must not leave a
# directory behind. Stub Path.mkdir (not write_bytes) so order_dir is
# never created at all - that's the only way to prove "no directory left
# behind" rather than just "no files left behind".
data_dir = _use_tmp_data_dir()
sensor_logger.requests = _FakeRequests(
    frame_resp=_FakeResp(200, content=b"FAKEJPEGBYTES"),
    ultrasonic_resp=_FakeResp(200, json_data={"distance_cm": 18.5, "t": 123.0}),
)
_orig_mkdir = Path.mkdir


def _raising_mkdir(self, *args, **kwargs):
    raise OSError("simulated disk/permissions failure")


Path.mkdir = _raising_mkdir
try:
    try:
        sensor_logger._capture_once("ESCROWTX555")
    except Exception as e:
        check(False, "write_fails: _capture_once raised instead of swallowing: %r" % e)
finally:
    Path.mkdir = _orig_mkdir
check(not (data_dir / "ESCROWTX555").exists(),
      "write_fails: should not have left a directory behind")

# 7. _run_one_cycle: a bad common.load_state() call inside one iteration
# must not propagate (guards against a torn read of order_state.json while
# buyer_app.py concurrently writes it - would otherwise silently kill the
# background thread since _loop() itself is an unguarded `while True:`).
_orig_load_state = common.load_state


def _raising_load_state():
    raise json.JSONDecodeError("simulated torn read", "doc", 0)


common.load_state = _raising_load_state
try:
    try:
        sensor_logger._run_one_cycle()
    except Exception as e:
        check(False, "run_one_cycle: exception propagated instead of being swallowed: %r" % e)
finally:
    common.load_state = _orig_load_state

# Sanity: _run_one_cycle still works normally once load_state is restored.
data_dir = _use_tmp_data_dir()
sensor_logger.requests = _FakeRequests(
    frame_resp=_FakeResp(200, content=b"FAKEJPEGBYTES"),
    ultrasonic_resp=_FakeResp(200, json_data={"distance_cm": 9.0, "t": 1.0}),
)


def _fake_load_state_pending():
    return {"status": "pending", "escrow_tx": "ESCROWTX666"}, []


common.load_state = _fake_load_state_pending
try:
    sensor_logger._run_one_cycle()
finally:
    common.load_state = _orig_load_state
check((data_dir / "ESCROWTX666").exists(),
      "run_one_cycle: sanity check - should still capture on a good iteration")

# 8. _parse_interval_s: non-finite strings must fall back to the default,
# not reach time.sleep() unclamped (inf raised an unhandled OverflowError
# outside _run_one_cycle()'s guard; a huge-but-finite value parked the loop
# thread forever with zero indication anything was wrong).
check(sensor_logger._parse_interval_s("inf") == sensor_logger._SENSOR_LOG_INTERVAL_DEFAULT,
      "_parse_interval_s('inf') should fall back to the default")
check(sensor_logger._parse_interval_s("Infinity") == sensor_logger._SENSOR_LOG_INTERVAL_DEFAULT,
      "_parse_interval_s('Infinity') should fall back to the default")
check(sensor_logger._parse_interval_s("nan") == sensor_logger._SENSOR_LOG_INTERVAL_DEFAULT,
      "_parse_interval_s('nan') should fall back to the default (nan compares False against everything)")
check(sensor_logger._parse_interval_s("-inf") == sensor_logger._SENSOR_LOG_INTERVAL_DEFAULT,
      "_parse_interval_s('-inf') should fall back to the default")

# 9. _parse_interval_s: a very large finite value gets clamped to the cap,
# not passed through to time.sleep() as-is.
check(sensor_logger._parse_interval_s("999999999") == sensor_logger._SENSOR_LOG_INTERVAL_MAX,
      "_parse_interval_s('999999999') should clamp to the max")
check(sensor_logger._SENSOR_LOG_INTERVAL_MAX == 3600.0,
      "_SENSOR_LOG_INTERVAL_MAX should be 1 hour (3600.0)")

# 10. _parse_max_captures: same non-finite defensiveness as _parse_interval_s
# (int(float('inf')) raises OverflowError, not ValueError, if not guarded).
check(sensor_logger._parse_max_captures("inf") == sensor_logger._MAX_CAPTURES_PER_DELIVERY_DEFAULT,
      "_parse_max_captures('inf') should fall back to the default")
check(sensor_logger._parse_max_captures("nan") == sensor_logger._MAX_CAPTURES_PER_DELIVERY_DEFAULT,
      "_parse_max_captures('nan') should fall back to the default")
check(sensor_logger._parse_max_captures("999999999") == 999999999,
      "_parse_max_captures('999999999') should parse through unclamped (no upper bound needed here)")

# 11. Per-delivery capture cap: a delivery already at MAX_CAPTURES_PER_DELIVERY
# must not get a new capture written, even though the network calls would
# otherwise succeed. No files should be deleted/rotated either - just no
# new one added.
data_dir = _use_tmp_data_dir()
capped_order_dir = data_dir / "ESCROWTX_CAPPED"
capped_order_dir.mkdir(parents=True)
for i in range(sensor_logger.MAX_CAPTURES_PER_DELIVERY):
    (capped_order_dir / f"{i}.json").write_text("{}")
sensor_logger.requests = _FakeRequests(
    frame_resp=_FakeResp(200, content=b"FAKEJPEGBYTES"),
    ultrasonic_resp=_FakeResp(200, json_data={"distance_cm": 5.0, "t": 1.0}),
)
_files_before = set(capped_order_dir.iterdir())
sensor_logger._capture_once("ESCROWTX_CAPPED")
_files_after = set(capped_order_dir.iterdir())
check(_files_before == _files_after,
      "capture_cap: no new file should be written once at the cap")
check(len(list(capped_order_dir.glob("*.json"))) == sensor_logger.MAX_CAPTURES_PER_DELIVERY,
      "capture_cap: json count should stay exactly at the cap")
check(len(list(capped_order_dir.glob("*.jpg"))) == 0,
      "capture_cap: no jpg should be written once at the cap")

# 12. Per-delivery capture cap: a delivery with fewer than the cap's worth of
# existing captures must still capture normally (no regression).
data_dir = _use_tmp_data_dir()
under_cap_dir = data_dir / "ESCROWTX_UNDERCAP"
under_cap_dir.mkdir(parents=True)
for i in range(sensor_logger.MAX_CAPTURES_PER_DELIVERY - 1):
    (under_cap_dir / f"{i}.json").write_text("{}")
sensor_logger.requests = _FakeRequests(
    frame_resp=_FakeResp(200, content=b"FAKEJPEGBYTES"),
    ultrasonic_resp=_FakeResp(200, json_data={"distance_cm": 5.0, "t": 1.0}),
)
sensor_logger._capture_once("ESCROWTX_UNDERCAP")
check(len(list(under_cap_dir.glob("*.json"))) == sensor_logger.MAX_CAPTURES_PER_DELIVERY,
      "capture_cap: a delivery under the cap should still capture normally")
check(len(list(under_cap_dir.glob("*.jpg"))) == 1,
      "capture_cap: a delivery under the cap should still write a jpg")

for _d in _tmp_dirs:
    shutil.rmtree(_d, ignore_errors=True)

print()
if fails:
    print("FAILED CHECKS:")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("All checks passed.")
