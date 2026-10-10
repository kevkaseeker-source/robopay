"""Server-side wallet actions for the buyer app's own (server-held) wallet:
send SOL on Solana devnet, send PEAQ and transfer a Machine-NFT on peaq
mainnet. The car owner's keys never come here - owners sign in their
browser (car/static/robopay_wallet.js) or in the CarOwnerApp.
"""
from solders.message import Message
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import Transaction

PEAQ_CHAIN_ID = 3338
MACHINE_REGISTRY = "0x64b93Cc29b251fAFa83BD110cDB1C24207f85536"  # peaq MachineRegistry (Tokenomics 2.0)
_REGISTRY_ABI = [
    {"name": "ownerOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "tokenId", "type": "uint256"}], "outputs": [{"name": "", "type": "address"}]},
    {"name": "transferFrom", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "from", "type": "address"}, {"name": "to", "type": "address"},
                {"name": "tokenId", "type": "uint256"}], "outputs": []},
]


def send_sol(rpc, keypair, to: str, amount_sol: float) -> str:
    """Plain SOL transfer; returns the signature."""
    ix = transfer(TransferParams(from_pubkey=keypair.pubkey(), to_pubkey=Pubkey.from_string(to),
                                 lamports=int(round(amount_sol * 1_000_000_000))))
    blockhash = rpc.get_latest_blockhash().value.blockhash
    tx = Transaction([keypair], Message([ix], keypair.pubkey()), blockhash)
    return str(rpc.send_transaction(tx).value)


class PeaqWallet:
    def __init__(self, private_key: str, rpc_url: str, chain_id: int = PEAQ_CHAIN_ID):
        from eth_account import Account
        from web3 import Web3
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        self.acct = Account.from_key(private_key)
        self.address = self.acct.address
        self.chain_id = chain_id

    def balance(self) -> float:
        return self.w3.eth.get_balance(self.address) / 1e18

    def _send(self, tx: dict) -> str:
        tx.setdefault("nonce", self.w3.eth.get_transaction_count(self.address))
        tx.setdefault("chainId", self.chain_id)
        tx.setdefault("gasPrice", self.w3.eth.gas_price)
        h = self.w3.eth.send_raw_transaction(self.acct.sign_transaction(tx).raw_transaction)
        self.w3.eth.wait_for_transaction_receipt(h, timeout=120)
        return "0x" + h.hex().removeprefix("0x")

    def send_peaq(self, to: str, amount: float) -> str:
        to = self.w3.to_checksum_address(to)
        if to == self.address:
            raise ValueError("that is this wallet's own address")
        return self._send({"to": to, "value": int(round(amount * 1e18)), "gas": 21000})

    def transfer_nft(self, machine_id: int, to: str) -> str:
        to = self.w3.to_checksum_address(to)
        reg = self.w3.eth.contract(address=self.w3.to_checksum_address(MACHINE_REGISTRY), abi=_REGISTRY_ABI)
        if reg.functions.ownerOf(machine_id).call() != self.address:
            raise ValueError("this wallet does not own that Machine-NFT")
        if to == self.address:
            raise ValueError("that is this wallet's own address")
        if len(self.w3.eth.get_code(to)) > 0:
            raise ValueError("recipient is a contract, not a wallet - refusing")
        fn = reg.functions.transferFrom(self.address, to, machine_id)
        fn.call({"from": self.address})  # simulate first
        return self._send(fn.build_transaction({"from": self.address, "gas": fn.estimate_gas({"from": self.address})}))
