# Tokenized robot ownership via peaq's Machine-NFT

Sub-project 1 of 3 from the "RoboPay dApp" brainstorm (2026-09-23). The other
two (sensor-data collection/dashboard, image-labeling tool) are separate,
independently-scoped sub-projects and are not covered here.

**Correction (2026-09-26), supersedes this spec's original Solana-home design where they
conflict:** the Solana-home onboarding path this spec was written against turned out to be
blocked by an upstream peaq bug (`CrossChainMirror`/`MachineBridgeAdapter` can't pay their own
cross-chain fee - see `docs/peaq-integration/crosschainmirror-bug-report.md`). Unit C was instead
onboarded as a **peaq-native** machine (`docs/peaq-integration/native-onboarding-workaround.md`
has the real transaction), which never hits that bug. This changes how a Solana payout owner is
resolved: `get_machine_activation_state(..., solana=...)` (this spec's original mechanism, `.native
.machine.owner`) does not apply to a native machine. `rpi/peaq_ownership.py` (`feat/x402-verifier`)
now reads `get_machine_management_state()` instead - the machine's ground-truth NFT owner (an EVM
address) plus its DID document's verification methods, matched by controller, decoded from an
Ed25519VerificationKey2020 entry back to a Solana `Pubkey`. The rest of this spec (single owner,
off-chain lookup, refuse-don't-fallback on any failure, out-of-scope items) is unchanged - only
the *mechanism* for resolving "who currently owns this" changed, not the design principles below.
See `docs/peaq-integration/ownership-transfer-guide.md` for what an owner handoff now looks like
operationally.

## Why

Kevin's framing: the robot is like a worker, and its human owner should
receive the income it generates from deliveries. Today that already happens
mechanically (`SELLER_PUBKEY` in `rpi/config.py` receives the SOL on every
`confirm_delivery`), but that ownership is a static config value, not
something represented or transferable on-chain.

peaq's Machine-ID onboarding (in progress, see
[[2026-09-21-peaq-integration]] status in `project_picarx_unit_c.md` memory -
currently blocked on the `CrossChainMirror` zero-balance issue) already mints
a **Machine NFT** for the robot as part of native onboarding, and peaq's own
SDK already ships `get_machine_owner()` / `transfer_machine()`. "Tokenizing"
Unit C's ownership does not need a new token system - it needs the delivery
payout to follow that NFT's current holder instead of a hardcoded address.

## Scope for this PoC

**In scope:**
- One owner per robot at a time (matches today's model).
- The NFT's current holder receives 100% of that robot's delivery income.
- Transferring the Machine NFT (peaq's own `transfer_machine()`) changes who
  gets paid, starting with the next delivery.
- Payout target resolved **off-chain**: the car/agent looks up the current
  owner via peaq's SDK right before signing `confirm_delivery`, and passes
  that address as the transaction's seller.

**Explicitly out of scope (documented, not built):**
- **Fractional/multi-owner shares.** Considered and deliberately deferred -
  no real fleet exists yet to design real requirements against, and a
  splitting mechanism is its own audit-worthy subsystem. The off-chain
  lookup below resolves to a single address either way, so nothing here
  blocks adding a splitter contract later as that single address.
- **On-chain enforcement of the owner check.** The stronger version: the
  Anchor escrow program itself reads peaq's Solana-side machine-registry via
  CPI and refuses any `confirm_delivery` whose seller account doesn't match
  the verified owner, so a compromised or buggy off-chain agent cannot pay
  the wrong address no matter what it tries to submit. This is the intended
  target design (same trust-boundary principle as the letterbox
  second-signer idea, applied to ownership instead of delivery-proof), but
  needs peaq's Solana-side machine-registry account layout (only the program
  ID `AgR8exgW2mwpf4v2PpojYuDHXVBThZtenS6XYjV4JnUD` is known so far, not its
  account structure) and a re-reviewed Anchor program change. Phase 2, after
  the off-chain version works and a real Machine NFT exists to test against.

## Data flow

```
Buyer funds escrow (unchanged)
        |
Car decides delivery is complete (QR or AI-agent path, unchanged)
        |
NEW: car/agent calls peaq SDK get_machine_activation_state(machine_id,
     solana=SolanaMachineCurrentStateParams()) -> .native.machine.owner
        |
        v
Resolved current owner pubkey  ---->  passed as `seller` for this confirm_delivery call
        |
solana.confirm_delivery(lat, lon, seller=<resolved pubkey>)
        |
SOL lands with whoever holds the Machine NFT right now
```

## Components touched

- `rpi/solana_client.py`: `SolanaClient.confirm_delivery()` gains an optional
  `seller: Pubkey | None` parameter. When given, it is used instead of
  `cfg.SELLER_PUBKEY` for the `seller` account in the instruction; when
  omitted, behavior is unchanged (so `car_main.py`'s existing fixed-QR path
  keeps working exactly as today unless explicitly opted in).
- New `rpi/peaq_ownership.py`: one function, `current_owner(machine_id) ->
  Pubkey`, wrapping `peaq_os_sdk`'s `get_machine_activation_state(machine_id,
  solana=SolanaMachineCurrentStateParams())` and returning
  `.native.machine.owner` from the `SolanaMachineCurrentState` it returns.
  **Correction found during implementation planning (2026-09-23):** the
  more obviously-named `get_machine_owner()` reads
  `MachineRegistry.ownerOf()` on the **peaq EVM chain** and returns an
  `Address` (`0x...`) - the machine's peaq-side registry owner, not usable
  as a Solana payout target. The actual Solana-native owner
  (`MachineRecord.owner: SolanaPubkey`, docstring: "Native Solana owner —
  never the peaq operator") only comes back through
  `get_machine_activation_state(..., solana=SolanaMachineCurrentStateParams())`,
  inside `.native.machine.owner` - both `.native` and `.native.machine` are
  `| None` and must be checked before use. No private key is needed for
  this call: it is a read against a keyless `PeaqosClient` context
  (`tokenomics20=Tokenomics20Config(deployment_id="peaq-mainnet",
  creation_home="solana", solana_rpc_url=...)`), the same pattern already
  used for `preview_machine_activation()` earlier this session. Raises on
  any failure (including `state.native is None` or `state.native.machine is
  None`) rather than returning a fallback value - callers decide what to do
  with that (here: refuse, see Error handling below).
- `car_main.py` / `delivery_agent.py`: call `current_owner(machine_id)`
  immediately before signing, pass the result into `confirm_delivery(...)`.
- `machine_id` becomes a config value once peaq onboarding actually succeeds
  for Unit C (currently blocked - see Dependencies below).
- **New dependency on the RPi itself:** `get_machine_activation_state()`
  needs `peaq_os_sdk` importable wherever `current_owner()` runs. That
  package is
  currently only installed in the WSL/Windows-PC venv used for the peaq
  onboarding work this session (`~/peaq-venv`), not in the RPi's own venv -
  it was never needed there before. Installing it on the RPi is untested;
  given the package's size (~50 dependencies, see the peaq-integration
  memory notes) this should be checked for footprint/install time on the
  RPi's actual hardware before assuming it is free, not just assumed to
  work because it worked on a PC.

## Error handling

If the owner lookup fails for any reason (peaq RPC unreachable, machine not
yet onboarded, malformed/missing owner record), **refuse the confirmation
rather than falling back to the old static `SELLER_PUBKEY`**. Guessing who
should be paid is worse than a delayed delivery: this matches the existing
`_refuse()` guardrail philosophy already in `delivery_agent.py`
("Refusing is a valid and useful outcome"). A failed lookup is not the
robot's problem to paper over - it should surface, not silently resolve to a
possibly-stale address.

## Testing

- **Offline:** extend `agent/selftest.py`'s stub harness with a mocked
  `get_machine_owner()` response, verifying the resolved address is the one
  actually passed into the (already-stubbed) `confirm_delivery` call, and
  that a lookup failure produces a refusal rather than falling back.
- **Live:** blocked until Unit C's Machine-ID onboarding actually completes
  (peaq's `CrossChainMirror` zero-balance issue - reported to peaq's Discord
  2026-09-22/23, awaiting their fix). Nothing here can be tested against a
  real NFT before that.

## Dependencies / current blockers

This entire sub-project is gated on peaq's Solana onboarding actually
completing for Unit C, which is currently blocked upstream (not by us) - see
`project_picarx_unit_c.md` memory for the full `CrossChainMirror` finding.
The design above can be implemented and offline-tested without that being
resolved; it cannot be proven end-to-end until it is.
