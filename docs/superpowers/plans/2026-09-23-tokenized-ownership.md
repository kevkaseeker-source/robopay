# Tokenized Robot Ownership Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delivery income follows whoever currently holds Unit C's peaq Machine-NFT, instead of a hardcoded `SELLER_PUBKEY`.

**Architecture:** A new `rpi/peaq_ownership.py` reads the current Solana-native owner from peaq's Tokenomics 2.0 SDK (read-only, no private key). `car_main.py` and `delivery_agent.py` call it immediately before signing `confirm_delivery`, passing the resolved owner as a per-call override on a `SolanaClient.confirm_delivery()` that now accepts one. A failed lookup refuses the confirmation rather than falling back to the old static address.

**Tech Stack:** Python 3.13, `solders`/`solana` (already a dependency), `peaq_os_sdk` (new RPi dependency, install checked in Task 5).

**Spec:** `docs/superpowers/specs/2026-09-23-tokenized-ownership-design.md`

## Global Constraints

- Single owner per robot only. No multi-owner/fractional splitting (spec: explicitly out of scope).
- Off-chain lookup only for this PoC. No Anchor program change, no on-chain enforcement (spec: Phase 2, future work).
- A failed owner lookup **must refuse the confirmation**, never fall back to the old static `SELLER_PUBKEY` (spec: Error handling).
- `confirm_delivery()`'s new `seller` parameter must be optional and default to today's behavior exactly, so the existing fixed-QR path and all of `agent/selftest.py`'s existing 31 cases keep passing unmodified unless a case explicitly opts in.
- `machine_id` is `None` by default everywhere (peaq onboarding for Unit C is not complete yet - see spec Dependencies). With it unset, both `car_main.py` and `delivery_agent.py` must behave exactly as they do today - no lookup attempted, no new refusal path reachable.

---

## Task 1: `SolanaClient.confirm_delivery()` gains an optional seller override

**Files:**
- Modify: `rpi/solana_client.py`
- Create: `tests/test_solana_client_seller.py`

**Interfaces:**
- Produces: `solana_client.SolanaClient.confirm_delivery(self, actual_lat: float, actual_lon: float, timestamp: int | None = None, seller: Pubkey | None = None) -> str` (return type and all other behavior unchanged from today).
- Produces (for Task 2 onward to consume as the payment target): the `seller` parameter is a `solders.pubkey.Pubkey`.

There is no existing `tests/` directory in this repo yet; this task creates it. The test needs no network access - it only replaces the two RPC methods `confirm_delivery` calls (`get_latest_blockhash`, `send_transaction`) and uses the real, already-installed `solders` library for everything else, so it checks the actual instruction that would be sent.

- [ ] **Step 1: Write the failing test**

Create `tests/test_solana_client_seller.py`:

```python
#!/usr/bin/env python3
"""Unit test for SolanaClient.confirm_delivery()'s optional seller override.

No network access: only get_latest_blockhash and send_transaction are
stubbed. Keypair/Pubkey/Instruction/Message/Transaction are the real
solders library, so this checks the actual instruction that would be sent,
not a re-implementation of it.

Run: python3 tests/test_solana_client_seller.py
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "rpi"))

from solders.hash import Hash
from solders.keypair import Keypair
from solders.pubkey import Pubkey

import solana_client

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


class _FakeBlockhashResp:
    def __init__(self, blockhash):
        self.value = type("V", (), {"blockhash": blockhash})()


class _FakeSendResp:
    def __init__(self, sig):
        self.value = sig


class _FakeRpc:
    """Only the two calls confirm_delivery makes before we inspect the tx."""

    def get_latest_blockhash(self):
        return _FakeBlockhashResp(Hash.default())

    def send_transaction(self, tx):
        return _FakeSendResp("SIGtest111")


def _client_with_fake_rpc():
    keypair = Keypair()
    keyfile = Path(tempfile.mktemp(suffix=".json"))
    keyfile.write_text(json.dumps(list(bytes(keypair))))
    client = solana_client.SolanaClient(
        "http://unused.test", str(keyfile),
        "3NmsWVX39uvzG3PBNPdSe4FTgudqSeLphJSbMDhV5F8Y",
    )
    client._rpc = _FakeRpc()
    return client


# 1. Explicit seller override is used for the seller account.
client = _client_with_fake_rpc()
override = Pubkey.new_unique()
accounts = solana_client._confirm_delivery_accounts(
    client.derive_escrow_pda(), client._keypair.pubkey(), override,
)
check(accounts[2].pubkey == override,
      "explicit seller: wrong pubkey in the seller account slot")
check(accounts[2].is_signer is False, "explicit seller: seller must not be a signer")

# 2. confirm_delivery() itself accepts seller= and does not raise.
sig = client.confirm_delivery(52.3609, 14.06, seller=override)
check(sig == "SIGtest111", "confirm_delivery: unexpected return with seller override")

# 3. Omitting seller falls back to cfg.SELLER_PUBKEY, unchanged from today.
import config as cfg
client2 = _client_with_fake_rpc()
accounts_default = solana_client._confirm_delivery_accounts(
    client2.derive_escrow_pda(), client2._keypair.pubkey(),
    Pubkey.from_string(cfg.SELLER_PUBKEY),
)
sig2 = client2.confirm_delivery(52.3609, 14.06)
check(sig2 == "SIGtest111", "confirm_delivery: unexpected return without seller override")
check(accounts_default[2].pubkey == Pubkey.from_string(cfg.SELLER_PUBKEY),
      "default seller: did not fall back to cfg.SELLER_PUBKEY")

print()
if fails:
    print("FAILED CHECKS:")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("All checks passed.")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 tests/test_solana_client_seller.py`
Expected: `AttributeError: module 'solana_client' has no attribute '_confirm_delivery_accounts'` (or an `AttributeError` on the `seller=` kwarg) - `confirm_delivery` does not accept `seller` yet.

- [ ] **Step 3: Implement the minimal change**

In `rpi/solana_client.py`, add a module-level helper just above the `SolanaClient` class (after `_load_keypair`, before `class SolanaClient:`):

```python
def _confirm_delivery_accounts(escrow_pda: Pubkey, signer: Pubkey, seller: Pubkey) -> list:
    """The three accounts confirm_delivery signs against, isolated so the
    seller substitution (tokenized-ownership payout) is directly testable
    without inspecting a serialized Transaction."""
    return [
        AccountMeta(pubkey=escrow_pda, is_signer=False, is_writable=True),
        AccountMeta(pubkey=signer, is_signer=True, is_writable=True),
        AccountMeta(pubkey=seller, is_signer=False, is_writable=True),
    ]
```

Then change `SolanaClient.confirm_delivery` (replace its existing body from `escrow_pda = self.derive_escrow_pda()` through the `accounts = [...]` block):

```python
    def confirm_delivery(self, actual_lat: float, actual_lon: float, timestamp: int = None,
                          seller: Pubkey = None) -> str:
        """GPS-verified payment release: escrow → seller. Retries up to 3x with backoff.

        seller overrides cfg.SELLER_PUBKEY for this call only - used to pay
        the current peaq Machine-NFT owner instead of the static configured
        address. See docs/superpowers/specs/2026-09-23-tokenized-ownership-design.md.
        """
        ts = int(timestamp or time.time())
        data = (CONFIRM_DELIVERY_DISC
                + struct.pack("<qqq", int(actual_lat * 1e7), int(actual_lon * 1e7), ts))

        escrow_pda = self.derive_escrow_pda()
        seller_pubkey = seller if seller is not None else Pubkey.from_string(cfg.SELLER_PUBKEY)
        accounts = _confirm_delivery_accounts(escrow_pda, self._keypair.pubkey(), seller_pubkey)
        ix = Instruction(program_id=self._program_id, accounts=accounts, data=data)
```

(The rest of the method - building/sending/retrying the transaction - is unchanged.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 tests/test_solana_client_seller.py`
Expected: `All checks passed.`

- [ ] **Step 5: Commit**

```bash
git add rpi/solana_client.py tests/test_solana_client_seller.py
git commit -m "Add optional seller override to SolanaClient.confirm_delivery()"
```

---

## Task 2: `rpi/peaq_ownership.py` - resolve the current Machine-NFT owner

**Files:**
- Create: `rpi/peaq_ownership.py`
- Create: `tests/test_peaq_ownership.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `peaq_ownership.OwnerLookupError(RuntimeError)`. `peaq_ownership.build_client(peaq_rpc_url: str, solana_rpc_url: str)` returning a configured, keyless `peaq_os_sdk.PeaqosClient`. `peaq_ownership.current_owner(client, machine_id: int) -> solders.pubkey.Pubkey`, raising `OwnerLookupError` on any failure. Tasks 3 and 4 call `build_client(...)` once and pass its result into `current_owner(...)`.

This module imports `peaq_os_sdk` lazily (inside the functions, not at module level), so `import peaq_ownership` alone never requires the package to be installed - only calling `build_client()`/`current_owner()` does. The test below stubs `peaq_os_sdk` with a minimal fake and never imports the real package, so it runs even where `peaq_os_sdk` is not installed (e.g. today's RPi venv).

- [ ] **Step 1: Write the failing test**

Create `tests/test_peaq_ownership.py`:

```python
#!/usr/bin/env python3
"""Offline test for rpi/peaq_ownership.py. Stubs peaq_os_sdk - no real
install of it required to run this.

Run: python3 tests/test_peaq_ownership.py
"""
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "rpi"))


class _FakeParams:
    pass


def install_sdk_stub():
    m = types.ModuleType("peaq_os_sdk")
    m.SolanaMachineCurrentStateParams = _FakeParams
    sys.modules["peaq_os_sdk"] = m


install_sdk_stub()

import peaq_ownership  # noqa: E402  (stub must be installed first)
from solders.pubkey import Pubkey  # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


class _FakeMachineRecord:
    def __init__(self, owner):
        self.owner = owner


class _FakeNativeObservation:
    def __init__(self, machine):
        self.machine = machine


class _FakeState:
    def __init__(self, status, native):
        self.status = status
        self.native = native


class _FakeClient:
    def __init__(self, result):
        self._result = result  # a _FakeState, or an Exception to raise

    def get_machine_activation_state(self, machine_id, solana=None):
        assert solana is not None, "solana param not passed to get_machine_activation_state"
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


# 1. Happy path: a present machine returns its native owner.
owner_pk = Pubkey.new_unique()
state = _FakeState("present", _FakeNativeObservation(_FakeMachineRecord(owner_pk)))
result = peaq_ownership.current_owner(_FakeClient(state), 12345)
check(result == owner_pk, "happy: wrong owner returned")

# 2. Reserved but not yet natively onboarded: native is None.
state = _FakeState("reserved", None)
try:
    peaq_ownership.current_owner(_FakeClient(state), 12345)
    check(False, "reserved: should have raised OwnerLookupError")
except peaq_ownership.OwnerLookupError:
    pass

# 3. native present but its machine record is None.
state = _FakeState("unavailable", _FakeNativeObservation(None))
try:
    peaq_ownership.current_owner(_FakeClient(state), 12345)
    check(False, "unavailable: should have raised OwnerLookupError")
except peaq_ownership.OwnerLookupError:
    pass

# 4. The SDK call itself raises (RPC down, etc.) - must be wrapped, not leaked.
try:
    peaq_ownership.current_owner(_FakeClient(RuntimeError("rpc down")), 12345)
    check(False, "sdk_error: should have raised OwnerLookupError")
except peaq_ownership.OwnerLookupError:
    pass
except RuntimeError:
    check(False, "sdk_error: raw RuntimeError leaked instead of OwnerLookupError")

print()
if fails:
    print("FAILED CHECKS:")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("All checks passed.")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 tests/test_peaq_ownership.py`
Expected: `ModuleNotFoundError: No module named 'peaq_ownership'`

- [ ] **Step 3: Write the implementation**

Create `rpi/peaq_ownership.py`:

```python
"""Resolve a peaq Machine-ID's current Solana-native owner.

Used to pay delivery income to whoever currently holds the Machine NFT
(peaq's own transfer_machine()/ownership tracking) instead of a fixed
SELLER_PUBKEY. See
docs/superpowers/specs/2026-09-23-tokenized-ownership-design.md.

get_machine_owner() in peaq_os_sdk reads MachineRegistry.ownerOf() on the
peaq EVM chain and returns an 0x... Address - that is NOT usable as a
Solana payout target. The Solana-native owner only comes back through
get_machine_activation_state(..., solana=SolanaMachineCurrentStateParams()),
inside .native.machine.owner (confirmed by reading the installed SDK
source, 2026-09-23).

Deliberately raises on any failure rather than returning a fallback: paying
the wrong owner is worse than a refused, retryable confirmation. Callers
decide what "failure" means for them - car_main.py and delivery_agent.py
both treat OwnerLookupError as a refusal, not a crash.
"""
from __future__ import annotations

from solders.pubkey import Pubkey


class OwnerLookupError(RuntimeError):
    """current_owner() could not resolve a usable Solana owner."""


def build_client(peaq_rpc_url: str, solana_rpc_url: str):
    """Construct a keyless PeaqosClient for reading machine ownership.

    No private key: this only ever reads. PeaqosClient's constructor
    requires six "legacy" contract addresses even in Tokenomics mode, where
    they are unused (confirmed live against peaq-mainnet, 2026-09-22) -
    zero addresses are safe here.
    """
    from peaq_os_sdk import Address, PeaqosClient, Tokenomics20Config

    zero = Address("0x0000000000000000000000000000000000000000")
    return PeaqosClient(
        rpc_url=peaq_rpc_url,
        identity_registry=zero,
        identity_staking=zero,
        event_registry=zero,
        machine_nft=zero,
        did_registry=zero,
        batch_precompile=zero,
        tokenomics20=Tokenomics20Config(
            deployment_id="peaq-mainnet",
            creation_home="solana",
            solana_rpc_url=solana_rpc_url,
        ),
    )


def current_owner(client, machine_id: int) -> Pubkey:
    """Return the Solana-native owner of machine_id, or raise OwnerLookupError.

    Args:
        client: A client from build_client(), or an equivalent test double
            exposing get_machine_activation_state(machine_id, solana=...).
        machine_id: The onboarded Machine-ID (full-width uint256).
    """
    from peaq_os_sdk import SolanaMachineCurrentStateParams

    try:
        state = client.get_machine_activation_state(
            machine_id, solana=SolanaMachineCurrentStateParams()
        )
    except Exception as e:
        raise OwnerLookupError(
            "could not read machine activation state for %s: %s" % (machine_id, e)
        ) from e

    if state.native is None or state.native.machine is None:
        raise OwnerLookupError(
            "machine %s has no native Solana record yet (status=%s)"
            % (machine_id, state.status)
        )
    return state.native.machine.owner
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 tests/test_peaq_ownership.py`
Expected: `All checks passed.`

- [ ] **Step 5: Commit**

```bash
git add rpi/peaq_ownership.py tests/test_peaq_ownership.py
git commit -m "Add rpi/peaq_ownership.py: resolve a Machine-NFT's current Solana owner"
```

---

## Task 3: Config - `MACHINE_ID` and `PEAQ_RPC_URL`

**Files:**
- Modify: `rpi/config.py`

**Interfaces:**
- Produces: `cfg.MACHINE_ID: int | None` (default `None` - peaq onboarding for Unit C is not complete yet). `cfg.PEAQ_RPC_URL: str` (default `"https://peaq.api.onfinality.io/public"`, the same endpoint used throughout this session's peaq work).

No test file - this is plain config, exercised by Tasks 4 and 5's own tests via `os.environ`.

- [ ] **Step 1: Add the two settings**

In `rpi/config.py`, add after the existing `SELLER_PUBKEY` line:

```python
# Tokenized ownership (docs/superpowers/specs/2026-09-23-tokenized-ownership-design.md).
# None until Unit C's peaq Machine-ID onboarding actually completes - with it
# unset, car_main.py and delivery_agent.py both fall back to SELLER_PUBKEY
# above exactly as before, unchanged.
MACHINE_ID = int(os.getenv("MACHINE_ID")) if os.getenv("MACHINE_ID") else None
PEAQ_RPC_URL = os.getenv("PEAQ_RPC_URL", "https://peaq.api.onfinality.io/public")
```

- [ ] **Step 2: Verify it imports cleanly**

Run: `python3 -c "import sys; sys.path.insert(0, 'rpi'); import config; print(config.MACHINE_ID, config.PEAQ_RPC_URL)"`
Expected: `None https://peaq.api.onfinality.io/public`

- [ ] **Step 3: Commit**

```bash
git add rpi/config.py
git commit -m "Add MACHINE_ID/PEAQ_RPC_URL config for tokenized ownership"
```

---

## Task 4: Wire `car_main.py`'s fixed-QR path into the owner lookup

**Files:**
- Modify: `car/car_main.py:242-266` (the `_confirm` function) and its call site in `main()`.

**Interfaces:**
- Consumes: `solana_client._confirm_delivery_accounts` indirectly via `SolanaClient.confirm_delivery(..., seller=...)` (Task 1). `peaq_ownership.build_client`, `peaq_ownership.current_owner`, `peaq_ownership.OwnerLookupError` (Task 2). `cfg.MACHINE_ID`, `cfg.PEAQ_RPC_URL`, `cfg.SOLANA_RPC_URL` (Task 3, and the existing `cfg.SOLANA_RPC_URL`).

`car_main.py` has no automated test harness today (unlike `agent/delivery_agent.py`) - it is verified live, via its own documented `--demo --dry-run` mode. This task's verification step uses that existing, already-established method rather than inventing a new one.

- [ ] **Step 1: Add the import**

In `car/car_main.py`, find the existing import block:

```python
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "rpi"))
import config as cfg  # noqa: E402  (path insert must happen first)
from solana_client import SolanaClient  # noqa: E402
```

Add one line after it:

```python
import peaq_ownership  # noqa: E402
```

- [ ] **Step 2: Update `_confirm` and its call site**

Replace the existing `_confirm` function:

```python
def _confirm(solana, dry_run, lat, lon, order_id):
    if dry_run:
        log.info("[dry-run] Would send confirm_delivery TX here.")
        report_delivered(lat, lon, "dry-run-tx")
        return
    try:
        sig = solana.confirm_delivery(lat, lon)
        log.info("Explorer: %s", DEVNET_EXPLORER.format(sig))
        send_log(f"TX bestaetigt! {sig[:20]}...")
        report_delivered(lat, lon, sig)
    except RuntimeError as e:
        log.error("TX fehlgeschlagen: %s", e)
        send_log(f"TX FEHLER: {e}")
        return

    # The escrow PDA is one-per-operator and stays allocated (status=DELIVERED)
    # until explicitly closed - without this, the next create_delivery fails
    # with "already in use" (hit this 2026-09-17 during live testing: had to
    # close it by hand twice before realizing it needs to happen every time).
    try:
        close_sig = solana.close_escrow()
        log.info("Escrow closed, ready for next order: %s", close_sig)
    except Exception as e:
        log.warning("close_escrow failed (payment already succeeded, just cleanup): %s", e)
```

with:

```python
def _confirm(solana, dry_run, lat, lon, order_id):
    if dry_run:
        log.info("[dry-run] Would send confirm_delivery TX here.")
        report_delivered(lat, lon, "dry-run-tx")
        return

    seller = None
    if cfg.MACHINE_ID is not None:
        try:
            client = peaq_ownership.build_client(cfg.PEAQ_RPC_URL, cfg.SOLANA_RPC_URL)
            seller = peaq_ownership.current_owner(client, cfg.MACHINE_ID)
            log.info("Paying current Machine-NFT owner: %s", seller)
        except peaq_ownership.OwnerLookupError as e:
            log.error("Could not resolve the current owner, refusing to confirm: %s", e)
            send_log(f"TX abgelehnt: Besitzer konnte nicht ermittelt werden: {e}")
            return

    try:
        sig = solana.confirm_delivery(lat, lon, seller=seller)
        log.info("Explorer: %s", DEVNET_EXPLORER.format(sig))
        send_log(f"TX bestaetigt! {sig[:20]}...")
        report_delivered(lat, lon, sig)
    except RuntimeError as e:
        log.error("TX fehlgeschlagen: %s", e)
        send_log(f"TX FEHLER: {e}")
        return

    # The escrow PDA is one-per-operator and stays allocated (status=DELIVERED)
    # until explicitly closed - without this, the next create_delivery fails
    # with "already in use" (hit this 2026-09-17 during live testing: had to
    # close it by hand twice before realizing it needs to happen every time).
    try:
        close_sig = solana.close_escrow()
        log.info("Escrow closed, ready for next order: %s", close_sig)
    except Exception as e:
        log.warning("close_escrow failed (payment already succeeded, just cleanup): %s", e)
```

The call site (inside `main()`'s `while True:` loop, `_confirm(solana, args.dry_run, lat, lon, order_id)`) does not change - `_confirm` still takes the same four arguments; it reads `cfg.MACHINE_ID` itself rather than taking a fifth parameter, matching how it already reads `cfg.SELLER_PUBKEY` indirectly through `SolanaClient`.

- [ ] **Step 3: Verify unchanged behavior with `MACHINE_ID` unset**

Run: `~/robopay-venv/bin/python3 car/car_main.py --demo --dry-run` (or the venv path documented in `car/SETUP_AND_ARCHITECTURE.md`)
Expected: identical log output to before this task - reaches "[dry-run] Would send confirm_delivery TX here." without any "Paying current Machine-NFT owner" or "TX abgelehnt" line, since `cfg.MACHINE_ID` is `None` by default.

- [ ] **Step 4: Commit**

```bash
git add car/car_main.py
git commit -m "car_main.py: pay the current Machine-NFT owner when MACHINE_ID is configured"
```

---

## Task 5: Wire `delivery_agent.py`'s AI-agent path, extend `selftest.py`

**Files:**
- Modify: `agent/delivery_agent.py:88-97` (config block), `agent/delivery_agent.py:582-675` (`Sensors.confirm_delivery`)
- Modify: `agent/selftest.py`

**Interfaces:**
- Consumes: same as Task 4 (`peaq_ownership.build_client`, `current_owner`, `OwnerLookupError`; `cfg.PEAQ_RPC_URL`, `cfg.SOLANA_RPC_URL`).
- Produces: extends the offline test suite's `SolanaClientStub.confirm_delivery` signature to `(self, lat, lon, timestamp=None, seller=None)`, recording `("confirm", lat, lon, seller)` in `CALLS` (was `("confirm", lat, lon)` - existing checks only inspect `c[0]`, so this is compatible).

- [ ] **Step 1: Add the import**

`delivery_agent.py` already does `import config as cfg` (it needs `cfg.SOLANA_RPC_URL` for its own `SolanaClient` construction) - reuse Task 3's `cfg.MACHINE_ID`/`cfg.PEAQ_RPC_URL` directly in Step 2 below rather than re-declaring separate module-level constants here; redeclaring them would let the two consumers (this file and `car_main.py`) silently drift if the env var is ever parsed differently in one place than the other.

Add near the top with the other `rpi/`-sourced imports (after `from solana_client import SolanaClient  # noqa: E402`):

```python
import peaq_ownership  # noqa: E402
```

- [ ] **Step 2: Update `Sensors.confirm_delivery`**

Find this block (right after the "buyer app must still be advertising the same order" check and before `self.decision_reason = reason.strip()`):

```python
        # And the buyer app must still be advertising the same order.
        current = fetch_agent_order()
        if current is not None and current.get("escrow_tx") != self._order.get("escrow_tx"):
            return self._refuse("the buyer app has a different active order now")

        self.decision_reason = reason.strip()
```

Insert the owner lookup between those two blocks:

```python
        # And the buyer app must still be advertising the same order.
        current = fetch_agent_order()
        if current is not None and current.get("escrow_tx") != self._order.get("escrow_tx"):
            return self._refuse("the buyer app has a different active order now")

        seller = None
        if cfg.MACHINE_ID is not None:
            try:
                client = peaq_ownership.build_client(cfg.PEAQ_RPC_URL, cfg.SOLANA_RPC_URL)
                seller = peaq_ownership.current_owner(client, cfg.MACHINE_ID)
            except peaq_ownership.OwnerLookupError as e:
                return self._refuse(
                    "could not resolve the current Machine-NFT owner: %s" % e
                )

        self.decision_reason = reason.strip()
```

Then find the real (non-dry-run) sign call:

```python
        self.sign_attempted = True
        try:
            self.signature = self._solana.confirm_delivery(lat, lon)
```

and change it to:

```python
        self.sign_attempted = True
        try:
            self.signature = self._solana.confirm_delivery(lat, lon, seller=seller)
```

(The `dry_run` branch above it, which sets `self.signature = "dry-run-tx"` without calling `confirm_delivery` at all, is unchanged - `seller` is computed either way so both paths stay consistent, but only the real path uses it.)

- [ ] **Step 3: Run the existing suite to confirm nothing broke yet**

Run: `python3 agent/selftest.py`
Expected: `ModuleNotFoundError: No module named 'peaq_ownership'` - `install_stubs()` does not yet register a fake `peaq_ownership`, and the real one is not installed in this environment (matches Task 2's design: the module is fine to import, but `delivery_agent.py` unconditionally imports it at module level, so the test harness must stub it, same as it already does for `solana_client`).

- [ ] **Step 4: Stub `peaq_ownership` in the test harness**

In `agent/selftest.py:81-105`, `SolanaClientStub`'s `confirm_delivery` currently reads (line 90-96):

```python
    def confirm_delivery(self, lat, lon, timestamp=None):
        CALLS.append(("confirm", lat, lon))
        if SolanaClientStub.behaviour == "send_failed":
            raise RuntimeError("confirm_delivery send failed: boom")
        if SolanaClientStub.behaviour == "unconfirmed":
            raise RuntimeError("confirm_delivery TX sent but not confirmed after 3 attempts: SIGmaybe")
        return "SIGconfirm111"
```

Change its signature and recorded call to match Task 1's real signature:

```python
    def confirm_delivery(self, lat, lon, timestamp=None, seller=None):
        CALLS.append(("confirm", lat, lon, seller))
        if SolanaClientStub.behaviour == "send_failed":
            raise RuntimeError("confirm_delivery send failed: boom")
        if SolanaClientStub.behaviour == "unconfirmed":
            raise RuntimeError("confirm_delivery TX sent but not confirmed after 3 attempts: SIGmaybe")
        return "SIGconfirm111"
```

Add a new `PeaqOwnershipStub` class directly below `SolanaClientStub` (i.e. right after its closing `return "SIGpay111"` at line 105, before the module-level `CALLS = []` at line 107):

```python
class PeaqOwnershipStub:
    behaviour = "ok"           # ok | lookup_fails
    owner_pubkey = "5W7EiKuNZTfj8f3sxQPUnyMPXpJj7NjjE1uYyRCwPWgi"

    @staticmethod
    def resolve(machine_id):
        CALLS.append(("owner_lookup", machine_id))
        if PeaqOwnershipStub.behaviour == "lookup_fails":
            raise RuntimeError("machine has no native Solana record yet")
        return Pubkey.from_string(PeaqOwnershipStub.owner_pubkey)
```

In `install_stubs()` (line 109-124), add right before the function's final line (`sys.modules["solana_client"] = sc`, line 124), i.e. right after it:

```python
    po = types.ModuleType("peaq_ownership")
    po.OwnerLookupError = RuntimeError
    po.build_client = lambda peaq_rpc_url, solana_rpc_url: PeaqOwnershipStub()
    po.current_owner = lambda client, machine_id: PeaqOwnershipStub.resolve(machine_id)
    sys.modules["peaq_ownership"] = po
```

In `run_case(...)` (line 206-209), add a `peaq_owner_behaviour="ok"` parameter to the signature, next to the existing `pay_behaviour="ok"`:

```python
def run_case(name, script, argv=("--dry-run",), chain=None, orders=None, distance=18.5,
             camera="ok", behaviour="ok", loaded_pubkey=AGENT_OP, keypath=str(KEYFILE),
             raise_on_turn=0, env=None, expect_rc=0, expect_abort=None,
             second_opinion=None, pay_behaviour="ok", peaq_owner_behaviour="ok"):
```

Inside the body, right after `SolanaClientStub.pay_behaviour = pay_behaviour` (line 219), add:

```python
    PeaqOwnershipStub.behaviour = peaq_owner_behaviour
```

`run_case` never clears `os.environ` between cases (line 230-236 only ever sets keys, never deletes them) - since Task 6's two new cases are the only ones that set `MACHINE_ID` and they run last in file order, this is safe as written, but it is a latent test-isolation gap for whoever adds cases after them. Fix it while touching this block: right before the `for k, v in (env or {}).items():` loop (line 235), add:

```python
    os.environ.pop("MACHINE_ID", None)
```

so every case starts from `MACHINE_ID` unset unless its own `env` dict sets it, regardless of case order.

- [ ] **Step 5: Run the existing suite to confirm it still passes with `MACHINE_ID` unset**

Run: `python3 agent/selftest.py`
Expected: `All checks passed.` (31 cases, unchanged from before this task - none of the existing cases pass `MACHINE_ID` in `env`, so `delivery_agent.MACHINE_ID` stays `None` and the new lookup code path is never reached).

- [ ] **Step 6: Add the two new cases**

Append, right before the final `print()` / `if fails:` block:

```python
# 19. Tokenized ownership: lookup succeeds, resolved owner is paid instead
# of the default seller.
rc, tr, _ = run_case("tokenized_owner", [[LOOK], [SECOND_OPINION_TOOL], [CONFIRM()]], argv=(),
                     chain=[escrow_bytes(), escrow_bytes(), escrow_bytes(status=1)],
                     env={"MACHINE_ID": "12345"}, expect_rc=0)
check(tr and tr["decision"]["confirmed"], "tokenized_owner: should have confirmed")
check(any(c[0] == "owner_lookup" and c[1] == 12345 for c in CALLS),
      "tokenized_owner: owner lookup was not attempted")
check(any(c[0] == "confirm" and c[3] == Pubkey.from_string(PeaqOwnershipStub.owner_pubkey)
          for c in CALLS), "tokenized_owner: resolved owner was not passed to confirm_delivery")

# 20. Tokenized ownership: lookup fails - refuse, never sign.
rc, tr, _ = run_case("tokenized_owner_lookup_fails",
                     [[LOOK], [SECOND_OPINION_TOOL], [CONFIRM()], [text("Understood.")]],
                     env={"MACHINE_ID": "12345"}, peaq_owner_behaviour="lookup_fails",
                     expect_rc=2)
check(tr and not tr["decision"]["confirmed"],
      "tokenized_owner_lookup_fails: should have refused")
check(tr and any("could not resolve the current Machine-NFT owner" in r
                 for r in tr["decision"]["refusals"]),
      "tokenized_owner_lookup_fails: wrong or missing refusal reason")
check(not any(c[0] == "confirm" for c in CALLS),
      "tokenized_owner_lookup_fails: confirm_delivery was attempted anyway")
```

This needs `Pubkey` importable at module scope in `selftest.py` for the checks above - it already is (`class Pubkey` is defined at the top of the file).

- [ ] **Step 7: Run the full suite one more time**

Run: `python3 agent/selftest.py`
Expected: `All checks passed.` (33 cases total: the original 31 plus these two).

- [ ] **Step 8: Commit**

```bash
git add agent/delivery_agent.py agent/selftest.py
git commit -m "delivery_agent.py: pay the current Machine-NFT owner when MACHINE_ID is configured"
```

---

## Task 6: Note the RPi install-footprint question, no code change

**Files:** none - this is a documentation/verification task, not a code task.

The spec flags that `peaq_os_sdk` (~50 dependencies, confirmed this session on the WSL/PC venv) has never been installed on the RPi's own venv, and that this should be checked rather than assumed free. This task is that check, deferred to whenever the RPi is next reachable and Tasks 1-5 are otherwise complete - it does not block finishing them, since none of Tasks 1-5's tests require `peaq_os_sdk` to actually be installed anywhere (Task 2's test stubs it; Tasks 4-5's live-path code only imports it lazily, inside functions that are never called while `MACHINE_ID` is unset).

- [ ] **Step 1: When the RPi is next reachable, check install footprint**

```bash
ssh -i ~/.ssh/picarx_key picarx@<rpi-address> \
  "time ~/robopay-venv/bin/pip install --dry-run 'peaq-os-sdk[solana]' 2>&1 | tail -5; df -h /"
```

Record the download size and estimated time in `agent/README.md` or `car/SETUP_AND_ARCHITECTURE.md` (whichever already documents RPi-side setup steps) so a future session does not have to rediscover it. If it turns out to be impractical on the RPi's actual storage/network, that is itself a finding worth a follow-up design note - not something to silently work around.

- [ ] **Step 2: No commit for this task unless the check reveals something to document**

If Step 1 finds nothing surprising, this task ends without a commit - it was a verification, not a change.
