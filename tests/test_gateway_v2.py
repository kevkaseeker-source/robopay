#!/usr/bin/env python3
"""Offline test for car/gateway_app.py: the submitted /buyer/ and /seller/
still reach their original apps, and /v2/buyer/ and /v2/seller/ reach the
separate Part-2 processes with their index pages rewritten to the v2 prefix.

Run: python tests/test_gateway_v2.py
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "car"))
import gateway_app  # noqa: E402

fails, calls = [], []


def check(cond, msg):
    if not cond:
        fails.append(msg)


class FakeUpstream:
    status_code = 200
    headers = {"Content-Type": "text/html"}
    content = b"<script>fetch('/status')</script><img src=\"/qrcode.png\">"


def fake_request(method, url, **kw):
    calls.append(url)
    return FakeUpstream()


gateway_app.requests.request = fake_request
c = gateway_app.app.test_client()

for path, upstream, prefix in (("/buyer/", ":5001/", "/buyer"), ("/seller/", ":5002/", "/seller"),
                               ("/v2/buyer/", ":5011/", "/v2/buyer"), ("/v2/seller/", ":5012/", "/v2/seller")):
    calls.clear()
    body = c.get(path).get_data(as_text=True)
    check(calls and calls[0].endswith(upstream), f"{path} must reach {upstream}, got {calls}")
    check(f"fetch('{prefix}/status')" in body and f'src="{prefix}/qrcode.png"' in body,
          f"{path} index must be rewritten to {prefix}, got {body}")

calls.clear()
c.post("/v2/buyer/order", json={})
check(calls == ["http://localhost:5011/order"], f"v2 API call routed wrong: {calls}")

# Landing page: one system - Buyer + two separate car owners on the familiar paths.
home = c.get("/").get_data(as_text=True)
links = re.findall(r'href="([^"]+)"', home)
check(links == ["/buyer/", "/seller/?owner=A", "/seller/?owner=B"], f"landing links: {links}")

# ?owner=A reaches the v2 seller with the query string intact.
calls.clear()
captured = {}
def fake_request_kw(method, url, **kw):
    calls.append(url); captured.update(kw); return FakeUpstream()
gateway_app.requests.request = fake_request_kw
c.get("/v2/seller/?owner=B")
check(calls and calls[0].endswith(":5012/") and captured.get("params", {}).get("owner") == "B",
      f"?owner=B must be forwarded to the v2 seller: {calls} {captured.get('params')}")

print()
if fails:
    print("FAILED CHECKS:")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("All checks passed.")
