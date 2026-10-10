"""Who gets paid for a delivery, and when the car must refuse.

Shared by the buyer app (which fixes the escrow's seller when the order is
placed) and the car (which checks that seller before driving and again
before paying). The escrow program only ever pays the seller stored at
create time (`WrongSeller` otherwise), so the rules here decide whether a
delivery happens at all - they can never redirect money on chain.

Rule (Kevin, 2026-10-05): the car's ownership must not change during a
delivery. The app blocks the NFT transfer while an order is open; if it is
transferred anyway (e.g. from another wallet), the car refuses to pay and
the buyer reclaims the escrow after its deadline.

PAYOUT_MODE:
  "did"    - pay the Machine-NFT owner's Solana wallet from the peaq DID
             (rpi/peaq_ownership.py). Fails closed: no MACHINE_ID or a
             failed lookup refuses; it never falls back to a static key.
  "static" - pay a fixed configured Solana key (the pre-DID behaviour).
             Must be chosen explicitly.

Pure apart from the peaq lookup, which is injectable for tests.
"""
import struct

from solders.pubkey import Pubkey

STATUS_PENDING, STATUS_DELIVERED, STATUS_CANCELLED = 0, 1, 2
STATUS_NAMES = {0: "PENDING", 1: "DELIVERED", 2: "CANCELLED"}
ESCROW_LEN = 138  # 8 discriminator + 3*32 pubkeys + 4*8 numbers + status + bump

_peaq = None      # peaq_ownership module, imported lazily (the SDK is heavy)
_clients = {}     # one SDK client per RPC URL


class PayoutRefused(Exception):
    """The car must not deliver or must not pay; the message says why."""


def decode_escrow(raw: bytes) -> dict:
    """Decode a DeliveryEscrow account (layout of lib.rs DeliveryEscrow)."""
    if len(raw) != ESCROW_LEN:
        raise ValueError("unexpected escrow size %d, expected %d" % (len(raw), ESCROW_LEN))
    amount, target_lat, target_lon, deadline = struct.unpack("<Qqqq", raw[104:136])
    return {
        "buyer": str(Pubkey(raw[8:40])),
        "seller": str(Pubkey(raw[40:72])),
        "drone_operator": str(Pubkey(raw[72:104])),
        "amount_lamports": amount,
        "target_lat_e7": target_lat,
        "target_lon_e7": target_lon,
        "deadline": deadline,
        "status": raw[136],
        "bump": raw[137],
    }


def read_escrow(rpc, escrow_pda: Pubkey):
    """Return the decoded escrow at escrow_pda, or None if no account exists."""
    info = rpc.get_account_info(escrow_pda).value
    return None if info is None else decode_escrow(bytes(info.data))


def _did_client(mode: str, machine_id, peaq_rpc_url: str):
    if mode != "did":
        raise PayoutRefused(f"unknown PAYOUT_MODE {mode!r} (use 'did' or 'static')")
    if machine_id is None:
        raise PayoutRefused("PAYOUT_MODE=did needs MACHINE_ID - refusing instead of paying a fixed wallet")
    global _peaq
    if _peaq is None:
        import peaq_ownership
        _peaq = peaq_ownership
    try:
        client = _clients.get(peaq_rpc_url)
        if client is None:
            client = _clients[peaq_rpc_url] = _peaq.build_client(peaq_rpc_url)
        return client
    except _peaq.OwnerLookupError as e:
        raise PayoutRefused(f"could not resolve the machine's current owner: {e}") from e


def resolve_payout_owner(mode: str, machine_id, peaq_rpc_url: str, static_pubkey: str) -> Pubkey:
    """Solana wallet that should receive this machine's income right now."""
    if mode == "static":
        return Pubkey.from_string(static_pubkey)
    client = _did_client(mode, machine_id, peaq_rpc_url)
    try:
        return _peaq.current_owner(client, int(machine_id))
    except _peaq.OwnerLookupError as e:
        raise PayoutRefused(f"could not resolve the machine's current owner: {e}") from e


def resolve_owner_details(mode: str, machine_id, peaq_rpc_url: str, static_pubkey: str):
    """(solana_pubkey, owner_evm_address or None) for display."""
    if mode == "static":
        return Pubkey.from_string(static_pubkey), None
    client = _did_client(mode, machine_id, peaq_rpc_url)
    try:
        return _peaq.current_owner_details(client, int(machine_id))
    except _peaq.OwnerLookupError as e:
        raise PayoutRefused(f"could not resolve the machine's current owner: {e}") from e


def check_order(escrow, owner: Pubkey, now: float) -> None:
    """Before the car drives: is this a live escrow that pays the current owner?"""
    if escrow is None:
        raise PayoutRefused("no escrow on chain for this order")
    if escrow["status"] != STATUS_PENDING:
        raise PayoutRefused("escrow is %s, not PENDING" % STATUS_NAMES.get(escrow["status"], escrow["status"]))
    if escrow["deadline"] <= now:
        raise PayoutRefused("escrow deadline has passed - the buyer can reclaim it")
    if escrow["seller"] != str(owner):
        raise PayoutRefused(
            f"escrow pays {escrow['seller']} but the car's current owner is {owner} - not delivering")


def check_payout(escrow, owner: Pubkey) -> Pubkey:
    """Right before paying: the owner must be the same as when the order was
    placed. Returns the wallet to pay (always the escrow's own seller)."""
    if escrow["seller"] != str(owner):
        raise PayoutRefused(
            f"ownership changed during the delivery (escrow pays {escrow['seller']}, "
            f"current owner is {owner}) - nobody is paid, the buyer can reclaim the escrow after its deadline")
    return Pubkey.from_string(escrow["seller"])
