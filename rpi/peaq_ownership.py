"""Resolve a peaq Machine-ID's current payout owner.

Unit C is onboarded as a peaq-NATIVE machine (not Solana-home - see
docs/peaq-integration/native-onboarding-workaround.md for why: the
Solana-home path is blocked by an upstream peaq bug, `CrossChainMirror`/
`MachineBridgeAdapter` can't pay their own cross-chain fee. Machine ID
5149596011477982620423871887556457753159696362422273317835964893685329625592,
onboarded 2026-09-26, tx
0x0cb36dd68f754b7fac6d5a59db54f7efa9a59d3023f7ac7f013a0fb6c9f8ae1f).

This means peaq's own Machine-NFT owner is a peaq-chain (EVM) address, not
directly usable as a Solana payout target. The Solana payout wallet is
recorded separately, as an Ed25519VerificationKey2020 entry (multibase:
did:key-style, multicodec 0xed01 + the raw 32-byte key, base58btc with a
'z' prefix) in the machine's DID document - see
docs/peaq-integration/native-onboarding-workaround.md for the exact JSON
that was submitted, and
docs/peaq-integration/ownership-transfer-guide.md for how a real owner
change should update both.

current_owner() resolves the payout target in two steps, both read from a
single get_machine_management_state() call (confirmed against the SDK
source and live against the real onboarded machine, 2026-09-26):
1. state.owner - the ground-truth NFT owner (an EVM address).
2. Find a verification method in state.did_document whose controller
   matches that CURRENT owner and whose type is
   Ed25519VerificationKey2020; decode its multibase key to a Pubkey.

Step 2's controller check is a deliberate consistency guard: if the NFT
has been transferred but the DID wasn't updated to match, the stale
verification method's controller won't match the new owner, and
current_owner() raises rather than paying whoever's key happens to still
be listed.

Deliberately raises on any failure rather than returning a fallback:
paying the wrong owner is worse than a refused, retryable confirmation.
Callers decide what "failure" means for them - car_main.py and
delivery_agent.py both treat OwnerLookupError as a refusal, not a crash.
"""
from __future__ import annotations

from solders.pubkey import Pubkey

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_ED25519_MULTICODEC_PREFIX = bytes([0xED, 0x01])


class OwnerLookupError(RuntimeError):
    """current_owner() could not resolve a usable Solana owner."""


def _b58decode(s: str) -> bytes:
    n = 0
    for c in s:
        if c not in _B58_ALPHABET:
            raise ValueError("not valid base58: %r" % s)
        n = n * 58 + _B58_ALPHABET.index(c)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + raw


def _decode_solana_multibase(multibase) -> Pubkey:
    """Decode a did:key-style multibase Ed25519 public key (multicodec
    0xed01 + 32 raw bytes, base58btc with a 'z' prefix) into a Pubkey -
    the inverse of the encoding used when the DID document was written."""
    if not isinstance(multibase, str):
        raise OwnerLookupError(
            "verification method's publicKeyMultibase is %r, not a string" % (multibase,)
        )
    if not multibase.startswith("z"):
        raise OwnerLookupError(
            "verification method's publicKeyMultibase %r is not "
            "z-prefixed base58btc" % multibase
        )
    try:
        raw = _b58decode(multibase[1:])
    except ValueError as e:
        raise OwnerLookupError(
            "could not base58-decode publicKeyMultibase %r: %s" % (multibase, e)
        ) from e
    if raw[:2] != _ED25519_MULTICODEC_PREFIX or len(raw) != 34:
        raise OwnerLookupError(
            "publicKeyMultibase %r is not a 34-byte Ed25519 multicodec key" % multibase
        )
    return Pubkey(raw[2:])


def build_client(peaq_rpc_url: str):
    """Construct a PeaqosClient for reading machine ownership.

    A syntactically valid private key is required by PeaqosClient's
    constructor even for read-only calls (confirmed empirically against
    live peaq mainnet, 2026-09-26) - it is never used to sign anything on
    this read-only path, so a fixed placeholder is fine. PeaqosClient's
    constructor also requires six "legacy" contract addresses even in
    Tokenomics mode, where they are unused (confirmed live, 2026-09-22) -
    zero addresses are safe here.

    Raises OwnerLookupError (not the raw exception) on any failure while
    building the client - e.g. peaq_os_sdk failing to import, or
    PeaqosClient's constructor raising for a bad URL or an SDK-internal
    reason. Callers (car_main.py, delivery_agent.py) only ever catch
    OwnerLookupError, so every failure mode of this module has to surface
    as one, not just current_owner()'s.
    """
    try:
        from peaq_os_sdk import Address, PeaqosClient, Tokenomics20Config

        zero = Address("0x0000000000000000000000000000000000000000")
        unused_read_only_key = "0x" + "11" * 32
        return PeaqosClient(
            rpc_url=peaq_rpc_url,
            private_key=unused_read_only_key,
            identity_registry=zero,
            identity_staking=zero,
            event_registry=zero,
            machine_nft=zero,
            did_registry=zero,
            batch_precompile=zero,
            tokenomics20=Tokenomics20Config(deployment_id="peaq-mainnet"),
        )
    except Exception as e:
        raise OwnerLookupError("could not build peaq client: %s" % e) from e


def current_owner(client, machine_id: int) -> Pubkey:
    """Return the Solana wallet that should be paid for machine_id's next
    delivery, or raise OwnerLookupError.

    Args:
        client: A client from build_client(), or an equivalent test double
            exposing get_machine_management_state(machine_id).
        machine_id: The onboarded Machine-ID (full-width uint256).
    """
    try:
        state = client.get_machine_management_state(machine_id)
    except Exception as e:
        raise OwnerLookupError(
            "could not read machine management state for %s: %s" % (machine_id, e)
        ) from e

    try:
        owner_raw = state.owner
        controller_raw = state.controller
        verification_methods = state.did_document.verification_methods
    except AttributeError as e:
        raise OwnerLookupError(
            "machine %s's management state is missing an expected field: %s" % (machine_id, e)
        ) from e

    if not owner_raw or str(owner_raw).lower() in ("none", "0x" + "0" * 40):
        raise OwnerLookupError(
            "machine %s has no usable owner (got %r) - unminted or burned NFT?"
            % (machine_id, owner_raw)
        )
    owner = str(owner_raw).lower()

    # The machine-wide DID controller (state.controller) is the ONLY thing
    # peaq itself gates DID writes on - confirmed empirically (2026-09-27,
    # test machine, see docs/peaq-integration/ownership-transfer-guide.md):
    # transferring the NFT does NOT move DID-write rights, and the OLD
    # owner can still call set_machine_verification_methods after losing
    # ownership, writing an entry whose OWN "controller" field claims to
    # belong to the new owner. A per-entry controller check alone is
    # exactly this attack's blind spot - it was tested and confirmed to
    # be fooled by a planted entry. The new owner must claim controller
    # rights via set_machine_controller (confirmed owner-gated: only the
    # current NFT owner can call it) before their DID entry is trusted.
    # This check is the actual security boundary; the per-entry match
    # below only picks which entry to use once this gate has passed.
    if str(controller_raw).lower() != owner:
        raise OwnerLookupError(
            "machine %s's DID controller (%s) does not match its current owner (%s) - "
            "the new owner must claim DID control (set_machine_controller) before its "
            "verification methods can be trusted (see "
            "docs/peaq-integration/ownership-transfer-guide.md)"
            % (machine_id, controller_raw, owner_raw)
        )

    matches = []
    try:
        for vm in verification_methods or ():
            if vm.method_type != "Ed25519VerificationKey2020":
                continue
            if str(vm.controller).lower() != owner:
                continue
            matches.append(_decode_solana_multibase(vm.public_key_multibase))
    except (AttributeError, TypeError) as e:
        raise OwnerLookupError(
            "machine %s's DID document is malformed: %s" % (machine_id, e)
        ) from e

    if not matches:
        raise OwnerLookupError(
            "machine %s's current owner (%s) has no matching Ed25519VerificationKey2020 "
            "in the machine's DID document - the owner may have transferred the NFT "
            "without updating the DID (see docs/peaq-integration/ownership-transfer-guide.md)"
            % (machine_id, owner_raw)
        )
    for later_match in matches[1:]:
        if later_match != matches[0]:
            raise OwnerLookupError(
                "machine %s's current owner (%s) has multiple DIFFERENT "
                "Ed25519VerificationKey2020 entries in its DID document - ambiguous, "
                "refusing rather than guessing which one to pay"
                % (machine_id, owner_raw)
            )
    return matches[0]
