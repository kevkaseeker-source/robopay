# peaq bug report: Solana-home onboarding reverts `ReservationPushSkipped` (`MachineBridgeAdapter` fee reserve insufficient)

**Status as of 2026-09-27: still unresolved upstream (peaq hasn't fixed the underlying Solana-home
path), but no longer blocking this project** - see "What unblocked this project" below for the
native-onboarding workaround that lets this project proceed without it. The revert is confirmed
still live and reproducible as of today via a read-only `eth_call`.

**Historical note (no longer accurate, kept for context):** this doc originally targeted
`CrossChainMirror` itself as the contract lacking funds (based on its `eth_getBalance` reading
`0x0`). The "Update (2026-09-26)" section below refines this: the contract that actually needs to
pay the LayerZero fee is `MachineBridgeAdapter`, a different contract - `CrossChainMirror` holding
zero balance appears to be by design (it isn't the contract meant to pay anything). The title and
root-cause section above have been updated to reflect this; treat the "Update" section as the
current understanding.

**Impact on this project (historical - resolved, see below):** Unit C's peaq Machine-ID onboarding
could not complete a Solana-home reservation. This project's implementation
(`rpi/peaq_ownership.py`, plus a Solana-side `seller` parameter and app-level wiring not included in
this public extract) was finished and tested offline independent of this blocker; the project
worked around it entirely by onboarding as a peaq-native machine instead (see
`native-onboarding-workaround.md`), so this is no longer a live blocker for RoboPay - only for
anyone who specifically needs a genuine Solana-*home* machine.

## What's broken

Calling `peaqos onboard --phase reservation --yes` (or the equivalent SDK call,
`Tokenomics20Config(creation_home="solana", ...)`) reverts during `eth_estimateGas`'s preflight
simulation — it never even reaches the point of sending a transaction, so no PEAQ is spent and no
retry-with-more-gas fixes it.

Decoded revert (see "How this was diagnosed" below): a call to

```
CrossChainMirror.reserveForeignMachine(machineId, homeChainId=5, nativeOwner)
```

on contract `0x71DCB313977d6884212395505f081a2991Bfe8E5` (peaq mainnet, chain ID 3338 — an
EIP-1967 proxy; live implementation at `0x2dfcaec4476d9bb3a062cc2db2f5c9c3c91ebcea` at time of
writing) reverts with the custom error `ReservationPushSkipped(uint16 homeChainId)`, called with
`homeChainId = 5` (Solana's protocol chain ID in peaq's own chain registry — confirmed against
`InfoDesk`'s `messagingVendorIdOf(5)` returning `30168`, matching the LayerZero endpoint ID peaq
uses for Solana).

## Root cause

**The `CrossChainMirror` contract itself holds zero native PEAQ balance.**

```
eth_getBalance(0x71DCB313977d6884212395505f081a2991Bfe8E5) == 0x0
```

`reserveForeignMachine` is declared `"stateMutability": "nonpayable"` in its own ABI (so the
caller isn't expected to attach a fee), and the contract has no `receive()`/`fallback()` function
at all — meaning a plain PEAQ transfer *to* the contract to top it up isn't even possible through
a simple send; whatever mechanism is meant to fund it must be something else on peaq's side
(an admin-only funding call, a different contract, or an as-yet-undeployed piece of their own
infrastructure). Ruled out before landing on this conclusion:

- **Not a caller-side balance/gas issue.** The operator wallet used for testing
  (`0x4d99BeAD5A4CCE20a7F93CB2CF62f1847263Ea8f`) was funded with ~74 PEAQ throughout testing.
- **Not chain-5 (Solana) registration.** `InfoDesk.isRegisteredChain(5)` → `true`,
  `isBridgingEnabled()` → `true`.
- **Not stale prior-attempt state.** Checked and clean; not a retry-of-a-broken-earlier-attempt
  artifact.
- **Not `MachineSubscription`'s economic-authority check.** `isEconomicAuthority()` → `true` for
  the calling wallet.

The likely explanation: `reserveForeignMachine` internally makes a low-level external call whose
own gas/value depends on the contract's own balance (consistent with paying an internal
cross-chain messaging fee — e.g. a LayerZero send fee — out of its own funds rather than the
caller's), and with zero balance that internal call fails, which the outer function surfaces as
`ReservationPushSkipped`.

## How this was diagnosed

No verified Solidity source was available through any normal channel at the time of
investigation:

- GitHub: the relevant repo returned 404/private.
- Blockscout (`scout.peaq.xyz`): returned HTTP 522 (origin unreachable) repeatedly.
- Sourcify: contract listed as "not verified."
- npm (JS SDK source, which might have documented the fee/funding requirement): the package page
  403'd.

Diagnosis instead came from:
1. Fetching the live implementation bytecode directly from the chain (behind the EIP-1967 proxy).
2. Disassembling it with `pyevmasm` to locate the `PUSH4 0x95f7b557` selector and tracing the logic
   around it. **Correction (2026-09-27, caught by independent re-verification):** `0x95f7b557` is
   the `ReservationPushSkipped` **error's** selector, not `reserveForeignMachine`'s function
   selector as originally stated here — the function's own selector is `0x9f6f193a` (both
   independently recomputed via `keccak256` against the exact signatures in `CrossChainMirror.json`).
   Doesn't change the diagnosis, just corrects which selector is which.
3. Finding a `GAS`/`CALL`/`ISZERO` pattern immediately preceding the revert path — a low-level
   external call whose failure gets wrapped into `ReservationPushSkipped`.
4. Cross-checking the "zero balance" hypothesis directly against `eth_getBalance` — confirmed.

**Independently reproduced, live (2026-09-27):** a separate verification pass built the exact
`reserveForeignMachine(uint256,uint16,bytes32)` calldata from scratch (selector `0x9f6f193a`,
recomputed independently) and called it read-only (`eth_call`, no transaction sent) against
`0x71DCB313977d6884212395505f081a2991Bfe8E5` with an arbitrary machine ID and `homeChainId=5`.
Got back `revert data: 0x95f7b5570000...0005` — decodes exactly to
`ReservationPushSkipped(homeChainId=5)`. This confirms the failure mode directly and reproducibly,
independent of the original bytecode-disassembly investigation.

Corroborating evidence found independently: peaq's own GitHub PR
(`peaqnetwork/peaq-os-skills` PR #10) documents, for this same CLI/SDK release, that *"the Solana
flow was not executed end to end (mainnet only, needs funded wallets and the released CLI)"* —
i.e. peaq's own team had not completed a real live run of this exact path before releasing it.

## Related, separate, already-fixed issue

A second, earlier bug in the same SDK release (`peaq-os-sdk` 0.8.0) was found and worked around
locally before this one: `peaq_os_sdk/tokenomics/abis/MANIFEST.json` ships
`source_revision`/`implementation_address`/`deployed_bytecode_hash` as `null` for all 7
Tokenomics 2.0 contracts, which made `OnboardingPrerequisites.deployment_ready` unconditionally
`False` for everyone on that release, regardless of correct setup. That one only required a local
patch on the calling side (`_internal/svm/onboarding_prerequisites.py`) and doesn't block
anything server-side — it's mentioned here only for completeness, since both were found in the
same investigation. It is **not** the same issue as the `CrossChainMirror` funding gap above.

## Reported to peaq

Both issues were reported in peaq's Discord (2026-09-22): this one (`CrossChainMirror`) with the
decoded selector/error name and the zero-balance finding, and the separate `MANIFEST.json`-nulls
issue above with its own root cause — confirmed seen by a peaq team admin. No fix or ETA had been
communicated back as of this doc's last-checked date above. (**Correction, 2026-09-27**: an earlier
version of this sentence attributed "the zero-balance finding" ambiguously to "the second" issue;
the zero-balance finding belongs to *this* issue, `CrossChainMirror` — the `MANIFEST.json` issue is
unrelated to any balance and never was.)

## Update (2026-09-26): the fee-paying contract is `MachineBridgeAdapter`, not `CrossChainMirror` itself

A follow-up investigation found the LayerZero message that mirrors a Solana reservation is likely
paid by a *different* contract, `MachineBridgeAdapter` (`0x791087c35484b567c53f392a624d2e4BaDcC53DF`)
- not by `CrossChainMirror` directly. Support for this: `peaq_os_sdk`'s own bundled deployment
record (`tokenomics/deployments.py`) sets this same address as `solana.peaq_peer`, i.e. it's
structurally configured as peaq's LayerZero peer contract for the Solana leg. `CrossChainMirror`
holding zero balance may simply be by design (it isn't the contract meant to pay anything).
`MachineBridgeAdapter` itself is *not* empty - confirmed at ≈13.26 PEAQ on 2026-09-26, and
**10.4283 PEAQ when independently re-checked on 2026-09-27** (this balance is dynamic, re-check
before quoting it anywhere rather than trusting either cached figure) - but that may still be too
little to cover one LayerZero send fee plus Solana account rent. This refines, rather than
overturns, the original diagnosis: the underlying problem is still "the contract meant to pay
peaq's side of this cross-chain fee doesn't have enough PEAQ to do it," just on a different
specific contract than first identified.

**Correction (2026-09-27):** an earlier version of this section claimed "peaq's own documentation
independently confirms this exact failure mode" and quoted a specific troubleshooting-guide
passage. An independent re-verification pass could not find this passage anywhere on
`docs.peaq.xyz` or in public web/GitHub search, despite a specific, targeted search for the exact
quoted text and for `ReservationPushSkipped`/`MachineBridgeAdapter` on peaq's docs site. **That
quote is retracted as unverifiable** - it may have come from a private Discord conversation rather
than public documentation, but this doc should not have presented it as "peaq's own documentation
confirms" without a checkable source. The rest of this section's findings (the address, the
balance figures, the LayerZero-peer configuration) are independently verified on-chain/in the SDK
source and stand regardless of this retraction.

**What IS independently, reproducibly confirmed (2026-09-27):** a separate verification pass built
`reserveForeignMachine`'s calldata from scratch and called it read-only against `CrossChainMirror`
- it reverts `ReservationPushSkipped(homeChainId=5)`, exactly as described above. This is
on-chain, checkable by anyone, right now, with no dependency on the retracted quote.

**The exact fee, found by direct observation (2026-09-27):** `MachineBridgeAdapter`'s
`NativeFeePaid` events show a real per-push cost of **~17.1385 PEAQ** for a successful Solana
mirror push (six such events observed at block 11764154, txs
`0x833018e3f9183329a98590cd54651391d1d8590afba3e8a4739a364ad2d3a3c9` and
`0xaedcf0f90cfd39360f6741e6191ebe3c4fbfa7788fc5a6c40f878291d65bc650`). The adapter's reserve fell
across those six pushes: 113.26 -> 96.12 -> 78.98 -> 61.84 -> 44.71 -> 27.57 -> 10.4283 PEAQ (its
current balance). This is consistent with "not enough to cover ~17.14 PEAQ" being the real gate for
the Solana push specifically.

**The chain-3 (Base) skip looks like a separate, non-balance issue.** In the same batch of
transactions above, destination chain 3 (Base) was skipped (`MirrorPushSkipped`) even while the
adapter held ~113 PEAQ and Solana (chain 5) pushes in the same batch were successfully paid and
sent. So Base's mirror path appears disabled or misconfigured independent of the adapter's balance
- worth peaq's team checking separately from the funding issue.

## What unblocked this project

A workaround was found and used successfully (2026-09-26): onboard Unit C as a **peaq-native**
machine (no `creation_home="solana"` claim) instead of a cross-chain one. This never calls
`reserveForeignMachine` at all, so it can't hit *this specific revert*. It does still run into the
same underfunded-adapter condition - Unit C's own onboarding transaction shows `MachineBridgeAdapter`
emitting `MirrorPushSkipped` for both Solana and Base - but the native path tolerates that skip as
non-fatal instead of reverting the whole onboarding. Full writeup, including the real on-chain
transaction and this nuance, at `docs/peaq-integration/native-onboarding-workaround.md`.

peaq funding `MachineBridgeAdapter` with enough PEAQ remains the actual, direct fix for anyone who
specifically needs a Solana-*home* machine (this project no longer does, but the underlying bug is
still open on peaq's side as far as this project knows).
