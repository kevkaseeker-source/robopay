#!/usr/bin/env python3
"""Offline test for car/mobile_ownership.py. Uses real Ed25519 keys/signatures
(via PyNaCl) so verify_signature() is genuinely exercised, and monkeypatches
peaq_ownership.build_client/current_owner so no real peaq_os_sdk/network is
needed.

Run: python3 tests/test_mobile_ownership.py
"""
import sys
import time
from pathlib import Path

import base58
import nacl.signing

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "car"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "rpi"))

import mobile_ownership  # noqa: E402
import peaq_ownership  # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


# A real keypair, used to produce genuine signatures the way the mobile
# app's wallet would.
signing_key = nacl.signing.SigningKey.generate()
pubkey_str = base58.b58encode(bytes(signing_key.verify_key)).decode()


def sign(message: str) -> str:
    sig = signing_key.sign(message.encode()).signature
    return base58.b58encode(sig).decode()


# 1. Happy path: a message signed by the real key, checked immediately,
# must verify without raising.
msg = mobile_ownership.build_challenge()
try:
    mobile_ownership.verify_signature(pubkey_str, msg, sign(msg))
except mobile_ownership.OwnershipCheckError as e:
    check(False, "happy_path: valid signature was rejected: %r" % e)

# 2. Wrong key signs the message - must raise, not silently pass.
other_key = nacl.signing.SigningKey.generate()
bad_sig = base58.b58encode(other_key.sign(msg.encode()).signature).decode()
try:
    mobile_ownership.verify_signature(pubkey_str, msg, bad_sig)
    check(False, "wrong_signer: should have raised OwnershipCheckError")
except mobile_ownership.OwnershipCheckError:
    pass

# 3. Tampered message (signature valid for a DIFFERENT message) - must raise.
tampered = msg + "\nextra"
try:
    mobile_ownership.verify_signature(pubkey_str, tampered, sign(msg))
    check(False, "tampered_message: should have raised OwnershipCheckError")
except mobile_ownership.OwnershipCheckError:
    pass

# 4. Expired challenge (timestamp far in the past) - must raise even
# though the signature itself is genuinely valid.
old_msg = f"RoboPay CarOwnerApp login\nnonce: aaaa\ntimestamp: {int(time.time()) - 9999}"
try:
    mobile_ownership.verify_signature(pubkey_str, old_msg, sign(old_msg))
    check(False, "expired_challenge: should have raised OwnershipCheckError")
except mobile_ownership.OwnershipCheckError:
    pass

# 5. Future-dated challenge (clock skew / forged timestamp) - must also raise.
future_msg = f"RoboPay CarOwnerApp login\nnonce: bbbb\ntimestamp: {int(time.time()) + 9999}"
try:
    mobile_ownership.verify_signature(pubkey_str, future_msg, sign(future_msg))
    check(False, "future_challenge: should have raised OwnershipCheckError")
except mobile_ownership.OwnershipCheckError:
    pass

# 6. Malformed pubkey string - must raise OwnershipCheckError, not a raw
# exception from Pubkey.from_string().
try:
    mobile_ownership.verify_signature("not-a-real-pubkey!!", msg, sign(msg))
    check(False, "malformed_pubkey: should have raised OwnershipCheckError")
except mobile_ownership.OwnershipCheckError:
    pass
except Exception as e:
    check(False, "malformed_pubkey: raw exception leaked: %r" % e)

# 7. Malformed base58 signature - must raise OwnershipCheckError cleanly.
try:
    mobile_ownership.verify_signature(pubkey_str, msg, "not valid base58 !!!")
    check(False, "malformed_signature: should have raised OwnershipCheckError")
except mobile_ownership.OwnershipCheckError:
    pass
except Exception as e:
    check(False, "malformed_signature: raw exception leaked: %r" % e)

# 8. Missing timestamp line entirely - must raise, not crash.
try:
    mobile_ownership.verify_signature(pubkey_str, "no timestamp here", sign("no timestamp here"))
    check(False, "missing_timestamp: should have raised OwnershipCheckError")
except mobile_ownership.OwnershipCheckError:
    pass


# --- verify_ownership(): signature + actual ownership match --------------

class _FakeClient:
    pass


def _patch_current_owner(result_or_exc):
    def fake_build_client(url):
        return _FakeClient()

    def fake_current_owner(client, machine_id):
        if isinstance(result_or_exc, Exception):
            raise result_or_exc
        return result_or_exc

    peaq_ownership.build_client = fake_build_client
    peaq_ownership.current_owner = fake_current_owner


_orig_build_client = peaq_ownership.build_client
_orig_current_owner = peaq_ownership.current_owner


class _FakePubkey:
    """Stands in for a solders.Pubkey - only needs str() to match pubkey_str."""

    def __init__(self, s):
        self._s = s

    def __str__(self):
        return self._s


# 9. verify_ownership: signature valid AND connected wallet IS the
# current owner -> must succeed (no exception).
msg9 = mobile_ownership.build_challenge()
_patch_current_owner(_FakePubkey(pubkey_str))
try:
    mobile_ownership.verify_ownership(pubkey_str, msg9, sign(msg9), "https://fake-rpc", 12345)
except mobile_ownership.OwnershipCheckError as e:
    check(False, "owner_match: should have succeeded, raised %r" % e)

# 10. verify_ownership: signature valid but connected wallet is NOT the
# current owner -> must raise, never silently grant access.
msg10 = mobile_ownership.build_challenge()
_patch_current_owner(_FakePubkey("SomeOtherWalletPubkeyXYZ"))
try:
    mobile_ownership.verify_ownership(pubkey_str, msg10, sign(msg10), "https://fake-rpc", 12345)
    check(False, "owner_mismatch: should have raised OwnershipCheckError")
except mobile_ownership.OwnershipCheckError:
    pass

# 11. verify_ownership: underlying ownership lookup itself fails (e.g.
# peaq RPC down) -> must surface as OwnershipCheckError, not crash, and
# must NOT grant access.
msg11 = mobile_ownership.build_challenge()
_patch_current_owner(peaq_ownership.OwnerLookupError("rpc unreachable"))
try:
    mobile_ownership.verify_ownership(pubkey_str, msg11, sign(msg11), "https://fake-rpc", 12345)
    check(False, "lookup_failure: should have raised OwnershipCheckError")
except mobile_ownership.OwnershipCheckError:
    pass

# 12. verify_ownership: bad signature must short-circuit BEFORE any
# ownership lookup happens (defense in depth - a forged request should
# never even reach the chain-read step).
msg12 = mobile_ownership.build_challenge()


def _should_not_be_called(*a, **k):
    check(False, "bad_signature: ownership lookup was reached despite invalid signature")
    return _FakePubkey(pubkey_str)


peaq_ownership.build_client = lambda url: _FakeClient()
peaq_ownership.current_owner = _should_not_be_called
try:
    mobile_ownership.verify_ownership(pubkey_str, msg12, bad_sig, "https://fake-rpc", 12345)
    check(False, "bad_signature: should have raised OwnershipCheckError")
except mobile_ownership.OwnershipCheckError:
    pass

peaq_ownership.build_client = _orig_build_client
peaq_ownership.current_owner = _orig_current_owner

print()
if fails:
    print("FAILED CHECKS:")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("All checks passed.")
