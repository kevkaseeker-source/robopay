#!/usr/bin/env python3
"""End-to-end test for picar_server.py's drive-watchdog behavior (the
2026-10-03 fix): real Flask routing, real request/response cycle, real
background threads doing the actual time.sleep()-based watchdog - the only
things stubbed are the Raspberry Pi hardware modules (picarx, vilib), which
don't exist/work off-device. This exercises the exact bug this fix closes:
a quick second /drive command must NOT get killed by an earlier command's
delayed auto-stop.

Run: python tests/test_picar_server_drive_watchdog.py
"""
import sys
import threading
import time
import types
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "car"))

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


# --- Stub the hardware modules picar_server.py imports at module level ---
class _FakePicarx:
    """Records every motor call in order, with a timestamp, so tests can
    assert not just "stop was called" but *when*, relative to other calls."""

    def __init__(self):
        self.calls = []  # list of (t, method, args)

    def _log(self, method, *args):
        self.calls.append((time.time(), method, args))

    def set_cam_pan_angle(self, *a): pass
    def set_cam_tilt_angle(self, *a): pass
    def set_dir_servo_angle(self, a): self._log("set_dir_servo_angle", a)
    def forward(self, s): self._log("forward", s)
    def backward(self, s): self._log("backward", s)
    def stop(self): self._log("stop")
    def get_distance(self): return 42.0


_fake_px_instance = _FakePicarx()

picarx_module = types.ModuleType("picarx")
picarx_module.Picarx = lambda: _fake_px_instance
sys.modules["picarx"] = picarx_module

vilib_module = types.ModuleType("vilib")


class _FakePicam2:
    def set_controls(self, controls): pass


class _FakeVilib:
    detect_obj_parameter = {"qr_data": "None"}
    flask_img = None
    picam2 = _FakePicam2()  # picar_server caps the frame rate via Vilib.picam2.set_controls

    @staticmethod
    def camera_start(**kw): pass
    @staticmethod
    def qrcode_detect_switch(v): pass
    @staticmethod
    def display(**kw): pass


vilib_module.Vilib = _FakeVilib
sys.modules["vilib"] = vilib_module

# Now safe to import the real module - hardware init runs against the fakes.
import picar_server  # noqa: E402

picar_server.DRIVE_DURATION_MAX_S = 1.0  # shrink the window so the test runs fast
client = picar_server.app.test_client()


def motor_calls_since(t0):
    return [c for c in _fake_px_instance.calls if c[0] >= t0]


# 1. A quick second /drive command must not be killed by the first
# command's delayed auto-stop - the exact bug this fix closes.
#
# Flask's test_client().post() runs the view function synchronously in the
# calling thread - including /drive's internal time.sleep(duration)
# watchdog wait - so it does NOT return until that whole wait finishes.
# The real server runs with threaded=True, where a second request arrives
# and is handled on its own thread while the first is still mid-sleep.
# Firing the first request on a background thread reproduces that real
# concurrency instead of accidentally serializing both requests.
_fake_px_instance.calls.clear()
t0 = time.time()
r1_result = {}
t1 = threading.Thread(
    target=lambda: r1_result.update(
        status=client.post("/drive", json={"speed": 40, "angle": 0}).status_code  # no duration_s, like the real D-pad UI
    )
)
t1.start()
time.sleep(0.3)  # well before DRIVE_DURATION_MAX_S=1.0s elapses
r2 = client.post("/drive", json={"speed": 40, "angle": -30})  # user taps "left" quickly after
check(r2.status_code == 200, "second /drive should succeed")
t1.join(timeout=2)
check(r1_result.get("status") == 200, "first /drive should succeed")

# Wait past BOTH commands' auto-stop windows (first: ~1.0s from t0, second: ~1.0s from its own start)
time.sleep(1.2)
calls = motor_calls_since(t0)
stop_calls = [c for c in calls if c[1] == "stop"]
check(len(stop_calls) == 1, f"expected exactly 1 stop() call (from the second command's own watchdog), got {len(stop_calls)}: {calls}")
if stop_calls:
    # The single stop() must come from the SECOND command's watchdog
    # (fires ~1.0s after the second request started, i.e. ~1.3s after t0),
    # not the first command's (~1.0s after t0) - if the bug were present,
    # a stop would land around the 1.0s mark instead, right after the
    # second command's set_dir_servo_angle/forward calls, killing it.
    stop_t = stop_calls[0][0] - t0
    check(stop_t > 1.1, f"stop() fired too early ({stop_t:.2f}s) - looks like the FIRST command's watchdog killed the second command, not its own")

# 2. A single /drive command with no follow-up genuinely still auto-stops
# (the core safety property - this must keep working).
_fake_px_instance.calls.clear()
t0 = time.time()
client.post("/drive", json={"speed": 40, "angle": 0})
time.sleep(1.3)
calls = motor_calls_since(t0)
check(any(c[1] == "stop" for c in calls), "a lone /drive command must still auto-stop on its own")

# 3. An explicit /stop shortly after /drive stops the car immediately
# (not just eventually via the watchdog) - same background-thread
# reasoning as test 1: the /drive call must not be allowed to block the
# test (or, in production, another request) for its full watchdog sleep.
_fake_px_instance.calls.clear()
t0 = time.time()
threading.Thread(
    target=lambda: client.post("/drive", json={"speed": 40, "angle": 0})
).start()
time.sleep(0.1)
client.get("/stop")
elapsed = time.time() - t0
calls = motor_calls_since(t0)
stop_calls = [c for c in calls if c[1] == "stop"]
check(len(stop_calls) >= 1, "explicit /stop should produce a stop() call")
check(elapsed < 0.5, "explicit /stop must not be blocked waiting on the watchdog's sleep")

# Let any lingering watchdog threads from test 3 finish before the process exits.
time.sleep(1.2)

print()
if fails:
    print("FAILED CHECKS:")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("All checks passed.")
