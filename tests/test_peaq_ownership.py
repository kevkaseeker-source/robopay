#!/usr/bin/env python3
"""Offline test for rpi/peaq_ownership.py. Stubs peaq_os_sdk - no real
install of it required to run this.

Run: python3 tests/test_peaq_ownership.py
"""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "rpi"))


class _FakeAddress:
    def __init__(self, value):
        self.value = value


class _FakeTokenomics20Config:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _FakePeaqosClient:
    """Stub PeaqosClient. Raises if _FakePeaqosClient.raise_on_init is set,
    to exercise build_client()'s own error wrapping."""

    raise_on_init = None

    def __init__(self, **kwargs):
        if _FakePeaqosClient.raise_on_init is not None:
            raise _FakePeaqosClient.raise_on_init
        self.kwargs = kwargs


def install_sdk_stub():
    m = types.ModuleType("peaq_os_sdk")
    m.Address = _FakeAddress
    m.PeaqosClient = _FakePeaqosClient
    m.Tokenomics20Config = _FakeTokenomics20Config
    sys.modules["peaq_os_sdk"] = m


install_sdk_stub()

import peaq_ownership  # noqa: E402  (stub must be installed first)
from solders.pubkey import Pubkey  # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


# A real, live-verified encode/decode pair (2026-09-26, against Unit C's
# actual onboarded machine): Solana pubkey 7VizNvqBSnHnP8ySnsjxxyUnBQCyb
# VnJHBDRyvaThXia's did:key-style multibase encoding. Using the real pair
# here (not just a synthetic one) means this test also catches a
# regression in the multicodec-prefix/base58 handling that a
# self-consistent round-trip test alone wouldn't.
REAL_OWNER_PUBKEY = "7VizNvqBSnHnP8ySnsjxxyUnBQCybVnJHBDRyvaThXia"
REAL_OWNER_MULTIBASE = "z6Mkkwz2yB5cnKnFVdp9UShop52mzyUq1P2eyC8MpCYUckVx"


_SENTINEL_USE_OWNER = object()


class _FakeVerificationMethod:
    def __init__(self, method_type, controller, public_key_multibase):
        self.method_type = method_type
        self.controller = controller
        self.public_key_multibase = public_key_multibase


class _FakeDidDocument:
    def __init__(self, verification_methods):
        self.verification_methods = verification_methods


class _FakeManagementState:
    def __init__(self, owner, verification_methods, controller=_SENTINEL_USE_OWNER):
        self.owner = owner
        # Defaults to matching `owner` - the common/legitimate case every
        # pre-existing test below represents. Tests that specifically
        # exercise a controller/owner mismatch (the real vulnerability
        # found 2026-09-27 - see rpi/peaq_ownership.py's current_owner())
        # pass a distinct value explicitly.
        self.controller = owner if controller is _SENTINEL_USE_OWNER else controller
        self.did_document = _FakeDidDocument(verification_methods)


class _FakeClient:
    def __init__(self, result):
        self._result = result  # a _FakeManagementState, or an Exception to raise

    def get_machine_management_state(self, machine_id):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


# 1. Happy path: the current owner has a matching Ed25519 verification
# method - decodes to the real, known Solana pubkey.
state = _FakeManagementState(
    owner="0xOwner1",
    verification_methods=[
        _FakeVerificationMethod("Ed25519VerificationKey2020", "0xOwner1", REAL_OWNER_MULTIBASE),
    ],
)
result = peaq_ownership.current_owner(_FakeClient(state), 12345)
check(str(result) == REAL_OWNER_PUBKEY, "happy: wrong owner decoded (got %s)" % result)

# 2. Multiple verification methods, but only one has the right type AND
# controller - it's correctly picked out from among non-matching entries
# (a wrong-type match and a right-type-wrong-controller match).
other_multibase = "z6MkiTBz1ymuepAQ4HEHYSF1H8quG5GLVVQR3djdX3mDooWp"  # unrelated example
state = _FakeManagementState(
    owner="0xOwner2",
    verification_methods=[
        _FakeVerificationMethod("EcdsaSecp256k1RecoveryMethod2020", "0xOwner2", "not-multibase-relevant"),
        _FakeVerificationMethod("Ed25519VerificationKey2020", "0xSomeoneElse", other_multibase),
        _FakeVerificationMethod("Ed25519VerificationKey2020", "0xOwner2", REAL_OWNER_MULTIBASE),
    ],
)
result = peaq_ownership.current_owner(_FakeClient(state), 12345)
check(str(result) == REAL_OWNER_PUBKEY, "multiple_methods: picked the wrong one")

# 3. Owner address comparison is case-insensitive (EVM addresses are
# commonly checksummed with mixed case; the DID's controller and the
# machine's actual owner should still match).
state = _FakeManagementState(
    owner="0xAbCdEf0000000000000000000000000000000001",
    verification_methods=[
        _FakeVerificationMethod(
            "Ed25519VerificationKey2020", "0xabcdef0000000000000000000000000000000001", REAL_OWNER_MULTIBASE
        ),
    ],
)
result = peaq_ownership.current_owner(_FakeClient(state), 12345)
check(str(result) == REAL_OWNER_PUBKEY, "case_insensitive: owner/controller case mismatch not tolerated")

# 4. Stale DID: the NFT was transferred (owner changed) but the DID still
# lists only the OLD owner's verification method - must refuse, not pay
# the old owner's still-listed Solana wallet.
state = _FakeManagementState(
    owner="0xNewOwner",
    verification_methods=[
        _FakeVerificationMethod("Ed25519VerificationKey2020", "0xOldOwner", REAL_OWNER_MULTIBASE),
    ],
)
try:
    peaq_ownership.current_owner(_FakeClient(state), 12345)
    check(False, "stale_did: should have refused (controller doesn't match current owner)")
except peaq_ownership.OwnerLookupError:
    pass

# 5. No verification methods at all.
state = _FakeManagementState(owner="0xOwner3", verification_methods=[])
try:
    peaq_ownership.current_owner(_FakeClient(state), 12345)
    check(False, "no_methods: should have raised OwnerLookupError")
except peaq_ownership.OwnerLookupError:
    pass

# 6. A verification method matches the owner but isn't an Ed25519 key type
# (e.g. it's the EVM controller's own secp256k1 method) - must not be
# mistaken for a Solana key.
state = _FakeManagementState(
    owner="0xOwner4",
    verification_methods=[
        _FakeVerificationMethod("EcdsaSecp256k1RecoveryMethod2020", "0xOwner4", "irrelevant"),
    ],
)
try:
    peaq_ownership.current_owner(_FakeClient(state), 12345)
    check(False, "wrong_type: should have raised OwnerLookupError")
except peaq_ownership.OwnerLookupError:
    pass

# 7. A matching Ed25519 method exists but its publicKeyMultibase is
# malformed - must raise OwnerLookupError, not crash with a raw exception.
for bad_multibase, label in [
    ("not-z-prefixed", "missing_z_prefix"),
    ("z!!!invalid-base58!!!", "invalid_base58"),
    ("z" + "1" * 10, "wrong_length_or_prefix"),
    (None, "not_a_string"),
    (12345, "not_a_string_int"),
]:
    state = _FakeManagementState(
        owner="0xOwner5",
        verification_methods=[
            _FakeVerificationMethod("Ed25519VerificationKey2020", "0xOwner5", bad_multibase),
        ],
    )
    try:
        peaq_ownership.current_owner(_FakeClient(state), 12345)
        check(False, "malformed_multibase[%s]: should have raised OwnerLookupError" % label)
    except peaq_ownership.OwnerLookupError:
        pass
    except Exception as e:
        check(False, "malformed_multibase[%s]: raw %r leaked instead of OwnerLookupError" % (label, e))

# 8. The SDK call itself raises (RPC down, etc.) - must be wrapped, not leaked.
try:
    peaq_ownership.current_owner(_FakeClient(RuntimeError("rpc down")), 12345)
    check(False, "sdk_error: should have raised OwnerLookupError")
except peaq_ownership.OwnerLookupError:
    pass
except RuntimeError:
    check(False, "sdk_error: raw RuntimeError leaked instead of OwnerLookupError")

# 9. build_client() itself fails (e.g. PeaqosClient's constructor raising
# for a bad URL or an SDK-internal reason) - must be wrapped, not leaked,
# so every caller can rely on catching only OwnerLookupError.
_FakePeaqosClient.raise_on_init = ValueError("bad rpc url")
try:
    peaq_ownership.build_client("https://peaq.example")
    check(False, "build_client_error: should have raised OwnerLookupError")
except peaq_ownership.OwnerLookupError:
    pass
except ValueError:
    check(False, "build_client_error: raw ValueError leaked instead of OwnerLookupError")
finally:
    _FakePeaqosClient.raise_on_init = None

# 10. build_client() happy path - just confirm it constructs without a
# solana_rpc_url argument (the old Solana-home signature is gone).
client = peaq_ownership.build_client("https://peaq.example")
check(isinstance(client, _FakePeaqosClient), "build_client_happy: did not return the PeaqosClient")


class _MalformedState:
    """A management-state double missing whatever attribute the test wants
    to simulate as absent - accessing it raises AttributeError, the same
    way a real object missing a field would, without needing a real
    peaq_os_sdk dataclass to construct a deliberately-incomplete one."""

    def __init__(self, **present):
        self._present = present

    def __getattr__(self, name):
        if name in self._present:
            return self._present[name]
        raise AttributeError(name)


# 11. Management state itself is missing the .owner field entirely (not
# just falsy - genuinely absent, e.g. an SDK response shape change) -
# must raise OwnerLookupError, not an unhandled AttributeError.
try:
    peaq_ownership.current_owner(_FakeClient(_MalformedState()), 12345)
    check(False, "missing_owner_field: should have raised OwnerLookupError")
except peaq_ownership.OwnerLookupError:
    pass
except AttributeError as e:
    check(False, "missing_owner_field: raw AttributeError leaked: %r" % e)

# 12. .owner is present but .did_document is missing - same requirement.
try:
    peaq_ownership.current_owner(_FakeClient(_MalformedState(owner="0xOwner6")), 12345)
    check(False, "missing_did_document: should have raised OwnerLookupError")
except peaq_ownership.OwnerLookupError:
    pass
except AttributeError as e:
    check(False, "missing_did_document: raw AttributeError leaked: %r" % e)

# 13. did_document.verification_methods is None (rather than an empty
# list) - must be treated the same as "no methods", not crash trying to
# iterate None.
state = _FakeManagementState(owner="0xOwner7", verification_methods=None)
try:
    peaq_ownership.current_owner(_FakeClient(state), 12345)
    check(False, "none_verification_methods: should have raised OwnerLookupError")
except peaq_ownership.OwnerLookupError:
    pass
except TypeError as e:
    check(False, "none_verification_methods: raw TypeError leaked: %r" % e)

# 14. Ambiguous: two Ed25519 methods both match the current owner, but
# with DIFFERENT keys - must refuse rather than silently picking the
# first one (a payment target must never be a guess).
distinct_multibase = "z6MkiTBz1ymuepAQ4HEHYSF1H8quG5GLVVQR3djdX3mDooWp"
state = _FakeManagementState(
    owner="0xOwner8",
    verification_methods=[
        _FakeVerificationMethod("Ed25519VerificationKey2020", "0xOwner8", REAL_OWNER_MULTIBASE),
        _FakeVerificationMethod("Ed25519VerificationKey2020", "0xOwner8", distinct_multibase),
    ],
)
try:
    peaq_ownership.current_owner(_FakeClient(state), 12345)
    check(False, "ambiguous: should have refused (two different matching keys)")
except peaq_ownership.OwnerLookupError:
    pass

# 14b. Two Ed25519 methods match the owner with the SAME key (e.g. a
# harmless duplicate entry) - not ambiguous, should resolve normally.
state = _FakeManagementState(
    owner="0xOwner9",
    verification_methods=[
        _FakeVerificationMethod("Ed25519VerificationKey2020", "0xOwner9", REAL_OWNER_MULTIBASE),
        _FakeVerificationMethod("Ed25519VerificationKey2020", "0xOwner9", REAL_OWNER_MULTIBASE),
    ],
)
result = peaq_ownership.current_owner(_FakeClient(state), 12345)
check(str(result) == REAL_OWNER_PUBKEY, "duplicate_same_key: should have resolved, not refused")

# 15. Degenerate owner values (None, empty string, the EVM zero address)
# must all be refused explicitly, even if a verification method happens
# to have a matching-looking controller - an unminted/burned NFT should
# never resolve to a payable owner.
for bad_owner, bad_controller, label in [
    (None, "None", "none_owner"),
    ("", "", "empty_string_owner"),
    ("0x" + "0" * 40, "0x" + "0" * 40, "zero_address_owner"),
]:
    state = _FakeManagementState(
        owner=bad_owner,
        verification_methods=[
            _FakeVerificationMethod("Ed25519VerificationKey2020", bad_controller, REAL_OWNER_MULTIBASE),
        ],
    )
    try:
        peaq_ownership.current_owner(_FakeClient(state), 12345)
        check(False, "degenerate_owner[%s]: should have raised OwnerLookupError" % label)
    except peaq_ownership.OwnerLookupError:
        pass

# 16. The live-proven attack (test machine, 2026-09-27): a former owner,
# after losing the NFT, plants a verification method whose OWN .controller
# field claims the NEW owner - but the machine-wide state.controller (the
# thing peaq actually gates DID writes on) was never reclaimed by the new
# owner, so it still points at the OLD owner. A per-entry-only check would
# be fooled by this (it was, before this fix); current_owner() must refuse.
state = _FakeManagementState(
    owner="0xNewOwner16",
    controller="0xOldOwner16",  # not reclaimed by the new owner yet
    verification_methods=[
        _FakeVerificationMethod("Ed25519VerificationKey2020", "0xNewOwner16", REAL_OWNER_MULTIBASE),
    ],
)
try:
    peaq_ownership.current_owner(_FakeClient(state), 12345)
    check(False, "controller_hijack: should have refused (state.controller != owner)")
except peaq_ownership.OwnerLookupError:
    pass

# 17. Legitimate case: the new owner has claimed DID control
# (set_machine_controller) so state.controller now matches state.owner -
# resolution proceeds normally, same as the original happy path.
state = _FakeManagementState(
    owner="0xNewOwner17",
    controller="0xNewOwner17",  # explicitly reclaimed - same as the default, but explicit here
    verification_methods=[
        _FakeVerificationMethod("Ed25519VerificationKey2020", "0xNewOwner17", REAL_OWNER_MULTIBASE),
    ],
)
result = peaq_ownership.current_owner(_FakeClient(state), 12345)
check(str(result) == REAL_OWNER_PUBKEY, "controller_reclaimed: should have resolved normally")

# 18. .owner and .did_document are both present, but .controller is
# missing entirely (SDK response shape change, not just falsy) - must
# raise OwnerLookupError, not an unhandled AttributeError.
malformed_but_has_did = _MalformedState(
    owner="0xOwner18",
    did_document=_FakeDidDocument(
        [_FakeVerificationMethod("Ed25519VerificationKey2020", "0xOwner18", REAL_OWNER_MULTIBASE)]
    ),
)
try:
    peaq_ownership.current_owner(_FakeClient(malformed_but_has_did), 12345)
    check(False, "missing_controller_field: should have raised OwnerLookupError")
except peaq_ownership.OwnerLookupError:
    pass
except AttributeError as e:
    check(False, "missing_controller_field: raw AttributeError leaked: %r" % e)

print()
if fails:
    print("FAILED CHECKS:")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("All checks passed.")
