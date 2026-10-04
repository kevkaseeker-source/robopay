# AI delivery-confirmation agent — design

Brainstormed 2026-09-17 by Kevin Ehrentraut and Gabriel Mandtler.

**Implementation status (2026-09-17):** buyer/seller wallet visibility and
the buyer-app trigger-mode toggle are built and deployed (commits
`caff4ab`, `15679ba`). `agent/delivery_agent.py` itself (the actual Claude
tool-use loop) is not built yet - selecting "KI-Agent" today creates a
real, funded order that nothing currently confirms, refundable after the
deadline. See also Gabriel Mandtler's 2026-09-17 code review, which found
several issues in the underlying Anchor program unrelated to this spec -
tracked separately, not part of this document.

## Research question

RoboPay Unit C currently triggers escrow release with a **deterministic,
hardcoded rule** in `car/car_main.py`: the camera's decoded QR string must
exactly match `BOX_QR_CODE`, optionally combined with an ultrasonic
distance window. This is one point along a spectrum of "how does a
machine prove it's allowed to release payment" designs — Unit B (the
drone) uses a GPS geofence instead of QR+distance, and x402 (HTTP 402
payment-challenge protocol) is a third, protocol-based paradigm.

The research goal is to **compare these trigger paradigms** —
condition-triggered (QR/GPS, what exists today) vs. **agent-decided**
(an LLM autonomously judging when to release funds) — as part of the
broader Machine Economy Lab research. x402 is a later extension of the
agent path (§ Future work), not part of this first build.

## What this spec covers

Building a standalone AI agent that can read the car's existing sensors
and autonomously decide to sign and send the `confirm_delivery`
transaction, running **in parallel with the existing `car_main.py`**, not
replacing it — so both trigger paradigms can be compared side by side
without risking the delivery flow that's already live and working. The
buyer picks which paradigm handles their specific order via a toggle in
the buyer app (added 2026-09-17, see § Buyer app below) — this
supersedes the original "separate test script" plan for creating
agent-targeted orders.

## Decisions made during brainstorming

- **Agent runtime:** Claude API with a tool-use loop (matches the existing
  Claude Code workflow this whole project is built through; well-documented,
  good fit for "read sensor tools, then decide to call a sign-and-send tool").
- **v1 capabilities:** read sensor/context data (QR, ultrasonic) and reason
  over it; independently sign and send the Solana transaction. x402-as-client
  (agent paying for external resources/verification) is explicitly deferred
  to a later round — v1 stays minimal.
- **Trigger condition:** the agent recognizes the QR code (via the same
  `/camera/qr` reading car_main.py already uses) as the box's code, and on
  that basis signs the escrow-release transaction itself — an LLM judgment
  call standing in for the current `qr == BOX_QR_CODE` Python comparison.
- **Relationship to `car_main.py`:** parallel, not a replacement. Own
  wallet, own escrow order, `car_main.py` keeps running the existing
  proven flow — it gets exactly one small addition (ignore orders not
  meant for it, see § Buyer app) and is otherwise unchanged.

## Architecture

A new standalone script, `agent/delivery_agent.py`, separate from
`car/car_main.py` and not started by any of the existing systemd services.

**Tools exposed to the agent (Claude tool-use):**
1. `get_qr()` — reads `/camera/qr` (reuses the same endpoint `car_main.py`
   already polls, e.g. via `PICAR_SERVER_URL` or the seller-app proxy)
2. `get_distance()` — reads `/ultrasonic`, same pattern
3. `sign_confirm_delivery(actual_lat, actual_lon)` — wraps the **existing**
   `SolanaClient.confirm_delivery()` from `rpi/solana_client.py` unchanged;
   this spec adds no new signing logic, only a new caller

**System prompt (sketch):** the agent is told it's monitoring a delivery
robot's camera/sensor feed and must decide, using its own judgment, when
the delivery is genuinely confirmed — then call the sign tool. No fixed
"call the tool the instant the string matches" instruction — the point of
the experiment is that the agent is *reasoning*, not pattern-matching.

**Loop:** poll `get_qr()`/`get_distance()` → feed results to Claude →
Claude either asks for more reads or calls `sign_confirm_delivery` →
script exits (or logs and idles) once signed.

## Important wrinkle: the escrow PDA is per-operator

`anchor/programs/drone-delivery/src/lib.rs` derives the escrow account as
`seeds = [b"escrow", drone_operator.key().as_ref()]` — **one escrow slot
per operator pubkey.** The existing flow always creates orders against the
fixed `OPERATOR_PUBKEY` (`car_main.py`'s operator, `7Viz...hXia`). For the
agent to have "its own escrow" to sign against, a delivery order has to be
created with the **agent's own pubkey** passed as `drone_operator` in
`create_delivery`. Resolved by the buyer app toggle below, which picks
the operator pubkey per order based on the buyer's choice.

## Buyer app: trigger-mode toggle

`car/buyer_app.py`'s order form gets a second control alongside the
existing "SOL ins Escrow einzahlen" button: a choice between **"Fester
QR-Code"** (today's behavior, default) and **"KI-Agent"**.

- `POST /order` body gains a `trigger_mode` field (`"fixed"` |
  `"agent"`, defaults to `"fixed"` if omitted — keeps existing API
  callers working unchanged)
- `create_delivery(lat, lon, operator_pubkey)` gets parameterized by
  operator pubkey instead of always using `common.operator_pubkey` —
  passes either the existing `OPERATOR_PUBKEY` or a new
  `AGENT_OPERATOR_PUBKEY` (env var, § Wallet / funding) depending on
  `trigger_mode`
- `_active_order` gains a `trigger_mode` field, so `/active_order`
  (already polled by both `car_main.py` and the new
  `agent/delivery_agent.py`) tells each consumer whether an order is
  meant for it
- **Both consumers filter client-side on the same shared endpoint** —
  no new API surface. `car_main.py` gets one small addition: only act
  when `trigger_mode == "fixed"`, otherwise keep waiting (mirrors what
  `delivery_agent.py` does for `"agent"`). This is the one necessary
  touch to `car_main.py` — everything else about it stays as-is.
- Only one order in flight at a time overall, same as today (not one
  slot per mode) — keeps `_active_order`'s bookkeeping simple for v1;
  running one QR-mode and one Agent-mode order concurrently is a
  possible future extension, not needed for the first comparison.

## Seller/owner app: show both operator wallets

`car/seller_app.py`'s `/wallet` currently shows only the payout wallet
(`SELLER_PUBKEY`, the PiCarOwner) — there's no visibility into either
operator wallet at all today. Once there are two operator wallets
(fixed-QR's existing one, the agent's new one), Kevin wants to be able
to tell them apart in this app, not just infer it from which path fired.

- `/wallet` response gains two more read-only entries: the fixed-QR
  operator's pubkey + balance, and the agent operator's pubkey +
  balance (both already public info — reads the same way
  `get_balance_sol()` already reads `SELLER_PUBKEY`, no new capability,
  no private keys touched)
- The order status / transaction views also show which `trigger_mode`
  the active or most recent order used, so it's visible which wallet
  is "live" for what's currently happening — not just three balances
  with no way to tell which one is relevant right now
- Still fully read-only, matching this app's existing "never touches
  private keys" design — same as the rest of `seller_app.py`

## Wallet / funding

New, dedicated **agent operator keypair**, generated fresh (not reusing
`picarx_operator.json`) — makes the two paths cleanly distinguishable
on-chain (separate pubkeys in transaction history) and means a bug in the
experimental agent path can't touch the funds/state of the working system.
Needs its own small Devnet SOL balance for transaction fees (faucet).

## Testing

Run the agent against the **real, already-live sensors** (via
`PICAR_SERVER_URL` / the seller-app proxy, same endpoints `car_main.py`
uses) — no need to mock sensor data, the hardware is already exposed over
HTTP and reachable. Compare: does the agent sign at a materially different
moment than the deterministic rule would have, on the same physical QR/
distance sequence? That comparison — timing, false-positive/negative
judgment, reasoning trace — is the actual research output.

## Out of scope for this spec (future work)

- x402 as a tool the agent pays through (e.g., paying an external
  verification/oracle service before it trusts its own read enough to
  sign) — the natural next round once v1's baseline agent-vs-rule
  comparison exists.
- Deciding where the agent ultimately "lives" on the PiCarX itself
  (on-device vs. cloud-side) — explicitly deferred per Kevin: build the
  agent first, decide PiCarX placement afterward.
- Running both trigger modes concurrently (one shared "slot" for now,
  see § Buyer app).
- Any change to the Anchor program itself — not needed for this spec.
  `car_main.py`, `buyer_app.py`, and `seller_app.py` get the small,
  specific changes described above, not a rewrite.
