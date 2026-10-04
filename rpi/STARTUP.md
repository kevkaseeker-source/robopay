# RPi / Delivery Node — Startup Guide

How the delivery node boots and connects. Read this before touching any
startup script. Two things vary independently: **4G connection mode** and
**deployment architecture**.

## 4G connection modes (choose ONE)

| Mode | Interface | Scripts | Notes |
|---|---|---|---|
| PPP dial-up (pppd) | `ppp0` | `startup.sh` → `start_tunnel.sh` | Classic mode. `pppd call 4g`, modem via `/dev/ttyUSB2`. |
| RNDIS USB sharing | `usb0` | `startup_rndis.sh` → `start_tunnel_rndis.sh` | Requires switching the SIM7600 HAT into RNDIS mode first: `echo -e 'AT+CUSBPIDSWITCH=9011,1,1\r' | sudo tee /dev/ttyUSB2`. |

## Deployment architectures (two generations)

### ARCHITECTURE A — Flask + ngrok ON the RPi (older)
Everything runs on the Raspberry Pi: `order_server.py` (Flask), ngrok tunnel,
and `main.py`.

```
startup.sh          # modem on + pppd/4G up, then tells you to run start_tunnel.sh
start_tunnel.sh     # order_server.py + ngrok (domain starless-morality-cranium.ngrok-free.dev:5000)
setup_autostart.sh  # ONE-TIME: installs systemd services for the whole A stack
```

### ARCHITECTURE B — Flask on PC, RPi polls it (current)
The order server runs on a PC (`order_server_pc.py`, Windows-compatible, no
keypair file needed) with its own ngrok tunnel. The RPi only brings up 4G and
runs `main.py`, which polls the PC server every 5 s.

```
PC:   order_server_pc.py + ngrok (same domain, port 5000)
RPi:  startup_rpi.sh   # 4G (pppd) + main.py with auto-restart loop
```

`main.py` points at `PC_SERVER_URL` in `main.py` (ngrok-free domain) — the
same tunnel the PC exposes.

## Current recommendation

**Architecture B + whichever 4G mode works for your SIM setup.** If the HAT is
in RNDIS mode use `startup_rndis.sh`/`start_tunnel_rndis.sh`; otherwise the
ppp0 pair. Scripts for Architecture A (`startup.sh`, `start_tunnel.sh`,
`setup_autostart.sh`) are kept for reference/rollback — the device may still
run them in the field.

## Files at a glance

| File | Purpose | Architecture |
|---|---|---|
| `main.py` | Delivery loop: poll server, GPS check, sign Solana TX | B (current) |
| `pixhawk_bridge.py` | GPS via SIM7600 HAT (AT+CGPSINFO), haversine | both |
| `solana_client.py` | Anchor escrow client (confirm_delivery etc.) | both |
| `order_server.py` | Flask server for the phone dapp (RPi version) | A |
| `order_server_pc.py` | Same server, PC/Windows version | B |
| `index.html` | Phone dapp frontend served by the Flask server | both |
| `config.py` | Env-var config (GPS ports, pubkeys, RPC, radius) | both |
| `balance_check.py` | Check devnet wallet balance | tool |
| `modem_on.py` | Power-cycle the SIM7600 HAT modem | both |
| `setup_autostart.sh` | One-time systemd autostart installer | A only |
| `startup.sh` / `start_tunnel.sh` | 4G (ppp0) + Flask/ngrok on RPi | A only |
| `startup_rndis.sh` / `start_tunnel_rndis.sh` | 4G (usb0/RNDIS) + Flask/ngrok on RPi | A only |
| `startup_rpi.sh` | 4G + main.py only (Flask on PC) | B |
| `requirements.txt` | Python deps | both |
