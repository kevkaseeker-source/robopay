#!/usr/bin/env python3
"""Offline test for the time-limited jury login in robopay_common.make_auth.

A second Basic Auth account (JURY_USERNAME / JURY_PASSWORD) is accepted only
up to and including JURY_EXPIRES (YYYY-MM-DD). The team's own login keeps
working independently, and a missing or malformed expiry disables the jury
login instead of making it permanent.

Run: python tests/test_jury_login.py
"""
import base64
import datetime as dt
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "car"))

from flask import Flask  # noqa: E402

import robopay_common as common  # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


def basic(user, pw):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def make_client(expires):
    for k in ("JURY_USERNAME", "JURY_PASSWORD", "JURY_EXPIRES"):
        os.environ.pop(k, None)
    os.environ["T_USER"], os.environ["T_PASS"] = "team", "team-secret"
    if expires is not None:
        os.environ["JURY_USERNAME"], os.environ["JURY_PASSWORD"] = "jury", "jury-secret"
        os.environ["JURY_EXPIRES"] = expires
    app = Flask(__name__)
    common.make_auth(app, "T_USER", "T_PASS", exempt_paths=("/machine",))

    @app.route("/")
    def index():
        return "ok"

    @app.route("/machine")
    def machine():
        return "machine"

    return app.test_client()


today = dt.date.today()
future = (today + dt.timedelta(days=30)).isoformat()
past = (today - dt.timedelta(days=1)).isoformat()

# 1. Valid jury login, team login unaffected, exempt path still exempt.
c = make_client(future)
check(c.get("/", headers=basic("jury", "jury-secret")).status_code == 200, "valid jury login rejected")
check(c.get("/", headers=basic("team", "team-secret")).status_code == 200, "team login broken by jury login")
check(c.get("/", headers=basic("jury", "wrong")).status_code == 401, "wrong jury password accepted")
check(c.get("/", headers=basic("team", "jury-secret")).status_code == 401, "mixed credentials accepted")
check(c.get("/").status_code == 401, "no credentials accepted")
check(c.get("/machine").status_code == 200, "exempt path now needs a login")

# 2. Expiry day itself still works.
c = make_client(today.isoformat())
check(c.get("/", headers=basic("jury", "jury-secret")).status_code == 200, "jury login rejected on its last valid day")

# 3. Expired jury login is rejected; team login still works.
c = make_client(past)
check(c.get("/", headers=basic("jury", "jury-secret")).status_code == 401, "expired jury login accepted")
check(c.get("/", headers=basic("team", "team-secret")).status_code == 200, "team login broken after jury expiry")

# 4. Missing or malformed expiry disables the jury login (fail closed).
for bad in ("", "soon", "2026-13-45"):
    c = make_client(bad)
    check(c.get("/", headers=basic("jury", "jury-secret")).status_code == 401,
          "jury login accepted with JURY_EXPIRES=%r" % bad)

# 5. No jury env at all: behaves exactly as before.
c = make_client(None)
check(c.get("/", headers=basic("jury", "jury-secret")).status_code == 401, "jury login accepted without JURY_* env")
check(c.get("/", headers=basic("team", "team-secret")).status_code == 200, "team login broken without JURY_* env")

print()
if fails:
    print("FAILED CHECKS:")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("All checks passed.")
