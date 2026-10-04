#!/usr/bin/env python3
"""Single-port gateway in front of buyer_app.py (5001) and seller_app.py
(5002) - needed because this Staex Hosting server can only have ONE
published public web address at a time (mcp__staex__publish_site:
"Calling this again with a different name or port replaces the current
address"), but we have two apps that both need to be publicly reachable.
Mounts them at /buyer/ and /seller/, each keeping its own HTTP Basic Auth
(just forwards whatever Authorization header the browser sent - the
upstream apps still do the real check).

Only the index page ("/") of each upstream app needs path rewriting: its
HTML contains root-relative fetch()/img-src calls like fetch('/order')
that would otherwise resolve against the gateway's own root instead of
/buyer/ or /seller/. Every other route (JSON/binary API responses, the
MJPEG stream) is a plain passthrough - nothing in those responses
references a path.

Run:
    venv/bin/python3 gateway_app.py
"""

import os
import re

import requests
from flask import Flask, Response, request, stream_with_context

PORT = int(os.getenv("GATEWAY_PORT", "8000"))
BUYER_UPSTREAM = "http://localhost:5001"
SELLER_UPSTREAM = "http://localhost:5002"
VERIFIER_UPSTREAM = "http://localhost:5003"

app = Flask(__name__)


def _rewrite_index(html: bytes, prefix: str) -> bytes:
    text = html.decode("utf-8")
    # only touches root-relative references inside fetch(...)/src=... -
    # generic on purpose so new endpoints added later don't need a matching
    # gateway change.
    text = re.sub(r"(fetch\(['\"])/", rf"\1{prefix}/", text)
    text = re.sub(r'(src=["\'])/', rf"\1{prefix}/", text)
    return text.encode("utf-8")


def _proxy(upstream_base: str, subpath: str, rewrite_prefix: str = None):
    url = f"{upstream_base}/{subpath}"
    headers = {k: v for k, v in request.headers if k.lower() not in ("host", "content-length")}
    try:
        upstream = requests.request(
            request.method, url, headers=headers, data=request.get_data(),
            params=request.args, stream=True, timeout=15,
        )
    except Exception as e:
        return Response(f"Gateway error: {e}", status=502)

    content_type = upstream.headers.get("Content-Type", "")
    is_stream = "multipart/x-mixed-replace" in content_type

    if is_stream:
        return Response(
            stream_with_context(upstream.iter_content(chunk_size=4096)),
            status=upstream.status_code, content_type=content_type,
        )

    body = upstream.content
    if rewrite_prefix and (subpath == "" or subpath == "/") and "text/html" in content_type:
        body = _rewrite_index(body, rewrite_prefix)

    resp_headers = [(k, v) for k, v in upstream.headers.items()
                    if k.lower() not in ("content-length", "content-encoding", "transfer-encoding", "connection")]
    return Response(body, status=upstream.status_code, headers=resp_headers)


@app.route("/")
def root():
    return """<!doctype html>
<html><head><meta name="viewport" content="width=device-width, initial-scale=1">
<title>RoboPay</title>
<style>body{background:#111;color:#eee;font-family:system-ui,sans-serif;padding:24px;}
a{display:block;background:#4ade80;color:#111;text-decoration:none;padding:16px;border-radius:8px;
margin-bottom:12px;font-weight:600;text-align:center;}</style></head>
<body><h1>RoboPay</h1>
<a href="/buyer/">Bestellen (Buyer)</a>
<a href="/seller/">Auto steuern (Seller/Operator)</a>
</body></html>"""


@app.route("/buyer/", defaults={"subpath": ""}, methods=["GET", "POST"])
@app.route("/buyer/<path:subpath>", methods=["GET", "POST"])
def buyer_proxy(subpath):
    return _proxy(BUYER_UPSTREAM, subpath, rewrite_prefix="/buyer")


@app.route("/seller/", defaults={"subpath": ""}, methods=["GET", "POST"])
@app.route("/seller/<path:subpath>", methods=["GET", "POST"])
def seller_proxy(subpath):
    return _proxy(SELLER_UPSTREAM, subpath, rewrite_prefix="/seller")


@app.route("/verifier/", defaults={"subpath": ""}, methods=["GET", "POST"])
@app.route("/verifier/<path:subpath>", methods=["GET", "POST"])
def verifier_proxy(subpath):
    # Pure JSON API (x402_verifier.py's /verify and /health) - no HTML index
    # to path-rewrite, unlike buyer/seller.
    return _proxy(VERIFIER_UPSTREAM, subpath)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True, debug=False, use_reloader=False)
