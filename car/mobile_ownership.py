"""Wallet-signature-based ownership gate for the CarOwnerApp mobile client.

Replaces a static password with proof-of-ownership: the mobile app's
connected Solana wallet signs a short-lived challenge message, and this
module verifies (a) the signature is genuinely from that wallet and (b)
that wallet is the machine's CURRENT registered payout owner per
rpi/peaq_ownership.py's existing, already-tested DID-resolution logic.

No new trust is introduced - this reuses current_owner() exactly as
car_main.py does for payout routing, just for access control instead.
Deliberately stateless (no server-side nonce store): the challenge
message embeds its own timestamp, and verify_signature() rejects
anything older than CHALLENGE_TTL_SECONDS. This is a read-access gate
for telemetry, not a funds-moving action, so a coarse replay window is
an acceptable trade-off for the hackathon MVP's time budget.
"""
from __future__ import annotations

import secrets
import sys
import time
from pathlib import Path

import base58
import nacl.exceptions
import nacl.signing
from solders.pubkey import Pubkey

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "rpi"))
import peaq_ownership  # noqa: E402  (path insert must happen first)

CHALLENGE_TTL_SECONDS = 300  # 5 minutes


class OwnershipCheckError(RuntimeError):
    """Raised for any malformed/invalid input - callers treat this as
    'access denied', never as a crash (same refuse-loudly philosophy as
    peaq_ownership.OwnerLookupError)."""


def build_challenge() -> str:
    """A short-lived message for the mobile app's wallet to sign. The
    nonce prevents two challenges from ever looking identical; the
    timestamp is what verify_signature() actually checks the age of."""
    nonce = secrets.token_hex(8)
    ts = int(time.time())
    return f"RoboPay CarOwnerApp login\nnonce: {nonce}\ntimestamp: {ts}"


def _extract_timestamp(message: str) -> int:
    for line in message.splitlines():
        if line.startswith("timestamp: "):
            try:
                return int(line.removeprefix("timestamp: ").strip())
            except ValueError:
                raise OwnershipCheckError("challenge message has a malformed timestamp line")
    raise OwnershipCheckError("challenge message is missing its timestamp line")


def verify_signature(pubkey_str: str, message: str, signature_b58: str) -> None:
    """Raises OwnershipCheckError if the signature doesn't verify, the
    pubkey/signature aren't well-formed, or the challenge has expired.
    Returns None (not a bool) on success - deliberately: this is a gate,
    not a predicate, so there's no "ignore the return value" failure mode."""
    ts = _extract_timestamp(message)
    age = time.time() - ts
    if age < 0 or age > CHALLENGE_TTL_SECONDS:
        raise OwnershipCheckError(f"challenge expired or has a future timestamp (age={age:.0f}s)")

    try:
        pubkey_bytes = bytes(Pubkey.from_string(pubkey_str))
    except Exception as e:
        raise OwnershipCheckError(f"malformed pubkey {pubkey_str!r}: {e}") from e

    try:
        sig_bytes = base58.b58decode(signature_b58)
    except Exception as e:
        raise OwnershipCheckError(f"malformed base58 signature: {e}") from e

    try:
        nacl.signing.VerifyKey(pubkey_bytes).verify(message.encode(), sig_bytes)
    except nacl.exceptions.BadSignatureError as e:
        raise OwnershipCheckError("signature does not match pubkey/message") from e
    except Exception as e:
        raise OwnershipCheckError(f"signature verification failed: {e}") from e


def verify_ownership(pubkey_str: str, message: str, signature_b58: str, peaq_rpc_url: str, machine_id: int) -> None:
    """Full gate: signature must verify AND that pubkey must be the
    machine's current registered owner. Raises OwnershipCheckError on
    any failure (bad signature, expired challenge, lookup failure, or
    genuine mismatch) - callers don't need to distinguish why, only
    that access is denied."""
    verify_signature(pubkey_str, message, signature_b58)

    try:
        client = peaq_ownership.build_client(peaq_rpc_url)
        owner = peaq_ownership.current_owner(client, machine_id)
    except peaq_ownership.OwnerLookupError as e:
        raise OwnershipCheckError(f"could not resolve current machine owner: {e}") from e

    if str(owner) != pubkey_str:
        raise OwnershipCheckError(
            f"wallet {pubkey_str} is not the machine's current owner (owner is {owner})"
        )
