"""Shared activity feed for the buyer page and both car-owner pages.

Every page shows the same history of the whole system:
  * deliveries (Solana): "Auto bestellt" (buyer pays into the escrow) and
    "Escrow released" (payout to the car owner) or a refund, grouped per
    delivery - built from order_state.json's tx_history;
  * wallet transfers: SOL sent (Solana), PEAQ sent and the Machine-NFT sent
    (peaq) - stored in activity_log.json.

Transfers are reported by the browser wallet (which signs them locally) or by
the buyer app's own wallet. A report only carries the chain, the kind and the
transaction id: the server looks the transaction up on chain and takes from,
to and amount from there, so a page can't invent entries.

The public addresses of CarOwner A and B are stored here too (the browser
registers them when a wallet is set up), so every viewer sees the names.
"""
import json
import re
import threading
import time
from pathlib import Path

import requests

LOG_FILE = Path(__file__).parent / "activity_log.json"
SOLANA_RPC = "https://api.devnet.solana.com"
PEAQ_RPC = "https://peaq.api.onfinality.io/public"
MACHINE_REGISTRY = "0x64b93cc29b251fafa83bd110cdb1c24207f85536"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

SOL_SIG = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{64,90}$")
SOL_ADDR = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
EVM_TX = re.compile(r"^0x[0-9a-fA-F]{64}$")
EVM_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")

_lock = threading.Lock()


class Rejected(ValueError):
    """A reported transaction could not be confirmed on chain."""


# ----------------------------------------------------------------- storage
def _load():
    try:
        d = json.loads(LOG_FILE.read_text())
        return {"events": d.get("events", []), "owners": d.get("owners", {})}
    except (OSError, ValueError):
        return {"events": [], "owners": {}}


def _save(d):
    tmp = LOG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d))
    tmp.replace(LOG_FILE)


def set_owner(role, sol, evm):
    """Remember the public addresses of CarOwner A or B."""
    if role not in ("A", "B"):
        raise ValueError("role must be A or B")
    if not SOL_ADDR.match(sol or "") or not EVM_ADDR.match(evm or ""):
        raise ValueError("invalid address")
    with _lock:
        d = _load()
        d["owners"][role] = {"sol": sol, "evm": evm}
        _save(d)


def add_event(event):
    """Store a verified transfer; the same transaction is stored only once."""
    with _lock:
        d = _load()
        if any(e.get("tx") == event["tx"] for e in d["events"]):
            return False
        d["events"].append(event)
        _save(d)
        return True


# ----------------------------------------------------------------- on-chain lookups
def _rpc(url, method, params):
    r = requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=20)
    r.raise_for_status()
    body = r.json()
    if body.get("error"):
        raise Rejected(str(body["error"].get("message", body["error"])))
    return body.get("result")


def verify_peaq(kind, tx, rpc=_rpc):
    """PEAQ transfer or Machine-NFT transfer, read back from peaq."""
    if not EVM_TX.match(tx or ""):
        raise Rejected("not a peaq transaction hash")
    receipt = rpc(PEAQ_RPC, "eth_getTransactionReceipt", [tx])
    if not receipt or receipt.get("status") != "0x1":
        raise Rejected("transaction not found or failed on peaq")
    if kind == "peaq":
        t = rpc(PEAQ_RPC, "eth_getTransactionByHash", [tx])
        value = int(t.get("value") or "0x0", 16)
        if value <= 0 or not t.get("to"):
            raise Rejected("not a PEAQ transfer")
        return {"from": _evm(t["from"]), "to": _evm(t["to"]), "amount": value / 1e18}
    if kind == "nft":
        for log in receipt.get("logs", []):
            topics = log.get("topics", [])
            if (log.get("address", "").lower() == MACHINE_REGISTRY and len(topics) == 4
                    and topics[0].lower() == TRANSFER_TOPIC):
                return {"from": _evm("0x" + topics[1][-40:]), "to": _evm("0x" + topics[2][-40:]),
                        "token_id": str(int(topics[3], 16))}
        raise Rejected("no Machine-NFT transfer in this transaction")
    raise Rejected("unknown kind")


def verify_solana(tx, rpc=_rpc):
    """Plain SOL transfer, read back from Solana devnet."""
    if not SOL_SIG.match(tx or ""):
        raise Rejected("not a Solana signature")
    t = rpc(SOLANA_RPC, "getTransaction", [tx, {"encoding": "jsonParsed", "commitment": "confirmed",
                                              "maxSupportedTransactionVersion": 0}])
    if not t or (t.get("meta") or {}).get("err") is not None:
        raise Rejected("transaction not found or failed on Solana")
    for ix in t["transaction"]["message"]["instructions"]:
        p = ix.get("parsed") or {}
        if ix.get("program") == "system" and p.get("type") == "transfer":
            info = p["info"]
            return {"from": info["source"], "to": info["destination"], "amount": info["lamports"] / 1e9}
    raise Rejected("no SOL transfer in this transaction")


def _evm(a):
    """Checksum-free canonical form for comparisons (lowercase)."""
    return a.lower()


def record(chain, kind, tx, rpc=_rpc):
    """Verify a reported transfer on chain and store it. Returns the event."""
    if chain == "peaq" and kind in ("peaq", "nft"):
        details = verify_peaq(kind, tx, rpc)
    elif chain == "solana" and kind == "sol":
        details = verify_solana(tx, rpc)
    else:
        raise Rejected("unknown chain/kind")
    event = {"chain": chain, "kind": kind, "tx": tx, "t": time.time(), **details}
    add_event(event)
    return event


# ----------------------------------------------------------------- the feed
def deliveries(tx_history, active_order=None, default_buyer=None):
    """Group tx_history into deliveries: ordered (escrow paid in), then
    released or refunded. Numbered in order, newest first."""
    out, by_escrow = [], {}
    for e in sorted(tx_history, key=lambda e: float(e.get("t") or 0)):
        kind = e.get("type")
        if kind == "create_delivery":
            d = {"n": len(out) + 1, "ordered_at": float(e.get("t") or 0), "order_tx": e.get("sig"),
                 "buyer": e.get("buyer") or default_buyer, "payee": e.get("seller"),
                 "status": "pending", "release_tx": None, "released_at": None,
                 "refund_tx": None}
            out.append(d)
            by_escrow[e.get("sig")] = d
        elif kind in ("confirm_delivery", "cancel_delivery"):
            d = by_escrow.get(e.get("escrow_tx"))
            if d is None:  # older entries carry no link: the last open delivery
                d = next((x for x in reversed(out) if x["status"] == "pending"), None)
            if d is None:
                continue
            if kind == "confirm_delivery":
                d.update(status="delivered", release_tx=e.get("sig"), released_at=float(e.get("t") or 0))
            else:
                d.update(status="refunded", refund_tx=e.get("sig"), released_at=float(e.get("t") or 0))
    if active_order and active_order.get("escrow_tx") in by_escrow:
        d = by_escrow[active_order["escrow_tx"]]
        d["payee"] = d["payee"] or active_order.get("seller")
        d["buyer"] = d["buyer"] or active_order.get("buyer_pubkey")
        if d["status"] == "pending" and active_order.get("status") in ("delivered", "refunded"):
            d["status"] = active_order["status"]
    # Every delivery before the newest one is over: an order that never got a
    # release/refund entry (e.g. history cleared mid-way) is shown as closed.
    for d in out[:-1]:
        if d["status"] == "pending":
            d["status"] = "closed"
    return list(reversed(out))


def feed(tx_history, active_order, buyer_pubkey, operator_pubkey):
    d = _load()
    events = sorted(d["events"], key=lambda e: e.get("t", 0), reverse=True)
    return {
        "deliveries": deliveries(tx_history, active_order, buyer_pubkey),
        "sol_transfers": [e for e in events if e["chain"] == "solana"],
        "nft_transfers": [e for e in events if e["kind"] == "nft"],
        "peaq_transfers": [e for e in events if e["kind"] == "peaq"],
        "owners": d["owners"],
        "buyer": buyer_pubkey,
        "operator": operator_pubkey,
        "now": time.time(),
    }


# ----------------------------------------------------------------- HTTP routes
def register_routes(app, state_fn, operator_pubkey, buyer_pubkey_fn=lambda active: None):
    """GET /activity (the feed), POST /activity (report a transfer for
    on-chain verification), POST /activity/owner (CarOwner A/B addresses).
    Same routes on the buyer and the seller app, both behind their login."""
    from flask import jsonify, request

    @app.route("/activity")
    def activity_feed():
        active, history = state_fn()
        buyer = buyer_pubkey_fn(active) or (active or {}).get("buyer_pubkey")
        return jsonify(feed(history, active, buyer, operator_pubkey))

    @app.route("/activity", methods=["POST"])
    def activity_report():
        body = request.get_json(silent=True) or {}
        try:
            event = record(str(body.get("chain")), str(body.get("kind")), str(body.get("tx", "")).strip())
        except Rejected as e:
            return jsonify({"success": False, "error": str(e)}), 400
        except requests.RequestException as e:
            return jsonify({"success": False, "error": f"chain lookup failed: {e}"}), 502
        return jsonify({"success": True, "event": event})

    @app.route("/activity/owner", methods=["POST"])
    def activity_owner():
        body = request.get_json(silent=True) or {}
        try:
            set_owner(str(body.get("role")), str(body.get("sol", "")), str(body.get("evm", "")))
        except ValueError as e:
            return jsonify({"success": False, "error": str(e)}), 400
        return jsonify({"success": True})
