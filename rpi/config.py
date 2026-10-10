import os

PIXHAWK_CONNECTION = os.getenv("PIXHAWK_CONNECTION", "/dev/serial0")
PIXHAWK_BAUD = int(os.getenv("PIXHAWK_BAUD", "115200"))
HAT_GPS_PORT = os.getenv("HAT_GPS_PORT", "/dev/ttyUSB2")

# Drone operator pubkey — used by PC server (no keypair file needed on Windows)
DRONE_OPERATOR_PUBKEY = os.getenv("DRONE_OPERATOR_PUBKEY", "D7bPfAfRpAaYst6ec9GMbTtRmSi9uDZzXe5E41r9NndN")

ARRIVAL_RADIUS_M = float(os.getenv("ARRIVAL_RADIUS_M", "13.0"))

TARGET_LAT = float(os.getenv("TARGET_LAT", "52.3609"))
TARGET_LON = float(os.getenv("TARGET_LON", "14.0600"))

# Helius free tier (empfohlen): https://dev.helius.xyz → App erstellen → Devnet → API Key kopieren
# Dann env var setzen: SOLANA_RPC_URL=https://devnet.helius-rpc.com/?api-key=DEIN_KEY
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.devnet.solana.com")
WALLET_KEYPAIR_PATH = os.getenv("WALLET_KEYPAIR_PATH", "/home/kevkaakvek/.config/solana/id.json")

DRONE_PROGRAM_ID = os.getenv(
    "DRONE_PROGRAM_ID",
    "3NmsWVX39uvzG3PBNPdSe4FTgudqSeLphJSbMDhV5F8Y",
)

# Seller wallet pubkey — set this after running: solana-keygen new --outfile ~/seller.json
# Then: solana address --keypair ~/seller.json
SELLER_PUBKEY = os.getenv("SELLER_PUBKEY", "7v8TjCEV3n4n6wEQDESaKyyAbCM5c1uijiQNdoCWddbg")

# Tokenized ownership (docs/superpowers/specs/2026-09-23-tokenized-ownership-design.md).
# None until Unit C's peaq Machine-ID onboarding actually completes - with it
# unset, car_main.py and delivery_agent.py both fall back to SELLER_PUBKEY
# above exactly as before, unchanged.
MACHINE_ID = int(os.getenv("MACHINE_ID")) if os.getenv("MACHINE_ID") else None
# Who gets paid (rpi/payout_policy.py): "did" = the Machine-NFT owner's Solana
# wallet from the peaq DID (needs MACHINE_ID, fails closed); "static" = the
# fixed SELLER_PUBKEY above, only when set explicitly.
PAYOUT_MODE = os.getenv("PAYOUT_MODE", "did")
PEAQ_RPC_URL = os.getenv("PEAQ_RPC_URL", "https://peaq.api.onfinality.io/public")

# Shared secret for the buyer app's machine endpoints (/active_order,
# /delivered, ...), sent as a header on every call - see
# car/robopay_common.require_machine_token. Must match the server's value.
MACHINE_TOKEN = os.getenv("MACHINE_TOKEN", "")
MACHINE_HEADERS = {"X-RoboPay-Machine-Token": MACHINE_TOKEN} if MACHINE_TOKEN else {}

DELIVERY_AMOUNT_SOL = float(os.getenv("DELIVERY_AMOUNT_SOL", "0.20"))
DEADLINE_MINUTES = int(os.getenv("DEADLINE_MINUTES", "60"))

TELEMETRY_POLL_INTERVAL = float(os.getenv("TELEMETRY_POLL_INTERVAL", "1.0"))
