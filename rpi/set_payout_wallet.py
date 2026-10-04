#!/usr/bin/env python3
"""Point a machine's DID at the Solana wallet that should receive its income.

This is step 3 of docs/peaq-integration/ownership-transfer-guide.md, as a
guarded script instead of a hand-written JSON file: it writes ONE
Ed25519VerificationKey2020 entry ("#solana-owner") into the machine's DID,
which is exactly what rpi/peaq_ownership.current_owner() reads to decide
where the car pays out.

Run it yourself, as the current NFT owner / DID controller (it must be the
same address - see the guide's step 2). The peaq private key is read with
getpass (never echoed, never written anywhere) and the script refuses to
send anything unless the key's address is the machine's current owner AND
DID controller.

peaq_os_sdk is Linux-only (imports `pwd`), so on Windows run it in WSL:
    python3 -m venv ~/.venvs/robopay-peaq
    ~/.venvs/robopay-peaq/bin/pip install peaq-os-sdk==0.8.0 solders
    cd /mnt/c/Users/kevka/robopay-wallet-work/rpi
    ~/.venvs/robopay-peaq/bin/python set_payout_wallet.py <SOLANA_PUBKEY>

Note: this REPLACES the DID's whole verification-methods list (SDK
semantics). The script prints the current list first and aborts if it holds
anything besides a single Solana entry, so nothing else is silently dropped.
"""
from __future__ import annotations

import argparse
import getpass
import sys

from solders.pubkey import Pubkey

import peaq_ownership as po

DEFAULT_MACHINE_ID = 5149596011477982620423871887556457753159696362422273317835964893685329625592  # Unit C
DEFAULT_RPC = "https://peaq.api.onfinality.io/public"
METHOD_ID = "#solana-owner"
METHOD_TYPE = "Ed25519VerificationKey2020"
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    pad = len(data) - len(data.lstrip(b"\x00"))
    return "1" * pad + out


def encode_solana_multibase(pubkey: Pubkey) -> str:
    """Inverse of peaq_ownership._decode_solana_multibase."""
    return "z" + _b58encode(po._ED25519_MULTICODEC_PREFIX + bytes(pubkey))


def build_signing_client(rpc_url: str, private_key: str):
    from peaq_os_sdk import Address, PeaqosClient, Tokenomics20Config

    zero = Address("0x0000000000000000000000000000000000000000")
    return PeaqosClient(
        rpc_url=rpc_url,
        private_key=private_key,
        identity_registry=zero,
        identity_staking=zero,
        event_registry=zero,
        machine_nft=zero,
        did_registry=zero,
        batch_precompile=zero,
        tokenomics20=Tokenomics20Config(deployment_id="peaq-mainnet"),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("solana_pubkey", help="Solana wallet that should receive the machine's income")
    ap.add_argument("--machine-id", type=int, default=DEFAULT_MACHINE_ID)
    ap.add_argument("--rpc", default=DEFAULT_RPC)
    args = ap.parse_args()

    target = Pubkey.from_string(args.solana_pubkey)
    multibase = encode_solana_multibase(target)
    assert po._decode_solana_multibase(multibase) == target  # round-trip sanity check

    reader = po.build_client(args.rpc)
    state = reader.get_machine_management_state(args.machine_id)
    owner = str(state.owner)
    controller = str(state.controller)
    current = list(state.did_document.verification_methods)

    print(f"Machine:          {args.machine_id}")
    print(f"NFT owner:        {owner}")
    print(f"DID controller:   {controller}")
    for vm in current:
        print(f"Current DID entry: {vm.id} {vm.method_type} -> "
              f"{po._decode_solana_multibase(vm.public_key_multibase) if vm.method_type == METHOD_TYPE else vm.public_key_multibase}")
    if len(current) > 1 or any(vm.method_type != METHOD_TYPE for vm in current):
        print("ABORT: the DID holds more than a single Solana entry - this script would drop the "
              "others. Update it by hand (see ownership-transfer-guide.md).")
        return 1
    if owner.lower() != controller.lower():
        print("ABORT: NFT owner and DID controller differ - do step 2 (set_machine_controller) "
              "first, see ownership-transfer-guide.md.")
        return 1
    print(f"New payout wallet: {target}")

    key = getpass.getpass("peaq private key of the NFT owner (input hidden): ").strip()
    if not key.startswith("0x"):
        key = "0x" + key
    from eth_account import Account

    signer = Account.from_key(key).address
    if signer.lower() != owner.lower():
        print(f"ABORT: this key belongs to {signer}, not to the NFT owner {owner}. Nothing sent.")
        return 1

    if input(f"Write {target} into the DID of machine {args.machine_id}? Type JA: ").strip() != "JA":
        print("Cancelled. Nothing sent.")
        return 1

    from peaq_os_sdk import Address, VerificationMethodInput

    client = build_signing_client(args.rpc, key)
    del key
    result = client.set_machine_verification_methods(
        args.machine_id,
        [VerificationMethodInput(
            id=METHOD_ID,
            method_type=METHOD_TYPE,
            controller=Address(owner),
            public_key_multibase=multibase,
        )],
    )
    print(f"Sent: https://peaq.subscan.io/tx/{result.transaction_hash}")

    resolved = po.current_owner(reader, args.machine_id)
    print(f"Car will now pay: {resolved}")
    if resolved != target:
        print("WARNING: resolved payout wallet does not match - check the transaction.")
        return 1
    print("OK - DID points at the new payout wallet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
