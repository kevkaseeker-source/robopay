# Drone — Unit B

Ref: paper §6.1–6.2 ("System Architecture" / "Hardware & Network Architecture", Figure 13)
and §7.4–7.6 (GPS hardware fix, outdoor delivery architecture, trust model).

## Role

Unit B is the existing, proven carrier node: a Raspberry Pi 4 mounted on a drone,
GPS-triggered `confirm_delivery` via its own Solana wallet. Implementation lives in
[`../rpi/`](../rpi/) (`main.py`, `pixhawk_bridge.py`, `solana_client.py`, `config.py`)
and [`../anchor/`](../anchor/) (escrow program). This folder is for drone-specific
hardware/flight notes as outdoor work continues, kept separate from the generic node
code.

- Raspberry Pi 4 + Pixhawk flight controller (MAVLink via `/dev/serial0`).
- GPS + 4G via Waveshare SIM7600X HAT (`AT+CGPSINFO` on `/dev/ttyUSB2`, pppd for LTE).
- Client-side arrival check: Haversine, `ARRIVAL_RADIUS_M = 13.0m`.
- **Known limitation** (§7.5): effective ~0.5Hz GPS update rate → up to ~20m positional
  uncertainty between fixes at 10m/s cruise speed. Accounted for by the drone
  decelerating on approach; worth tuning arrival radius / approach speed in outdoor trials.

## Status

- [x] Indoor PoC — GPS geofence triggers escrow release
- [x] Outdoor test — geofence trigger + TX confirmation (~4s)
- [ ] 2-of-2 co-signing with Unit A (letterbox) — see [`../rpi/LETTERBOX_IMPLEMENTATION.md`](../rpi/LETTERBOX_IMPLEMENTATION.md)
- [ ] Outdoor trials accounting for GPS sampling-rate limitation
