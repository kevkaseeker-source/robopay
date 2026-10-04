# Sensor data collection during delivery

Sub-project 2 of 3 from the "RoboPay dApp" brainstorm (2026-09-23). The other
two (tokenized ownership, see
`docs/superpowers/specs/2026-09-23-tokenized-ownership-design.md`; image-
labeling tool, not yet started) are separate, independently-scoped
sub-projects and are not covered here.

## Why

Kevin's framing: while Unit C drives toward a delivery target, it should
periodically capture a camera frame paired with the ultrasonic distance
reading at that moment, and keep those pairs somewhere central rather than
losing them. Purpose is deliberately left open (possible future AI
training, per Kevin's own comparison to Pokémon GO's crowdsourced visual-
positioning data collection) rather than scoped to one specific downstream
use — this spec covers collecting and viewing the data, not what it gets
used for later.

Today, neither delivery path persists anything centrally: the fixed-QR path
(`car/car_main.py`, the one actually in live use) saves no images or sensor
readings at all; the AI-agent path (`agent/delivery_agent.py`, not
currently deployed) saves frames per-run to local disk on wherever it runs,
uncollected and not even in `.gitignore`. Neither gives a standing, central
record of what the robot saw across deliveries.

## Scope for this PoC

**In scope:**
- Periodic capture (camera frame + ultrasonic distance, paired) at a fixed
  11-second interval.
- Capture runs only while a delivery is active (order created, not yet
  confirmed) — matches the window `car/car_main.py` and the shared order
  state already track today. No capture when idle.
- Central storage on the StaexHosting server (the same VPS already hosting
  `buyer_app.py`/`seller_app.py`/`gateway_app.py`), not on the RPi.
- A simple gallery view in the existing Seller/Owner web app: per delivery,
  a list of captured frames with each one's ultrasonic value and timestamp
  alongside it.
- A **delivery register**: every completed delivery, most recent first,
  each row linking to (a) that delivery's captured sensor data (the gallery
  above) and (b) a Solana Explorer link for the on-chain `confirm_delivery`
  transaction — the payment proof. This already exists today (Seller app's
  `/transactions` route + its transaction table) except for the gallery
  link, which is the actual new piece — see "Delivery register" below.
- Reuses the Seller app's existing HTTP Basic Auth — no new auth system.

**Explicitly out of scope (documented, not built):**
- **Any downstream AI-training pipeline.** This spec only collects and
  displays the data; labeling, dataset export, and training are separate
  concerns (labeling is sub-project 3 of the original brainstorm; training
  itself isn't scoped anywhere yet).
- **Retention/cleanup policy.** Captured files accumulate on StaexHosting
  indefinitely for now. Fine for a PoC's data volume; a real policy (age-
  based deletion, storage quota, archiving) is future work once actual
  usage shows what's needed.
- **Capturing during the AI-agent delivery path.** That path isn't deployed
  today and doesn't share the same central order-state mechanism this
  design hooks into (`car/robopay_common.py`'s `load_state()`, used by
  `car_main.py`/`seller_app.py`/`buyer_app.py`). If/when the AI-agent path
  is deployed, wiring it into the same collector is a small follow-up, not
  part of this PoC.
- **Changing `car/car_main.py` at all.** Deliberate: that file runs live on
  physical hardware and carries several fragile, hard-won fixes (see
  `car/SETUP_AND_ARCHITECTURE.md` §14). This design reaches the robot's
  sensors the same way `seller_app.py` already does today for its live
  camera/ultrasonic view — over the existing mesh proxy calls — so the car
  and its own control loop are untouched.
- **Changing the existing `/clear_history` behavior.** `car/buyer_app.py`
  already has a `/clear_history` route that wipes `tx_history` (used
  elsewhere, e.g. demo resets) — the Register (below) is built directly on
  top of that same list, so calling it also empties the Register. This is
  pre-existing behavior this design does not change; documented here as a
  known limitation, not fixed.

## Delivery register

A new top-level view (`/seller/register`) lists every completed delivery,
most recent first, each row showing:
- delivery timestamp,
- a link to that delivery's captured sensor data (the gallery above,
  `/seller/gallery/<escrow_tx>`),
- a Solana Explorer link for the on-chain payment proof
  (`https://explorer.solana.com/tx/{sig}?cluster=devnet`, the same URL
  pattern `car/car_main.py` already logs today via `DEVNET_EXPLORER`), using
  the `confirm_delivery` signature.

Built directly on `car/robopay_common.py`'s existing `tx_history` list
(`load_state()` already returns it; `buyer_app.py`'s `/delivered` handler
already appends a `{"type": "confirm_delivery", "sig": ..., "t": ...}`
entry on every completed delivery — this pre-dates this spec). That
existing entry does not carry the order's `escrow_tx`, only the confirm
signature, so it cannot be joined to this design's sensor-data captures
(which are tagged by `escrow_tx`) as-is. Minimal fix: add `escrow_tx` to
that one `_tx_history.append(...)` call in `buyer_app.py` (the active
order's `escrow_tx` is already in scope at that point in the code) — a
one-line, purely additive change, no existing behavior altered.

**Correction found while re-reading `seller_app.py` for the implementation
plan:** a delivery register already exists and does not need to be built.
`seller_app.py`'s `/transactions` route already filters `tx_history` for
`confirm_delivery` entries and returns them newest-first; its `INDEX_HTML`
already renders that list as a table with a Solana Explorer link per row
(`pollTx()`). Once the one-line `escrow_tx` addition above lands, that
field flows through `/transactions` automatically (the route just
serializes whatever's in each `tx_history` entry — no route code change
needed). The only actual gap is a gallery link: `pollTx()`'s row template
gets one added line linking to `/gallery/<tx.escrow_tx>` when that field is
present. No new `/register` route, no new backend endpoint for this part —
just a small JS template change to a table that already exists.

## Data flow

```
Buyer creates order (unchanged)
        |
car/robopay_common.py's shared state: active order set
        |
NEW: background thread in seller_app.py, every 11s while an order is active:
        GET {PICAR_SERVER_URL}:8080/debug/frame   (same call seller_app.py
        GET {PICAR_SERVER_URL}:8080/ultrasonic     already makes today)
        |
        save frame + ultrasonic value + timestamp, tagged by escrow_tx,
        to car/sensor_data/<escrow_tx>/<timestamp>.jpg (+ a paired record)
        on the StaexHosting filesystem
        |
Delivery confirmed -> buyer_app.py's /delivered handler:
   - active order clears -> capture stops
   - tx_history gets a confirm_delivery entry, NOW including escrow_tx
     (one-line addition to existing code)
        |
seller_app.py's EXISTING /transactions route and INDEX_HTML table
   (pollTx()) now show a gallery link per row, since escrow_tx flows
   through automatically once the buyer_app.py addition lands
        |
NEW: /seller/gallery/<escrow_tx> lists that delivery's captured pairs
     (proxied automatically through the existing gateway /seller/ route,
     protected by the same HTTP Basic Auth as the rest of the Seller app)
```

## Components touched

- **New: `car/sensor_logger.py`** — the capture loop itself, kept separate
  from `seller_app.py` so that file doesn't grow unbounded. Exposes a
  `start(app_state)`-style entry point that `seller_app.py` calls once at
  startup to launch the background thread. Responsible for: polling
  `common.load_state()` for the active order, timing the 11-second
  interval, fetching frame + ultrasonic from the RPi, and writing both to
  disk under `car/sensor_data/<escrow_tx>/`.
- **Modified: `car/seller_app.py`** — starts `sensor_logger`'s background
  thread at app init (a few lines, mirroring how the app already starts
  today); adds one new route, `/gallery/<escrow_tx>` (becoming
  `/seller/gallery/<escrow_tx>` through the existing gateway proxy — no
  gateway change needed, since `gateway_app.py`'s `/seller/<path:subpath>`
  route already catches anything under `/seller/`). `INDEX_HTML`'s existing
  `pollTx()` function gets one added line in its row template: a gallery
  link when `tx.escrow_tx` is present. The existing `/transactions` route
  itself needs no code change — it already serializes whatever fields are
  in each `tx_history` entry, so `escrow_tx` flows through once
  `buyer_app.py` starts including it.
- **Modified: `car/buyer_app.py`** — one-line addition to the existing
  `_tx_history.append({"type": "confirm_delivery", ...})` call in the
  `/delivered` handler, adding `"escrow_tx": _active_order.get("escrow_tx")`
  so the existing transaction table can link to a delivery's sensor-data
  captures. No other change to this file; its own routes/behavior are
  otherwise untouched.
- **Not touched:** `car/car_main.py`, `car/gateway_app.py`, anything on the
  RPi itself. The capture side of this feature lives entirely on the
  StaexHosting side, reusing the mesh-proxy pattern `seller_app.py`'s
  existing `/proxy/ultrasonic` and `/proxy/qr` routes already establish.

## Error handling

- **RPi/mesh unreachable during a capture attempt:** log and skip that
  cycle; try again at the next 11-second tick. The background thread must
  never crash or take down `seller_app.py` — matches how `seller_app.py`'s
  existing proxy routes already handle RPi connectivity failures (return an
  error response for that one request, keep running).
- **No active order:** the thread simply does nothing that cycle — not an
  error condition.
- **Order changes between cycles:** each cycle reads the current order
  fresh from `common.load_state()` and tags that cycle's capture with
  whatever escrow_tx is active *then* — no cross-cycle state to go stale.
- **Disk fills up on StaexHosting:** out of scope for this PoC (see
  Scope above) — not handled, a known limitation.
- **A `tx_history` entry predates the `escrow_tx` field** (any
  `confirm_delivery` entry recorded before this change ships): the existing
  transaction table still shows the row (timestamp + Explorer link) exactly
  as it does today, just without a gallery link — never errors or hides the
  entry outright.

## Testing

- Offline, following `agent/selftest.py`'s existing stub-harness pattern:
  fake `/debug/frame`/`/ultrasonic` HTTP responses and a fake
  `common.load_state()`, verifying the logger captures at the right cadence
  only while an order is active, tags files with the correct escrow_tx, and
  skips cleanly on a simulated RPi-unreachable response. No live hardware
  needed.
- A small test for the new `/gallery/<escrow_tx>` route against a
  pre-seeded set of fake captured files, checking it lists them correctly.
- A small test for the existing `/transactions` route confirming `escrow_tx`
  is now present in its response once `buyer_app.py`'s fix lands, and that
  its existing filtering behavior (only `confirm_delivery` entries, newest
  first) is unchanged.
- A focused test for `buyer_app.py`'s `/delivered` handler confirming the
  new `escrow_tx` field is actually included in the appended `tx_history`
  entry, and that everything else about that handler's existing behavior
  (order status, `delivery_tx`, the `dry-run-tx` exclusion) is unchanged.
- Live end-to-end testing (real captures against the physical robot) is
  blocked on the car being powered on and reachable — not attempted as part
  of writing this spec (car is currently off).

## Dependencies

None on other sub-projects or on the blocked peaq `CrossChainMirror` issue
— this is independent of sub-project 1 (tokenized ownership) and can be
implemented and tested (offline) regardless of that blocker.
