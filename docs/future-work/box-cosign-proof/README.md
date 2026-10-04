# Proposal: proof of delivery by challenge-response and a box co-signature (2-of-2)

Status: **proposal, not implemented.** Drafted 2026-10-03 for review by Kevin and Xinyan.
It builds on the 2-of-2 idea already planned in the paper, Section 7.6.

## 1. Problem: what the chain verifies today

Read from `anchor/programs/drone-delivery/src/lib.rs` and `car/car_main.py`:

- `confirm_delivery` is signed by **one** key, the carrier (`drone_operator`).
- The on-chain position check compares the submitted position with the order's own
  target. The car submits that target itself, so the check cannot fail either way.
- The QR code is a **fixed string** (`BOX_QR_CODE`, default `ROBOPAY-BOX-C`). The car
  compares it locally. Anyone who knows or photographs the code can reproduce it.
- GPS is self-reported by the same device that gets paid, is accurate to only a few
  metres, and can be spoofed.

So the program trusts the carrier's own claim that the delivery happened.

## 2. Proposal

The box (Unit A) becomes an independent second signer, and the proof is fresh for every
delivery.

1. At order creation the escrow records the box's public key (`box`).
2. When the car arrives, the box generates a random one-time `nonce` (valid about 60 s)
   and shows it as a QR code on a small display, or on the buyer's phone.
3. The car reads the code with its camera and builds the `confirm_delivery` transaction
   with `proof = hash(nonce || escrow PDA || car pubkey)`. The car signs it.
4. The car sends the partly signed transaction to the box over the existing Staex MCC
   tunnel.
5. The box checks: nonce known and not expired, `proof` matches, parcel sensor confirms
   a parcel was deposited (weight cell and door switch). Only then does it co-sign and
   submit.
6. `confirm_delivery` requires **both** signatures: `drone_operator` and `box`
   (equal to `escrow.box`).

Existing timeout and refund path stays: if nobody confirms before the deadline, the
buyer can `cancel_delivery`.

## 3. On-chain changes

- `DeliveryEscrow`: add `box: Pubkey`; set in `create_delivery`.
- `ConfirmDelivery` accounts: add `box_signer: Signer`, constraint `== escrow.box`.
- Optional: store `proof: [u8; 32]` in the escrow or emit it as an event, for audit.
- The existing bounding-box check may stay as a sanity check but is no longer the proof.
- Redeploy together with the `close_escrow` fix of 2026-10-02 (not yet on devnet).

## 4. Hardware and software

- Box: small display (e-ink or LCD), nonce service, existing weight cell (HX711) and
  door switch as the physical gate before co-signing.
- Car: existing camera and QR decoder; add transaction building and signing with the
  nonce proof; send to box.
- No new radios are needed for the first version.

## 5. Security view

| Attacker | Result |
|---|---|
| Copies or photographs the QR code | Useless, the nonce is one-time and expires |
| Spoofs GPS | Irrelevant, GPS is no longer the proof |
| Carrier alone claims "delivered" | Fails, the box signature is missing |
| Seller or carrier without access to the box | Cannot produce the box signature |
| Buyer refuses to let the box sign | No payout; refund after the deadline |
| Relay of the code from far away | Still possible in theory. Mitigation later: distance-bounding radio (UWB or NFC) |
| Compromised box key | Breaks the guarantee. Mitigation: keep the key on the box, rotate per order |

Honest limit: the box owner (the buyer side) is trusted not to co-sign false deliveries
for their own money, which is aligned with their incentives. Disputes need the AI photo
check and the x402 second opinion as an extra layer, not as the main proof.

## 6. Tests to write first

Offline self-tests with stubs, then devnet: replayed nonce refused, expired nonce
refused, wrong car key refused, missing box signature refused, parcel sensor not
triggered refused, happy path pays the seller once, deadline refund still works.

## 7. Open questions

- Who generates and holds the box key, and how is it registered per order?
- Display on the box or on the buyer's phone?
- Does the box submit the transaction, or the car after receiving the box signature?
- Which trust lines stay in the paper (Section 7.6, Chapter 12)?
