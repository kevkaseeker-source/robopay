#!/usr/bin/env python3
"""Offline test for picar_server.py's _clamp_duration_s().

car/picar_server.py can't be imported directly outside the real Raspberry
Pi - it does `from picarx import Picarx` / `from vilib import Vilib` at
module level and starts real camera/motor hardware as an import-time side
effect. So this test extracts just the one pure function under test from
the file's own source (via ast) and executes only that, rather than
importing the module or re-implementing the function's logic by hand.

Run: python tests/test_picar_server_duration.py
"""
import ast
import math
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parent.parent / "car" / "picar_server.py"

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


def _extract_function(name, extra_globals):
    """Compile and exec just one top-level function from picar_server.py's
    source, in a namespace pre-seeded with the module-level names it reads
    (extra_globals) - avoids importing the whole hardware-dependent module."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            mod = ast.Module(body=[node], type_ignores=[])
            code = compile(mod, filename=str(SOURCE), mode="exec")
            ns = {"math": math, **extra_globals}
            exec(code, ns)
            return ns[name]
    raise AssertionError("%s not found in %s" % (name, SOURCE))


# _clamp_duration_s reads the module-level DRIVE_DURATION_MAX_S constant -
# seed the extracted function's namespace with the real file's own default
# (5.0, confirmed by reading car/picar_server.py's DRIVE_DURATION_MAX_S
# line) so the cap this test exercises matches production, not a guess.
_clamp_duration_s = _extract_function("_clamp_duration_s", {"DRIVE_DURATION_MAX_S": 5.0})

# 1. Normal, in-range values pass through unchanged.
check(_clamp_duration_s(2.0) == 2.0, "normal: 2.0 should pass through unchanged")
check(_clamp_duration_s("3.5") == 3.5, "normal: numeric string should parse")

# 2. Values above the cap are clamped to the cap, not rejected outright.
check(_clamp_duration_s(999999) == 5.0, "clamp: huge finite value should clamp to 5.0")

# 3. Omitted/zero duration defaults to the safety cap, NOT indefinite driving
# (changed 2026-10-03: a flaky connection meant the web UI's release-side
# /stop could simply never arrive, leaving the car driving forever under
# the old "0 = no auto-stop" semantics - see picar_server.py's comment on
# _clamp_duration_s for the incident this fixes).
check(_clamp_duration_s(0) == 5.0, "zero: should default to the safety cap, not run forever")

# 4. Non-finite values (inf, -inf, nan) also default to the safety cap.
check(_clamp_duration_s(float("inf")) == 5.0, "inf: should default to the safety cap")
check(_clamp_duration_s(float("-inf")) == 5.0, "-inf: should default to the safety cap")
check(_clamp_duration_s(float("nan")) == 5.0, "nan: should default to the safety cap")

# 5. Negative values default to the safety cap, never a negative sleep.
check(_clamp_duration_s(-5.0) == 5.0, "negative: should default to the safety cap")

# 6. Garbage (non-numeric) input degrades to the safety cap rather than raising.
check(_clamp_duration_s("not-a-number") == 5.0, "garbage: should default to the safety cap")
check(_clamp_duration_s(None) == 5.0, "None: should default to the safety cap")
check(_clamp_duration_s([1, 2]) == 5.0, "list: should default to the safety cap")

print()
if fails:
    print("FAILED CHECKS:")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("All checks passed.")
