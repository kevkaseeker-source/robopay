# Workaround: onboard Unit C natively on peaq instead of Solana-home

**Status: done. Unit C has a real, live peaq Machine-ID as of 2026-09-26.**

- **Machine ID:** `5149596011477982620423871887556457753159696362422273317835964893685329625592`
- **Transaction:** `0x0cb36dd68f754b7fac6d5a59db54f7efa9a59d3023f7ac7f013a0fb6c9f8ae1f` (peaq mainnet, chain
  3338, block `0xb37640`, `status: 0x1` — independently verified via `eth_getTransactionReceipt`,
  not just the CLI's own success message)
- **Machine type:** `robopay-picarx-unit-c`
- **Cost:** 0.4704 PEAQ (≈ $0.018)
- **Owner/controller:** `0x4d99BeAD5A4CCE20a7F93CB2CF62f1847263Ea8f` (this project's `peaq_operator`
  EVM wallet)

This directly unblocks the project's participation in the "Advance the Machine Economy with peaq"
Superteam Earn track (deadline 2026-10-27) despite the still-unresolved upstream
`CrossChainMirror` funding gap documented in
`docs/peaq-integration/crosschainmirror-bug-report.md`.

## Why this works

**Confirmed precisely (updated 2026-09-27, independently re-verified across multiple rounds,
including an adversarial pass that specifically tried to disprove this section — see revision note
at the end of this section):** a direct read of the installed `peaq-os-sdk` source (v0.8.0),
specifically `tokenomics/activate_machine.py`, shows `MachineStateAndSync.activateMachine` never
calls `reserveForeignMachine(uint256,uint16,bytes32)` — the function that reverts
`ReservationPushSkipped` on the Solana-home path. That much is a hard guarantee: the native path
literally does not call that function, confirmed by reading the SDK's own onboarding code.

**However — this is NOT the same as "never touches the underlying problem."** Unit C's own real
onboarding receipt (`0x0cb36dd6...ae1f`, block `0xb37640`) contains two `MirrorPushSkipped` logs
emitted by `MachineBridgeAdapter` (`0x791087c35484b567c53f392a624d2e4BaDcC53DF`) — one for
destination chain 5 (Solana), one for destination chain 3 (Base) — with no `NativeFeePaid` log
alongside them. So the native path *does* hit the same underfunded-adapter condition as the
Solana-home path; it just **tolerates the skip as non-fatal instead of reverting the whole
onboarding**. Practical consequence: Unit C's onboarding was never actually mirrored to either
Solana or Base — this project doesn't rely on that mirror (see "No automatic Solana-side owner
tracking" below), but a future integrator who does rely on it should know the native path doesn't
guarantee it happens.

Further on-chain evidence on the fee mechanism (independently checked against a real block, not
inferred): `MachineBridgeAdapter`'s `NativeFeePaid` events currently show ~17.14 PEAQ per successful
Solana push. At Unit C's onboarding block the adapter held ~13.26 PEAQ (below that threshold, hence
the skip); it holds ~10.43 PEAQ as of 2026-09-27. The **Base (chain 3) skip looks like a separate
issue, not the same funding shortfall**: in a later batch of onboardings at block 11764154 (e.g. tx
`0x833018e3f9183329a98590cd54651391d1d8590afba3e8a4739a364ad2d3a3c9`), Base was skipped even while
the adapter's reserve was ~113 PEAQ and Solana pushes in the same batch were successfully paid and
sent — so the Base mirror path looks disabled or misconfigured on peaq's side independent of
balance, not merely underfunded.

*Revision note: an earlier version of this section claimed the native path "structurally never
calls the broken function at all" and separately claimed a speculative `MirrorPushSkipped`-style
event "was never actually confirmed to exist" and retracted it. Both statements were wrong — later,
more careful re-verification (independently reproduced by two separate review passes) found the
`MirrorPushSkipped` events described above are real and present in Unit C's own receipt. This
section has been corrected accordingly; the surviving, accurate claim is narrower: the native path
doesn't call `reserveForeignMachine`, so it can't hit that specific revert, but it does still run
into the underlying fee/skip condition on the same underfunded contract.*

On-chain evidence native onboarding genuinely works today, independent of anything this project did:
at least 8,800+ successful native `HomeChainRegistered` events on `CrossChainMirror` since block
11,485,485 (peaq mainnet), including at least one call from a wallet unrelated to this project (this
only shows native onboarding succeeds broadly — it doesn't by itself say anything about whether the
cross-chain mirror push succeeds for any given machine, which is a separate condition as shown
above).

## What "native" costs us vs. the originally-planned Solana-home flow

- **Identity location:** the Machine-NFT (ERC-721) and DID document live on peaq's own chain, not
  as Solana accounts. `get_machine_owner(machine_id)` (peaq_os_sdk) returns a real, meaningful
  peaq-chain (EVM) owner address for this machine. (Note: Unit C's Solana-home onboarding was never
  actually completed, so this isn't an observed before/after on Unit C itself — it's based on
  reading the SDK source for what that function would have returned for an incomplete/foreign-home
  machine, which is why this project avoided relying on it for the Solana-home path in the first
  place.)
- **Irreversible:** the machine ID is `keccak256(machine_type, credential_subject_hex)`. The exact
  pair used here (`robopay-picarx-unit-c` + the hex encoding of Unit C's own Solana pubkey,
  `7VizNvqBSnHnP8ySnsjxxyUnBQCybVnJHBDRyvaThXia`) can now never be onboarded as a Solana-home
  machine, even if peaq fixes `CrossChainMirror` later. A genuinely Solana-home onboarding for
  Unit C, if ever wanted, would need a different `credential_subject_hex` (a "new" machine
  identity from peaq's point of view).
- **No automatic Solana-side owner tracking.** peaq's own Solana bridge/mirror (the thing that's
  broken) is what would have automatically exposed "this machine's current owner" as a Solana
  pubkey. Since we're not using it, this project's own code has to resolve "who should get paid"
  a different way — see the "What this changes for tokenized ownership" section below.

## How the Solana linkage was recorded

Unit C's Solana owner pubkey was included in the machine's DID document as an
`Ed25519VerificationKey2020` verification method, multibase-encoded per the standard `did:key`
convention (multicodec prefix `0xed01` + the raw 32-byte pubkey, base58btc-encoded with a `z`
prefix):

```json
{
  "verificationMethods": [
    {
      "id": "#solana-owner",
      "methodType": "Ed25519VerificationKey2020",
      "controller": "0x4d99BeAD5A4CCE20a7F93CB2CF62f1847263Ea8f",
      "publicKeyMultibase": "z6Mkkwz2yB5cnKnFVdp9UShop52mzyUq1P2eyC8MpCYUckVx"
    }
  ],
  "authentication": [0],
  "serviceEndpoints": []
}
```

This is an on-chain, publicly readable claim — "this peaq machine's controller also controls this
Solana wallet" — but it is *not* an authoritative ownership record enforced by peaq's own protocol
(peaq's own SDK has no built-in mechanism to treat a DID verification method as controlling
payouts/transfers; nothing in peaq's system stops the DID from disagreeing with who currently owns
the Machine-NFT). This project's own code (`rpi/peaq_ownership.py`) treats the DID as
*authoritative only when its `controller` matches the machine's current NFT owner* - see below -
so a stale or mismatched DID entry is refused rather than trusted.

**Resolved (confirmed empirically, 2026-09-27, disposable test machine - not Unit C):** peaq does
**NOT** restrict DID writes to the current NFT owner by default. Transferring the Machine-NFT does
**NOT** move DID-write ("controller") rights - those stay with whoever last held them until
explicitly reassigned. Confirmed live: after transferring a test machine's NFT, the **former**
owner could still successfully call `set_machine_verification_methods` and plant an entry whose
own `controller` field falsely claimed the new owner - and the original (pre-fix)
`rpi/peaq_ownership.py` was fooled by exactly this, resolving the attacker-planted key as if it
were legitimate.

The fix, also confirmed live: peaq DOES gate one specific call -
`PeaqosClient.set_machine_controller(machine_id, controller)` - to the current NFT owner only (a
non-owner attempting it gets `NOT_OWNER_OR_CONTROLLER`). So the real security boundary is a
separate, explicit "claim DID control" step the **new** owner must take right after receiving the
NFT, before anything else. `rpi/peaq_ownership.py`'s `current_owner()` now checks
`state.controller == state.owner` as its primary gate - it refuses (rather than trusting any
verification method) until that claim has happened. See
`docs/peaq-integration/ownership-transfer-guide.md` for the corrected three-step transfer process.

Today (Unit C, unchanged) the NFT owner and the DID controller are still the same single party
(`0x4d99BeAD5A4CCE20a7F93CB2CF62f1847263Ea8f`), so there is no exposure yet - this only matters
starting at Unit C's first real ownership handoff.

## What this changes for tokenized ownership

`rpi/peaq_ownership.py` originally assumed a Solana-home machine and called
`get_machine_activation_state(machine_id, solana=SolanaMachineCurrentStateParams())` →
`.native.machine.owner` to resolve the current Solana payout address directly from peaq's own
state. That call path doesn't apply to a peaq-native machine. It has been redesigned (2026-09-26)
to call `get_machine_management_state(machine_id)` instead, reading the machine's ground-truth NFT
owner plus its DID document's verification methods (matched by controller, as described above).
(This project tracks its own design decisions in an internal spec doc not included in this public
extract.)

## Reproducing this (CLI)

```bash
peaqos activate \
  --machine-type '<identity domain>' \
  --credential-subject-hex 0x<32-byte anchor, hex> \
  --manufacturer <EVM address or Solana pubkey, informational only> \
  --tier entry \
  --payment peaq \
  --did-document <path to a JSON DID document, see above> \
  --dry-run   # remove --dry-run once the preview looks right - it costs
              # real (tiny) PEAQ and is irreversible once submitted
```

Requires `TOKENOMICS_DEPLOYMENT_ID=peaq-mainnet` and the six legacy contract addresses configured
(`peaqos init --non-interactive` handles this) — no `--chain solana` flag, which is what selects
the broken cross-chain path.
