# Transferring Unit C's ownership (peaq Machine-NFT + Solana payout)

**When to use this:** whenever Unit C's peaq Machine-NFT changes hands - a real sale/handoff of
the robot, or moving it to a different operator's wallet. This is a **three-step, manual** process
(confirmed live, 2026-09-27, on a disposable test machine - see below for why the second step is
mandatory, not optional). Missing step 2 or 3 is safe (the code refuses to pay rather than paying
the wrong wallet - see "What happens if you skip a step" below), but the new owner won't get
paid until all three steps are done, and **skipping step 2 leaves a real security gap**, not just
a missing feature.

Machine ID: `5149596011477982620423871887556457753159696362422273317835964893685329625592`
(`robopay-picarx-unit-c`, onboarded 2026-09-26 - see
`docs/peaq-integration/native-onboarding-workaround.md`).

**Confirmed live (2026-09-27):** peaq does NOT move DID-write ("controller") rights when the NFT
is transferred, and does NOT restrict DID writes to the current NFT owner by default. The
**former** owner can still write DID entries after losing the NFT - including one that falsely
claims the new owner as its `controller`, which would fool a check that only looks at each entry's
own `controller` field. The only thing peaq actually gates to the current NFT owner is
`set_machine_controller` itself. That's why step 2 below (claiming DID control) is a hard
requirement, done by the **new** owner, immediately after step 1, before step 3 or anything else -
see `docs/peaq-integration/native-onboarding-workaround.md`'s "Resolved" note for the full test
writeup.

## Why three steps

Unit C's identity (the peaq Machine-NFT), its DID-write authority, and its delivery payout target
(a Solana wallet) are three separate things, on purpose:

1. **Who owns the Machine-NFT** - a plain ERC-721 on peaq's chain. This is the actual,
   ground-truth "who owns this robot" record.
2. **Who may write the DID document** - the machine-wide `controller` field on the machine's
   management state, settable only by the current NFT owner via `set_machine_controller`. This
   does **not** move automatically when the NFT transfers (confirmed live, 2026-09-27) - it stays
   with whoever last claimed it until explicitly reassigned.
3. **Which Solana wallet gets paid** - recorded as an `Ed25519VerificationKey2020` entry in the
   machine's DID document.

`rpi/peaq_ownership.py`'s `current_owner()` trusts a DID verification method only if **both**
the machine-wide `state.controller` matches the current NFT owner (step 2's gate) **and** the
verification method's own `controller` field also matches. Skipping step 2 means `state.controller`
still points at the old owner, so `current_owner()` refuses outright - it won't even look at step
3's entries, let alone trust one written by whoever currently holds DID control. This is
deliberately strict: checking only the per-entry `controller` (the original design) was proven,
via a live test, to let a **former** owner plant a fraudulent entry that still passes. Clean
failure mode, but the robot's income is effectively paused until steps 2 and 3 both happen.

## Step 1: transfer the Machine-NFT (peaq, EVM side)

The new owner needs an EVM wallet (any address; doesn't need to be funded to *receive* an NFT).

```bash
# From the CURRENT owner's peaqOS CLI environment:
peaqos machine transfer 5149596011477982620423871887556457753159696362422273317835964893685329625592 \
  0x<NEW_OWNER_EVM_ADDRESS> \
  --yes
```

This uses the safe transfer path by default (checks the recipient can actually hold an ERC-721);
don't add `--unsafe` unless you're certain the recipient address isn't a contract that can't
receive NFTs - the CLI's own help warns this can permanently strand the machine.

Add `--dry-run` first to preview without signing, same as onboarding.

## Step 2: claim DID control (peaq, EVM side, run by the NEW owner - REQUIRED, do this immediately)

**Do this before anything else, including step 3.** Until the new owner runs this, the machine's
DID document is still under the *old* owner's control - `set_machine_verification_methods` would
succeed if the old owner called it, and `current_owner()` will refuse to resolve anything at all
(not just refuse a stale entry) until this step is done.

This was tested (2026-09-27) via the `peaq_os_sdk` Python API directly, not a dedicated CLI
subcommand - use `PeaqosClient.set_machine_controller`, run by the NEW owner with their own signing
key:

```python
from peaq_os_sdk import PeaqosClient, Address, Tokenomics20Config

client = PeaqosClient(
    rpc_url="https://peaq.api.onfinality.io/public",
    private_key="0x<NEW_OWNER_EVM_PRIVATE_KEY>",
    # ... same six zero-address legacy params as build_client() in rpi/peaq_ownership.py
    tokenomics20=Tokenomics20Config(deployment_id="peaq-mainnet"),
)
client.set_machine_controller(
    5149596011477982620423871887556457753159696362422273317835964893685329625592,
    Address("0x<NEW_OWNER_EVM_ADDRESS>"),
)
```

This call is gated to the *current NFT owner* (confirmed live, 2026-09-27: a non-owner attempting
it gets rejected with `NOT_OWNER_OR_CONTROLLER`) - so it can only be run successfully once step 1
has actually gone through, and only by whoever now holds the NFT. Check whether `peaqos` has grown
a CLI wrapper for this (`peaqos machine --help`) before assuming the Python path above is the only
option - it wasn't checked during the 2026-09-27 test since the Python path was already in hand
from building `rpi/peaq_ownership.py`.

## Step 3: update the DID's Solana verification method (peaq, EVM side, run by the NEW owner)

The new owner needs to declare which Solana wallet should receive Unit C's delivery income, using
the same multibase encoding used at onboarding (multicodec `0xed01` + the raw 32-byte Solana
pubkey, base58btc-encoded with a `z` prefix - see `rpi/peaq_ownership.py`'s
`_decode_solana_multibase()` for the inverse of this encoding, which shows the exact byte layout).

```bash
cat > new-owner-verification-methods.json << 'EOF'
[
  {
    "id": "#solana-owner",
    "methodType": "Ed25519VerificationKey2020",
    "controller": "0x<NEW_OWNER_EVM_ADDRESS>",
    "publicKeyMultibase": "<the new owner's Solana pubkey, multibase-encoded>"
  }
]
EOF

peaqos machine did set-verification-methods \
  5149596011477982620423871887556457753159696362422273317835964893685329625592 \
  --file new-owner-verification-methods.json \
  --yes
```

**This replaces the entire verification-methods array** (per the CLI's own description) - if the
DID ever ends up with more than the one Solana entry (e.g. an EVM controller method added later),
include all of them in this file, not just the new one, or the others will be dropped.

## Verifying the handoff worked

```python
from peaq_ownership import build_client, current_owner
client = build_client("https://peaq.api.onfinality.io/public")
print(current_owner(client, 5149596011477982620423871887556457753159696362422273317835964893685329625592))
```

Should print the new owner's Solana pubkey. If it raises `OwnerLookupError` instead, step 2 and/or
3 either didn't happen yet, or an address doesn't exactly match the new NFT owner's address (case
doesn't matter - the comparison is case-insensitive - but it must be the same address).

## What happens if you skip a step

- **Only step 1 done (NFT transferred, DID control not claimed):** `state.controller` still
  points at the old owner, so `current_owner()` refuses immediately - it won't even look at the
  DID's verification methods. This is also what protects against the old owner planting a
  fraudulent entry in the meantime: without DID control, any `set_machine_verification_methods`
  call the old owner makes is a no-op as far as `current_owner()`'s trust is concerned once step 2
  eventually happens and a clean entry is written in step 3.
- **Steps 1 and 2 done, step 3 skipped:** `state.controller` now matches the new owner, but no
  verification method's own `controller` matches yet either (or a stale one from the old owner is
  still there and no longer matches) - `current_owner()` finds no usable entry and refuses.

Either way, `car_main.py`/`delivery_agent.py` both treat `OwnerLookupError` as a refusal, not a
crash - deliveries stop signing (the robot keeps working, payment just doesn't release) until all
three steps are done. This is the intended, safe failure mode: refusing to pay is always preferred
over guessing who should get paid.
