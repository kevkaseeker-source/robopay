#!/usr/bin/env python3
"""Rehearse the Machine-NFT handoff on a THROWAWAY peaq machine (mainnet).

Unit C's real NFT never moves here. This creates a separate test machine
owned by test wallet A, hands it to test wallet B the same way the
CarOwnerApp will, and back - so every contract call is proven on the exact
contracts the real handoff uses, for well under 1 PEAQ (Unit C's own
onboarding cost 0.47 PEAQ).

Test keys live only in ~/.robopay-rehearsal/ (mode 600) and hold a few PEAQ
at most. Kevin's own key is asked for exactly once, by `fund`, via getpass,
and never stored.

peaq_os_sdk is Linux-only -> run in WSL:
    cd /mnt/c/Users/kevka/robopay-part2/rpi
    PY=~/.venvs/robopay-peaq/bin/python
    $PY peaq_rehearsal.py setup            # create test wallets A/B (no cost)
    $PY peaq_rehearsal.py fund             # Kevin sends PEAQ to A and B (asks for his key)
    $PY peaq_rehearsal.py onboard          # A creates the test machine (~0.5 PEAQ)
    $PY peaq_rehearsal.py status
    $PY peaq_rehearsal.py transfer A B     # handoff step 1 (A signs)
    $PY peaq_rehearsal.py takeover B       # steps 2+3 back to back (B signs), then verify
    $PY peaq_rehearsal.py plant A          # optional: old owner plants a DID entry (attack demo)
    $PY peaq_rehearsal.py transfer B A && $PY peaq_rehearsal.py takeover A
    $PY peaq_rehearsal.py sweep            # return leftover PEAQ to Kevin's wallet
"""
import getpass
import json
import os
import secrets
import sys
from pathlib import Path

from solders.keypair import Keypair

import peaq_ownership as po
from set_payout_wallet import build_signing_client, encode_solana_multibase

RPC = os.getenv("PEAQ_RPC_URL", "https://peaq.api.onfinality.io/public")
CHAIN_ID = 3338
FUNDER = os.getenv("REHEARSAL_FUNDER", "0x4d99BeAD5A4CCE20a7F93CB2CF62f1847263Ea8f")
HOME = Path(os.getenv("REHEARSAL_HOME", Path.home() / ".robopay-rehearsal"))
STATE = HOME / "state.json"
METHOD_ID, METHOD_TYPE = "#solana-owner", "Ed25519VerificationKey2020"


# ------------------------------------------------------------------ local state
def load():
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def save(state):
    HOME.mkdir(mode=0o700, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2))
    STATE.chmod(0o600)


def wallet(state, who):
    """(evm_private_key, evm_address, solana_keypair) of test wallet A or B."""
    w = state["wallets"][who]
    sol = Keypair.from_bytes(bytes(json.loads((HOME / w["solana_file"]).read_text())))
    return (HOME / w["evm_file"]).read_text().strip(), w["evm"], sol


def confirm(question):
    if input(question + " Type JA: ").strip() != "JA":
        sys.exit("Cancelled. Nothing sent.")


def w3():
    from web3 import Web3
    return Web3(Web3.HTTPProvider(RPC))


def peaq(addr):
    return w3().eth.get_balance(w3().to_checksum_address(addr)) / 1e18


def send_peaq(key, to, amount_peaq):
    from eth_account import Account
    web3 = w3()
    acct = Account.from_key(key)
    tx = {"to": web3.to_checksum_address(to), "value": int(amount_peaq * 1e18), "gas": 21000,
          "gasPrice": web3.eth.gas_price, "nonce": web3.eth.get_transaction_count(acct.address),
          "chainId": CHAIN_ID}
    h = web3.eth.send_raw_transaction(acct.sign_transaction(tx).raw_transaction)
    web3.eth.wait_for_transaction_receipt(h, timeout=120)
    return h.hex()


def did_entry(owner_evm, solana_pubkey):
    from peaq_os_sdk import Address, VerificationMethodInput
    return VerificationMethodInput(id=METHOD_ID, method_type=METHOD_TYPE, controller=Address(owner_evm),
                                   public_key_multibase=encode_solana_multibase(solana_pubkey))


# ------------------------------------------------------------------ commands
def cmd_setup(state):
    from eth_account import Account
    HOME.mkdir(mode=0o700, exist_ok=True)
    state.setdefault("wallets", {})
    for who in ("A", "B"):
        if who in state["wallets"]:
            continue
        acct = Account.create()
        sol = Keypair()
        (HOME / f"evm_{who}.key").write_text(acct.key.hex())
        (HOME / f"solana_{who}.json").write_text(json.dumps(list(bytes(sol))))
        for f in (f"evm_{who}.key", f"solana_{who}.json"):
            (HOME / f).chmod(0o600)
        state["wallets"][who] = {"evm": acct.address, "solana": str(sol.pubkey()),
                                 "evm_file": f"evm_{who}.key", "solana_file": f"solana_{who}.json"}
    save(state)
    for who, w in state["wallets"].items():
        print(f"Test owner {who}: peaq {w['evm']}  |  Solana payout {w['solana']}")
    print(f"Keys stored in {HOME} (mode 600). Next: fund")


def cmd_fund(state, amount_a="2", amount_b="1"):
    a, b = state["wallets"]["A"]["evm"], state["wallets"]["B"]["evm"]
    key = getpass.getpass(f"peaq private key of the funding wallet {FUNDER} (input hidden): ").strip()
    key = key if key.startswith("0x") else "0x" + key
    from eth_account import Account
    if Account.from_key(key).address.lower() != FUNDER.lower():
        sys.exit("ABORT: that key is not the funding wallet. Nothing sent.")
    confirm(f"Send {amount_a} PEAQ to A ({a}) and {amount_b} PEAQ to B ({b}) on peaq MAINNET?")
    print("A:", send_peaq(key, a, float(amount_a)))
    print("B:", send_peaq(key, b, float(amount_b)))
    del key
    print(f"Balances: A {peaq(a):.4f} PEAQ, B {peaq(b):.4f} PEAQ")


def cmd_onboard(state):
    from peaq_os_sdk import Address
    from peaq_os_sdk.tokenomics import SUBSCRIPTION_TIER_ENTRY
    from peaq_os_sdk.tokenomics.activation_types import ActivateMachineParams
    if state.get("machine_id"):
        sys.exit(f"Test machine already exists: {state['machine_id']}")
    key, evm, sol = wallet(state, "A")
    params = ActivateMachineParams(
        controller=Address(evm),
        verification_methods=(did_entry(evm, sol.pubkey()),),
        authentication=(),
        service_endpoints=(),
        machine_type="robopay-rehearsal",
        credential_subject=secrets.token_bytes(32),
        manufacturer=Address(evm),
        tier=SUBSCRIPTION_TIER_ENTRY,
        max_net_peaq_amount=int(1.5e18),
    )
    client = build_signing_client(RPC, key)
    preview = client.preview_machine_activation(params)
    print("Preview:", preview)
    confirm("Create the throwaway test machine on peaq MAINNET (irreversible, ~0.5 PEAQ)?")
    result = client.activate_machine(params)
    state["machine_id"] = str(result.machine_id)
    save(state)
    print(f"Test machine {result.machine_id} owned by A ({evm})")
    cmd_status(state)


def machine_id(state):
    if not state.get("machine_id"):
        sys.exit("No test machine yet - run onboard first.")
    return int(state["machine_id"])


def cmd_status(state):
    mid = machine_id(state)
    reader = po.build_client(RPC)
    s = reader.get_machine_management_state(mid)
    names = {w["evm"].lower(): who for who, w in state["wallets"].items()}
    names.update({w["solana"]: f"{who} (Solana)" for who, w in state["wallets"].items()})
    print(f"Test machine {mid}")
    print(f"  NFT owner      {s.owner}  [{names.get(str(s.owner).lower(), '?')}]")
    print(f"  DID controller {s.controller}  [{names.get(str(s.controller).lower(), '?')}]")
    for vm in s.did_document.verification_methods:
        key = po._decode_solana_multibase(vm.public_key_multibase)
        print(f"  DID entry {vm.id}: controller {vm.controller}, pays {key}  [{names.get(str(key), '?')}]")
    try:
        print(f"  => the car would pay: {po.current_owner(reader, mid)}  [{names.get(str(po.current_owner(reader, mid)), '?')}]")
    except po.OwnerLookupError as e:
        print(f"  => the car would refuse to pay: {e}")
    for who, w in state["wallets"].items():
        print(f"  balance {who}: {peaq(w['evm']):.4f} PEAQ")


def cmd_transfer(state, frm, to):
    from peaq_os_sdk import Address
    key, evm, _ = wallet(state, frm)
    to_evm = state["wallets"][to]["evm"]
    confirm(f"Transfer the TEST machine's NFT from {frm} to {to} ({to_evm})?")
    r = build_signing_client(RPC, key).transfer_machine(Address(evm), Address(to_evm), machine_id(state))
    print("transfer tx:", r.transaction_hash)
    cmd_status(state)


def cmd_takeover(state, who):
    """Handoff steps 2 and 3 back to back, then verify (closes the planted-entry window)."""
    from peaq_os_sdk import Address
    key, evm, sol = wallet(state, who)
    client, mid = build_signing_client(RPC, key), machine_id(state)
    r2 = client.set_machine_controller(mid, Address(evm))
    print("step 2 setController tx:", r2.transaction_hash)
    r3 = client.set_machine_verification_methods(mid, [did_entry(evm, sol.pubkey())])
    print("step 3 setVerificationMethods tx:", r3.transaction_hash)
    payee = po.current_owner(po.build_client(RPC), mid)
    print("VERIFIED: the car now pays", who if str(payee) == str(sol.pubkey()) else f"SOMEONE ELSE: {payee}")


def cmd_plant(state, who):
    """Attack demo: the current DID controller writes an entry that claims to
    belong to the OTHER wallet but pays itself (only possible before the new
    owner runs takeover)."""
    key, evm, sol = wallet(state, who)
    other = "B" if who == "A" else "A"
    confirm(f"Plant a fake DID entry as {who} (controller field = {other}, pays {who})?")
    r = build_signing_client(RPC, key).set_machine_verification_methods(
        machine_id(state), [did_entry(state["wallets"][other]["evm"], sol.pubkey())])
    print("plant tx:", r.transaction_hash)
    cmd_status(state)


def cmd_sweep(state):
    for who in ("A", "B"):
        key, evm, _ = wallet(state, who)
        web3 = w3()
        bal = web3.eth.get_balance(web3.to_checksum_address(evm))
        fee = 21000 * web3.eth.gas_price
        if bal > 2 * fee:
            print(who, "->", FUNDER, send_peaq(key, FUNDER, (bal - 2 * fee) / 1e18))
    cmd_status(state)


COMMANDS = {"setup": cmd_setup, "fund": cmd_fund, "onboard": cmd_onboard, "status": cmd_status,
            "transfer": cmd_transfer, "takeover": cmd_takeover, "plant": cmd_plant, "sweep": cmd_sweep}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        sys.exit(__doc__)
    COMMANDS[sys.argv[1]](load(), *sys.argv[2:])
