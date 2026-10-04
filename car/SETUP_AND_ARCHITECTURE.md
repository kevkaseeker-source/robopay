# Unit C (PiCar-X) — Setup & Architecture Notes

Written 2026-09-10 while doing the first full software setup on the physical
RPi (hostname `picarx`), documenting how the pieces actually talk to each
other end to end. Keep this updated as the design changes — it's meant to
answer "how does X actually reach Y" without having to re-read every script.

## 1. Physical / network setup

- Raspberry Pi 4B, Raspberry Pi OS 64-bit (Debian 13 "trixie"), Python 3.13.5
- SIM7600G-H USB dongle + GPS antenna mounted (SIM card not yet inserted —
  GPS/4G connectivity is not used for anything on Unit C right now, see §3)
- Robot HAT has its own Type-C charging port, separate from the RPi's own
  power — the RPi can stay on wall power while the HAT runs off its battery
  for motors/servos/ultrasonic. Main power switch must be on for the HAT's
  I2C peripherals to respond (confirmed via `i2cdetect -y 1`: nothing at
  power-off, device at `0x14` once the switch is on).
- SSH key auth set up for remote (non-interactive) control, in addition to
  the picarx password login.

## 2. Software stack installed on the RPi

System-wide (`/usr/local/lib/python3.13/dist-packages`, via `sudo pip3
install --break-system-packages`, Debian's PEP 668 lock requires this flag):

- `robot-hat` (branch `2.5.x`) — low-level HAT driver, official install.py
- `vilib` (main branch) — camera/QR/vision, official install.py. Note:
  `mediapipe`/`tflite-runtime` are skipped on Python 3.13 (no wheels yet) —
  only affects face/gesture recognition, **not** QR reading (pyzbar-based).
- `picar-x` (branch `2.1.x`) — motor/servo/ultrasonic driver

Isolated venv at `~/robopay-research-group/venv` (**no** `--system-site-packages`)
— used only for the Solana side:

- `solana==0.36.6`, `solders==0.26.0` (auto-resolved), `requests`
- Deliberately pinned old: `solana>=0.40` dropped the synchronous
  `solana.rpc.api.Client` entirely (only `AsyncClient` remains), which
  `rpi/solana_client.py` depends on. Installing it into the *system*
  site-packages also collided with the `solders` version already pulled in
  by `vilib`/`robot-hat`, corrupting both — hence a fully separate venv
  rather than trying to make one Python environment satisfy both stacks.
- Run any script that imports `solana_client` (i.e. `car_main.py`) with
  `~/robopay-research-group/venv/bin/python3`, not system `python3`.
  `picar_server.py` (no Solana import) keeps using system `python3`.

## 3. Data flow / how the pieces talk to each other

```
 Buyer's phone/browser                     PC server (order_server_pc.py,
        |  places order (lat/lon,               Kevin's PC, via ngrok)
        |  buyer_pubkey, escrow_tx)                     ^  |
        v                                                |  | GET /active_order
 create_delivery() on-chain                              |  | POST /delivered, /rpi_log
 (Anchor program, Devnet)                                |  v
                                                    car_main.py (RPi, venv python)
                                                           |
                                    polls over HTTP (localhost:8080)
                                                           |
                                                           v
                                                  picar_server.py (RPi, system python)
                                                    /camera/qr   -> Vilib QR read
                                                    /ultrasonic  -> Picarx distance read
```

`car_main.py` (new, this session) is the car's equivalent of the drone's
`rpi/main.py`. It does **not** talk to hardware directly — it polls
`picar_server.py`'s own HTTP endpoints on localhost, which is what actually
holds the `Picarx()`/`Vilib` singletons.

**Arrival detection, and why it's safe not to use real GPS here:** the
on-chain `confirm_delivery` instruction ([lib.rs:41-77](../anchor/programs/drone-delivery/src/lib.rs))
only checks that the lat/lon *we submit* are within tolerance of the
escrow's stored target — it has no independent way to verify the RPi is
actually where it claims. Whoever controls the RPi's signing key is
already a fully trusted oracle for GPS-based delivery, too (documented in
the paper, chapter 12, as the "RPi as sole signer of truth" limitation).
Given that, Unit C's physical trigger (QR code matching the order's
`escrow_tx`, read within 15–25cm via ultrasonic) is just a different,
*additional* real-world gate in front of the same trust boundary — when it
passes, `car_main.py` re-submits the order's own `lat`/`lon` values
(not real sensor GPS) to `confirm_delivery`. No smart-contract changes
were needed for this.

**Known limitation, not yet mitigated:** anyone with SSH access to the RPi
could call `confirm_delivery` directly, bypassing the QR/ultrasonic check
entirely — same limitation as the drone. Worth a line in the paper's
Security Analysis (ch. 12) as an accepted PoC-stage limitation, not a
solved problem.

## 4. Wallets (Devnet)

Two new keypairs generated directly on the RPi with `solders` (no Solana
CLI installed — not needed, `solders.keypair.Keypair()` + writing the
64-byte secret as a JSON array is the same file format the CLI produces):

| Role | Path (RPi) | Pubkey |
|---|---|---|
| Unit C operator (signs `confirm_delivery`, pays gas) | `~/.config/solana/picarx_operator.json` | `7VizNvqBSnHnP8ySnsjxxyUnBQCybVnJHBDRyvaThXia` |
| PiCarOwner / seller (receives the SOL payout) | `~/.config/solana/picarowner.json` | `7uoFeSG546UvK5HYyA97GVmJUTvrXWgGgkTgxspH4d1C` |

Operator wallet funded with 5 Devnet SOL via the manual web faucet
(`faucet.solana.com`) — the RPC-based `request_airdrop` call against
`api.devnet.solana.com` returned `Internal error` (that public faucet is
commonly rate-limited/overloaded).

`car_main.py` reuses the existing `rpi/config.py` env-var mechanism rather
than a separate config file (so it doesn't collide with the drone's
config): run it with `WALLET_KEYPAIR_PATH` and `SELLER_PUBKEY` env vars set
to the two paths/pubkeys above. "PiCarOwner" is our naming for what the
shared code calls `SELLER_PUBKEY`/`seller` — same on-chain field, just
labeled for clarity in Unit C's own logs/docs.

## 5. Bugs found and fixed in `picar_server.py` (this session)

1. **Flask reloader crash-loops the camera.** `vilib/vilib.py` sets
   `os.environ['FLASK_DEBUG'] = 'development'` as an import side effect.
   Since `picar_server.py` imports `vilib` before calling `app.run()`, this
   silently enabled Werkzeug's debug reloader, which re-execs the whole
   script in a child process — re-running `Vilib.camera_start()` while the
   parent still holds the camera open, crashing with `RuntimeError: Failed
   to acquire camera: Device or resource busy`. Fixed by passing
   `debug=False, use_reloader=False` explicitly to `app.run()`.
2. **`Vilib.qr_coder_reader()` doesn't exist** in the installed vilib
   version (0.3.19) — 500 error on `/camera/qr`. The real API is:
   call `Vilib.qrcode_detect_switch(True)` once after `camera_start()`,
   then read the continuously-updated `Vilib.detect_obj_parameter['qr_data']`
   (default value is the **string** `"None"`, not Python `None`, when
   nothing is detected — normalized to real `None` in a small `read_qr()`
   helper added to `picar_server.py`).

## 6. QR camera-detection limits (found 2026-09-11, live testing)

Dense QR codes (e.g. a Luma event-ticket QR encoding a long URL) reliably
failed to decode from the camera — tested across many distances (15-45cm),
lighting levels, `pyzbar`, OpenCV's built-in `QRCodeDetector`, and even
OpenCV's CNN-based `WeChatQRCode` detector (model weights downloaded from
`github.com/WeChatCV/opencv_3rdparty`) — all failed identically. A short/
simple QR code (a "https://scandit.com" demo QR) decoded on the first try,
same camera/lighting/distance. Root cause: QR **module density** (too many
small modules for this 5MP fixed-focus camera to resolve), not focus,
lighting, or a software bug.

**Design decision that follows from this (supersedes the original plan):**
The QR the camera reads is a **fixed, static marker on the delivery box**
(`BOX_QR_CODE` in `car_main.py`, default `"ROBOPAY-BOX-C"`) — the same one
every time, not generated per order. It only proves "the car found the
box"; which order is being fulfilled is tracked separately via the single
active order from `order_server_pc.py`. For the PoC, no physical cube is
needed — a phone just displaying the QR code is enough. This also means
the buyer-ordering flow doesn't need to generate/display a QR at all.
Consequence for later: the real `escrow_tx` (~88 base58 chars) must never
be used directly as QR content — it's exactly the kind of dense code that
fails here.

## 7. MCC (Staex connectivity) — set up 2026-09-11

**SIM as a full network interface:** the SIM7600G-H is a plain USB modem
(not the HAT variant with built-in ECM/RNDIS), so it needed a classic PPP
dial-up rather than just an AT-command PDP context:
- APN set via AT command: `AT+CGDCONT=1,"IP","iot.truphone.com"` on
  `/dev/ttyUSB2` (the AT-command port; `/dev/ttyUSB0` errors, `ttyUSB1`/
  `ttyUSB4` are silent/other-purpose ports — found by probing all 5 with a
  hard `timeout` wrapper, since a naive open-and-read can hang indefinitely
  on the wrong port).
- PPP config: `/etc/ppp/peers/staex-sim` (peer options) +
  `/etc/chatscripts/staex-sim.chat` (dial script, `ATD*99#`). Brought up
  with `sudo pppd call staex-sim` — creates `ppp0`, confirmed working with
  real internet (ping to 8.8.8.8, HTTP to bild.de) and does **not**
  override the existing default route (car stayed reachable over
  Ethernet/WLAN throughout — pppd logged "not replacing existing default
  route").
- `/etc/resolv.conf` fixed to `8.8.8.8`/`1.1.1.1` and locked with
  `chattr +i` — same fix as the MIBO fleet needed; without it MCC can't
  resolve the parent address ("No CAS address resolved").

**MCC install (`docs.staex.io`):**
```bash
curl -sL -o /tmp/staex-repo.noarch.deb https://packages.staex.io/linux/deb/staex-repo.noarch.deb
sudo apt-get install -y /tmp/staex-repo.noarch.deb
sudo apt-get update
sudo apt-get install -y mcc
```
**Init — the key trick for non-interactive init (SSH, no TTY prompt):**
`mcc init` normally prompts interactively for the network certificate and
private key. Both can be passed non-interactively instead: certificate as
a positional base64 argument, private key piped via `--stdin`. Do **not**
pipe the sudo password into the same command — it eats the stdin meant for
the private key. Cache sudo first (`sudo -S -v`), then run mcc init as a
plain `sudo` call so its stdin is free:
```bash
echo <sudo-password> | sudo -S -v
echo "<network-private-key-base64>" | sudo mcc init --force --stdin "<network-certificate-base64>"
sudo chmod 400 /etc/mcc/node-private-key.txt
sudo systemctl enable --now mcc
```
Credentials for this node came from Maksim (Staex) — not reused from any
other project's node.

**Parent connection:** a freshly-init'd node has no parent configured
(`/etc/mcc/mcc.conf` ships with `parents =` empty) — `mcc nodes` shows only
itself until one is set. Fixed by editing `mcc.conf`:
`parents = public.staex.io:9376`, then `sudo systemctl restart mcc`. Confirmed
via `sudo journalctl -u mcc`: `active parent is 88.99.68.57:9376`, and
`sudo mcc parent` returns the same.

**Result:** Unit C's MCC node is live — ID
`pbraybxmydh10prae3hdv87t0q7pwetgzrqdahbtckewnsb7tywg`, connected to parent
`88.99.68.57:9376`, service enabled (survives reboot).

**Not done yet:** no tunnel created for `picar_server.py`'s port 8080
(`mcc create-tunnel --targets tcp:8080 --remote-node <id>`) — needs a
`--remote-node` value (whichever node should be allowed to reach this car,
e.g. Kevin's own PC if it also runs MCC) that wasn't available this
session. Once created, the dashboard/API would be reachable at
`http://picar-x.staex:8080` per the docstring in `picar_server.py`,
matching the drone project's `http://picar-x.staex` naming convention.

**Migrated to Kevin's own "RoboPay" network — 2026-09-15.** The setup
above used a network Maksim (Staex) had provisioned under his own
account — fine to start, but every new node (e.g. a future backend
server) would've needed him to issue fresh credentials each time. Kevin
self-service-created a "RoboPay" network at cas.staex.io/new-network
under his own account instead, which lets him mint node credentials
himself going forward. Migrated the car onto it:
```bash
sudo systemctl stop mcc
sudo rm -rf /etc/mcc
echo "<new-network-private-key>" | sudo mcc init --force --stdin "<new-network-certificate>"
sudo chmod 400 /etc/mcc/node-private-key.txt
sudo sed -i 's/^parents = *$/parents = public.staex.io:9376/' /etc/mcc/mcc.conf
sudo systemctl start mcc
sudo reboot
```
This intentionally issues the car a **new node identity** (old node ID
`pbraybxmydh10...` is gone) — confirmed via `journalctl -u mcc`: `active
parent is 188.245.186.74:9376`. New node ID:
`r93awhdrreetty839ssk48bj227kxp9vw5ezgmm2aj9477sb0rwg`. Verified with a
full reboot afterward — all four services (`picar-server`, `car-trigger`,
`mcc`, `ppp-staex-sim`) came back up cleanly on the new network, same as
every prior autostart test. Any future node (backend server, etc.) should
join this same "RoboPay" network — Kevin can self-issue its credentials
from cas.staex.io without needing to ask Maksim again.

**Follow-up bug found 2026-09-15:** `/dev/ttyUSB2` (the AT-command port used
in `/etc/ppp/peers/staex-sim`) is not stable — the SIM7600 dongle's ttyUSB
numbering shifted after a replug/reboot (`ttyUSB2` disappeared, remaining
ports renumbered), so `ppp-staex-sim.service` crash-looped with `pppd:
unrecognized option '/dev/ttyUSB2'` (a confusing error for what's actually
"device doesn't exist"). Re-probed all `/dev/ttyUSB*` the same way as in
§7 and found the AT port had moved to `ttyUSB3`. Fixed properly this time
by pointing the peers file at the **stable** udev path instead of the
numbered one: `/dev/serial/by-id/usb-SimTech__Incorporated_SimTech__Incorporated_0123456789ABCDEF-if02-port0`
(`ls /dev/serial/by-id/` lists these — the `-if02-port0` suffix is the USB
interface number, which doesn't change on replug/renumbering the way the
`ttyUSB*` number does). If `ppp-staex-sim` ever crash-loops again with a
similar "unrecognized option" error, check `ls /dev/serial/by-id/` first —
the underlying device is very likely just fine.

## 8. systemd autostart — set up 2026-09-12

Two units, `/etc/systemd/system/picar-server.service` and `car-trigger.service`:
- `picar-server.service` — runs `python3 picar_server.py` as `User=root`
  (needed for I2C/GPIO hardware access), `Restart=on-failure`
- `car-trigger.service` — runs `car_main.py` via the venv python, as the
  `picarx` user, `After=picar-server.service` + `Requires=picar-server.service`
  so it only starts once the sensor server is up, `Restart=always` (so it
  loops back to "waiting for order" after each dry-run demo cycle instead
  of exiting once). Currently launches with `--demo --dry-run` — real
  (non-demo, non-dry-run) mode needs the PC order server actually running
  first (see §8 open items), and dry-run stays the safe default for
  something that starts unattended on every boot.

**Bug found and fixed:** the `picarx` library calls `os.getlogin()` during
`Picarx.__init__` (`picarx.py:48`) to tag its config file's owner. This
throws `OSError: [Errno -25] Unknown error -25` under systemd, because
`os.getlogin()` needs a controlling terminal/login session (queries utmp)
that a systemd service simply doesn't have — a well-known Python gotcha,
not specific to this library. It worked fine every time we'd started
`picar_server.py` manually over SSH, because an SSH session does have one,
which is exactly why this didn't show up until switching to systemd.
Patched in `/usr/local/lib/python3.13/dist-packages/picarx/picarx.py` to
`os.environ.get('USER') or os.environ.get('LOGNAME') or 'picarx'` instead.

**Verified with a real reboot** (not just `systemctl restart`): both
services come up with zero manual intervention. `car-trigger.service`
takes ~20-40s longer than `picar-server.service` to actually start after
boot — it waits on `network-online.target`, which takes a bit to resolve
over WiFi (association + DHCP) — this is expected, not a bug; give it
under a minute after power-on before expecting it to be polling.

**Also verified by an unplanned power loss** (2026-09-12, car battery/RPi
lost power mid-session): `picar-server`, `car-trigger`, and `mcc` all came
back on their own with zero intervention — a stronger test than the
scripted reboot, since it wasn't a clean shutdown. Only the PPP/SIM
connection (§7) didn't come back, because it had only ever been started
manually (`nohup pppd ...`), never as a systemd unit. Fixed the same way:
`/etc/systemd/system/ppp-staex-sim.service` running
`/usr/sbin/pppd call staex-sim` (the peers file already has `nodetach` +
`persist`, so a plain `Type=simple` unit with `Restart=always` is enough
— no wrapper script needed). All four services — `picar-server`,
`car-trigger`, `mcc`, `ppp-staex-sim` — are now `systemctl enable`d and
confirmed to survive both a clean reboot and a hard power loss.

## 9. Current status / open next steps

- [x] robot-hat / vilib / picar-x installed, hardware confirmed live
      (ultrasonic + I2C tested with the car's main power on)
- [x] `picar_server.py` fixed and running, all endpoints verified
      (`/health`, `/ultrasonic`, `/camera/qr`, `/dashboard`)
- [x] `car_main.py` written, demo+dry-run polling loop verified end-to-end,
      updated to match against a fixed `BOX_QR_CODE` (see §6)
- [x] Unit C Devnet wallets generated and operator wallet funded
- [x] Live QR test done — confirmed a real camera-detection limitation
      (§6), not a bug; simple/short QR codes decode fine
- [x] SIM card (Staex M2M) physically inserted, working as a full PPP
      network interface, MCC installed and connected to its parent (§7)
- [ ] Owner-side keypair (`picarowner.json`) still lives on the RPi —
      should move to Kevin's own wallet/phone before any real MWA dApp
      login flow is built (see plan discussed 2026-09-11, not yet actioned)
- [ ] MCC tunnel for port 8080 not yet created (needs a remote-node id, §7)
- [ ] Real (non-dry-run) `confirm_delivery` TX not yet fired end-to-end
- [ ] No phone-ordering flow exists yet for Unit C specifically — right now
      `car_main.py --demo` uses a fixed local order for testing. Wiring it
      to a real PC-side order server (reusing `order_server_pc.py`, which
      is generic enough to work as-is) is a separate follow-up. Since the
      QR is now static (§6), the order flow no longer needs to generate a
      QR at all — simplifies this step.
- [ ] Owner-side Solana Mobile dApp (MWA wallet-connect, live
      video/telemetry over the MCC tunnel once it exists, drive controls)
      — architecture discussed 2026-09-11, not yet started
- [x] systemd autostart for `picar_server.py` + `car_main.py`, verified
      with a real reboot — power on car, wait under a minute, everything
      runs with no SSH/terminal needed (§8). Currently in `--demo --dry-run`
      mode by design (see §8) until the real order flow exists.
- [x] Real order app (`car/order_app_pc.py`) built and the first real
      end-to-end delivery completed on Devnet (§10)

## 10. First real end-to-end delivery — 2026-09-16

**`car/order_app_pc.py`** — a new Flask app, runs on the buyer's own
machine (not the RPi — buyer is a separate role, see §"Migrated to
Kevin's own RoboPay network" for the same reasoning applied to wallets).
Holds a **fixed buyer Devnet keypair** (`HKSt5XDrvupqbkj8JG8wyX4jJnf3aXdbjGp6zXUckEXD`,
per Kevin's requirement that the buyer identity stay constant rather than
being generated per order) and implements `create_delivery`/`cancel_delivery`
client-side (this repo previously only had these for the drone in
`solana_client.py`, which only wraps `confirm_delivery`/`close_escrow` —
the buyer-side instructions didn't have a Python client yet). Serves the
same `/active_order` etc. API shape as the drone's `order_server_pc.py`,
so `car_main.py` needed no changes beyond pointing `PC_SERVER_URL` at it.
Page also embeds the car's live camera/ultrasonic (reads `picar_server.py`
directly, needs `PICAR_SERVER_URL` set and the two to be on the same
network), shows both wallets' live Devnet balances, a transaction table
(labeled "Buyer TX" / "Escrow-Release TX" / "Cancel TX" — the underlying
instruction names are `create_delivery`/`confirm_delivery`/`cancel_delivery`),
and drive/camera-angle controls that POST straight to `picar_server.py`.
State (active order + tx history) persists to `order_state.json` next to
the script — an earlier version kept this in memory only and lost track
of a real pending order on every restart of the Flask process, which
matters because the escrow PDA is one-per-operator (see lib.rs) — you
can't just place a new order over a forgotten pending one.

**Two real bugs found getting the first live delivery to work:**
1. **CORS blocked the dashboard's sensor/control calls.** The order app
   and `picar_server.py` run on different host:port, so browser JS
   `fetch()` calls are cross-origin. Images (`<img src=".../mjpg">`)
   aren't subject to CORS, which is why the camera feed "just worked"
   while `/ultrasonic` and `/camera/qr` silently returned nothing — and
   separately, POST calls (`/drive`, `/camera/angle`) need CORS
   *preflight* handling (`Access-Control-Allow-Methods`/`-Headers` on the
   `OPTIONS` response), not just `Access-Control-Allow-Origin` on the
   real response, or the browser blocks them before they're ever sent.
   Fixed with an `@app.after_request` hook in `picar_server.py` setting
   all three headers.
2. **`solana_client.py`'s `confirm_transaction()` never detected success.**
   `status.confirmation_status` is a `solders` enum
   (`TransactionConfirmationStatus.Finalized`), not the plain string
   `"finalized"` — the old code's `status.confirmation_status in
   ("confirmed", "finalized")` check silently never matched anything.
   Net effect: the very first real `confirm_delivery` actually succeeded
   on-chain within seconds (verified independently via
   `getSignatureStatuses` and the seller balance moving), but
   `car_main.py` sat there burning the full retry budget (3 attempts ×
   60s timeout) before giving up with a `RuntimeError` — so *the money
   moved correctly the whole time*, only the client's own success
   detection (and therefore the `/delivered` callback to the order app)
   was broken. Fixed by comparing `str(status.confirmation_status).lower()`
   with substring checks instead of an exact match.

**Also found:** `create_delivery`/`confirm_delivery` share one escrow PDA
per operator (`seeds = [b"escrow", drone_operator_pubkey]`) — there can
only ever be one order in flight per unit at a time, and a second
`create_delivery` fails outright if the first hasn't been confirmed or
cancelled yet. `cancel_delivery` requires the deadline to have passed
(`DEADLINE_MINUTES`, default 60) — ran into this directly when an earlier
dry-run-only order's deadline expired while it sat untouched; had to
`cancel_delivery` (buyer gets the lamports back, escrow account closes)
before a fresh order could be placed.

**Result — first real, live, on-chain delivery, fully verified:**
- `create_delivery` (buyer → escrow): [`5CuW74T8...`](https://explorer.solana.com/tx/5CuW74T8WTi6gANwxUyhbjYTSnDhaWaJeDF2864s9SpsUSbjahmjbU5UB1LdEZbNB6PaLgi1xhYnHiQwakJGNft9?cluster=devnet)
- Buyer showed the static box QR (`ROBOPAY-BOX-C`) to the car's camera —
  ultrasonic distance check temporarily disabled via the new
  `REQUIRE_DISTANCE=false` env var on `car_main.py` (QR-only matching;
  distance check is still there and can be re-enabled, just not needed
  for this indoor test)
- `confirm_delivery` (escrow → seller), fired autonomously by the car's
  own operator wallet: [`5xYW4xys...`](https://explorer.solana.com/tx/5xYW4xysrr8EhXb2S9rbkUNKZ388cnXJaLRh3M4H64fSNrxHekh8m5yNfn2H94S6T3UuUFew2iYekynAhKoCQFj1?cluster=devnet),
  `confirmationStatus: finalized`, `err: null`
- Seller/PiCarOwner wallet balance moved 0 → 0.2 SOL, confirmed both via
  direct RPC call and on the order app's live wallet panel

This is the milestone the rest of the session had been building toward —
first confirmed case of Unit C's own machine wallet autonomously signing
a real payout, triggered purely by a physical QR-code proof of delivery.

## 11. Split into buyer_app.py + seller_app.py — 2026-09-16

`order_app_pc.py` (§10) mixed two separate roles on one unauthenticated
page — placing orders (buyer) and monitoring/driving the car (seller/car
owner). Split into `car/buyer_app.py` and `car/seller_app.py`, sharing
config/Solana/state-file helpers via `car/robopay_common.py`. Each has its
own HTTP Basic Auth login (`BUYER_USERNAME`/`BUYER_PASSWORD`,
`SELLER_USERNAME`/`SELLER_PASSWORD` env vars — native browser login
prompt, no custom form needed, works fine on mobile). `buyer_app.py` still
owns `order_state.json` and is the one `car_main.py`'s `PC_SERVER_URL`
points at (its machine-facing routes — `/active_order`, `/delivered`,
`/force_delivery`, `/rpi_log` — are exempted from auth, since `car_main.py`
has no concept of HTTP login). `seller_app.py` never touches any private
key; it only calls `picar_server.py`'s HTTP API and reads the shared state
file read-only for the Escrow-Release tx history. This is the intended
foundation for the future Car Owner Solana Mobile dApp.

**Bug found deploying seller_app.py behind the MCC tunnel:** the frontend
JS originally fetched `picar_server.py` directly at `PICAR_SERVER_URL`
(the MCC tunnel's DNS name, e.g. `http://backend-server`) from the
browser. That hostname only resolves/routes from *inside* the MCC
network — i.e. from the backend server itself — not from an arbitrary
phone or PC browser on the public internet, so drive buttons silently did
nothing and the camera feed didn't load. Fixed by adding `/proxy/*` routes
to `seller_app.py` (`/proxy/drive`, `/proxy/stop`, `/proxy/camera/angle`,
`/proxy/ultrasonic`, `/proxy/camera/qr`, and a streaming proxy
`/proxy/mjpg` for the MJPEG feed) — the Flask backend fetches
`picar_server.py` server-side (which *can* reach the MCC hostname) and
relays the response; the browser now only ever talks to `seller_app.py`'s
own public domain.

## 12. Backend server on Staex Hosting — 2026-09-16

Answers the earlier open question ("does Staex have hosting Kevin can use
instead of a VPS") — yes: **staexhosting.com**, a self-service Staex
product. Server "Staex" (2 vCPU, 2GB RAM, 20GB encrypted disk), address
`10.40.174.209` — **on a private network, not reachable from the internet
by IP**; the only initial access is a browser-based root shell
("Open terminal" on the server's dashboard page) or the MCP connection
(see below).

**MCC on the backend server:**
- Installed the same way as the RPi (§7): `staex-repo.noarch.deb` → `apt-get install mcc`
- Joined Kevin's own "RoboPay" network (same one the car is on, see the
  "Migrated to Kevin's own RoboPay network" section above) using the
  self-service **"Run the following command to setup a new node"** flow
  on the network's page at `cas.staex.io/networks/<network-id>` — simpler
  than the certificate+`--stdin`-private-key flow used earlier: just
  `mcc init <network-certificate>` (the certificate is the whole
  network's shared credential, safe to reuse across every node in it) —
  it then interactively asks for the network's private key (also shared,
  safe to reuse) and generates its own fresh, unique node identity
  locally. Parent line (`parents = public.staex.io:9376`) has to be added
  manually — this server's `mcc.conf` template only ships the commented
  example, not an empty active line like the RPi's did, so a naive `sed`
  replace silently matches nothing; just append the line instead.
- Node ID: `zrk9czd25ek1ns3h81saecdf0zesnv50b0f1v573f7x66yvjw5fg`, same
  parent as the car (`188.245.186.74:9376`)

**Three MCC tunnels, created on the car, granting this server access:**
each service `picar_server.py` exposes lives on a different port, and
each port needed its **own** tunnel with its **own** DNS name (set via
`--name` at creation) — a mistake worth flagging since it's easy to
assume one tunnel covers "the car":
```bash
mcc create-tunnel --name backend-server       --remote-node <server-node-id> --targets tcp:8080 --stdin   # picar_server.py API
mcc create-tunnel --name backend-server-video --remote-node <server-node-id> --targets tcp:9000 --stdin   # vilib's separate MJPEG stream process
mcc create-tunnel --name backend-server-ssh   --remote-node <server-node-id> --targets tcp:22   --stdin   # SSH access to the car from the server
```
(all run with the network private key piped via `--stdin`, same as the
network-migration `mcc init` earlier — sudo password and the piped secret
must not share one command, see §7's note on that). Reachable from the
backend server as `http://backend-server:8080`, `http://backend-server-video:9000`,
and `ssh ...@backend-server-ssh` respectively — **not** interchangeable
hostnames despite being the same physical car. `robopay_common.py` has
both `PICAR_SERVER_URL` and `PICAR_VIDEO_URL` for exactly this reason.

**Apps running as systemd services** (`robopay-buyer.service`,
`robopay-seller.service` — same autostart pattern as the RPi, §8):
`WorkingDirectory=/root/robopay-research-group/car`,
`ExecStart=/root/robopay-venv/bin/python3 buyer_app.py` (or
`seller_app.py`), env vars for the Basic Auth credentials and
`PICAR_SERVER_URL=http://backend-server` / `PICAR_VIDEO_URL=http://backend-server-video`
set inline via `Environment=`. `python3-venv`/pip and all required
packages (`solana==0.36.6`, `flask`, `qrcode`, `pillow`, `requests`) were
already present on this server image — didn't need installing.

**Publicly reachable via Staex Hosting's "Web address" feature**
(dashboard → Web address / `mcp__staex__publish_site` → pick a name + which
local port it maps to, HTTPS terminated at their edge, plain HTTP to the
app locally). **Important limitation, found 2026-09-17:** this feature
only supports ONE published address per server at a time — publishing a
second name/port silently *replaces* the first rather than adding to it
(confirmed via the tool's own description: "Calling this again with a
different name or port replaces the current address"). Publishing
`robopay-buyer` then `robopay-seller` separately, as originally set up,
meant only the seller app was ever really reachable from outside — the
buyer app returned 404 despite running fine locally, which is what broke
buyer-app access on the day before the Superteam Germany bootcamp.

**Fix: a small gateway app (`gateway_app.py`, port 8000) in front of both.**
It does **not** merge buyer_app.py and seller_app.py — those stay two
fully separate processes/files/ports/logins, exactly as before. The
gateway is a third, separate Flask process that only proxies:
`/buyer/*` → `http://localhost:5001/*`, `/seller/*` →
`http://localhost:5002/*`, forwarding method/headers/body untouched (so
each app's own Basic Auth check still runs on the real app). The only
non-trivial part: each app's index page HTML contains root-relative
`fetch('/order')`/`<img src="/qrcode.png">` calls that would otherwise
resolve against the gateway's own root once loaded through a `/buyer/`
prefix — the gateway rewrites just those two patterns
(`fetch('/...` → `fetch('/buyer/...`, `src="/...` → `src="/buyer/...`) in
the index HTML only, via regex, before relaying it. Every other
route (JSON responses, the MJPEG stream) passes through unchanged.
Runs as `robopay-gateway.service` (same systemd pattern as the other two).
One address now published: **https://robopay.staexhosting.com**
(→ port 8000) with `/` showing a two-button picker page, `/buyer/` and
`/seller/` for the two apps. The old separate
`robopay-buyer.staexhosting.com` / `robopay-seller.staexhosting.com`
addresses no longer resolve — this account can only ever have one live
address, so there was no way to keep them as separate hostnames.

Both apps now run 24/7 independent of Kevin's home network or laptop
being on — this is what makes the car usable away from home (e.g. at a
Superteam Germany event): as long as the car has power and *any*
connectivity (venue WiFi, or its own SIM/PPP as a fallback, §7), MCC finds
it and both apps keep working.

**SSH jump-host for remote debugging:** with the `backend-server-ssh`
tunnel in place, `ssh picarx@backend-server-ssh` from the Staex Hosting
server's shell reaches the car directly — added a short `~/.ssh/config`
alias there (`Host car` → `HostName backend-server-ssh`, `User picarx`) so
it's just `ssh car`. Practical effect: debugging the car from anywhere
only requires opening staexhosting.com in a browser and clicking
"Open terminal" — no dependency on being on the same network as the car
or having the RPi's SSH key on whatever device is at hand.

**MCP connection for direct agent access to the server:** Staex Hosting
exposes an MCP endpoint (`https://staexhosting.com/mcp`) with scoped API
keys (`server:read`, `vm:exec`, `vm:files`, `site:publish`). Connected via
`claude mcp add --transport http --scope user staex https://staexhosting.com/mcp --header "Authorization: Bearer <key>"`
— `--scope user` matters: this repo's working directory is a shared
Google Drive folder synced across the whole Staex team, and a
project-scoped `.mcp.json` would have put the Bearer token in that shared
folder. User scope keeps it in the local machine's own Claude config
instead. Requires the standalone `claude` CLI (the VS Code extension
alone doesn't expose one on PATH) — installed via
`irm https://claude.ai/install.ps1 | iex` on Windows. The connection
intermittently fails with a Cloudflare Tunnel error (1033) on Staex's
side — transient so far, falls back cleanly to the browser terminal when
it happens.

**Known rough edge:** the Staex Hosting browser terminal has a paste bug
— pasting a multi-line command block sometimes re-executes it 2-4× and/or
concatenates repeated pastes into one malformed line (e.g.
`backend-server-sshssh`). Workarounds that worked: prefer single-line
commands (`printf '...\n...' > file` instead of a `cat <<EOF` heredoc for
anything that must land correctly), keep any interactively-typed hostname
short (hence the `ssh car` alias), or just use the MCP `run_command` tool
instead once it's connected.

## 14. On-site at Superteam Germany bootcamp (2026-09-17) — findings

**The car's WiFi only knew the home network.** At the venue it had no
connectivity at all, so none of the MCC tunnels (§13) resolved from the
backend server. Fixed by adding the venue's WiFi as an additional
NetworkManager profile (`nmcli connection add type wifi con-name
'superteam-germany' ifname wlan0 ssid '<ssid>' wifi-sec.key-mgmt wpa-psk
wifi-sec.psk '<psk>' connection.autoconnect yes`) — NetworkManager holds
multiple saved profiles at once and auto-connects to whichever is in
range, so this didn't touch the existing home-WiFi profile.

**Venue WiFi and a personal phone hotspot both looked connected but
passed no real traffic** — DNS resolved fine, every TCP connection timed
out with zero response (`curl --max-time` → exit 28, no SYN-ACK, no
RST). Two different root causes produced the identical symptom:
- Venue WiFi: most likely device-fingerprinting/NAC quarantining an
  unrecognized-looking MAC (Raspberry Pi's OUI) into a walled-garden
  segment — normal client devices (phone, laptop) had no captive portal
  and worked immediately, so it's not a portal, it's silent
  per-device filtering. Fix requires the venue's network admin to
  whitelist the Pi's WiFi MAC.
- Phone hotspot: carrier-side tethering detection/throttling (common on
  German prepaid plans — TTL-based DPI distinguishes the phone's own
  traffic from a tethered client's and blocks/degrades the latter).
  Confirmed by testing: the phone's own mobile data (WiFi off) browsed
  fine, but anything connected *through* its hotspot got the same
  DNS-ok/TCP-blocked pattern.

**The actual fix: the car's own Staex M2M SIM (SIM7600G-H), independent
of any local WiFi.** This is the fallback originally intended for this
exact scenario (see §7) and turned out to be the right answer, not a
side project. Two things were required to get it working:
1. **The RPi needs its own separate, stable power source (a USB-C
   powerbank), not shared with the PiCar-X drive/HAT battery.** With
   power shared, the SIM7600 modem failed to enumerate at all
   (`dmesg`: "Cannot enable. Maybe the USB cable is bad?", repeated,
   then "unable to enumerate USB device") — a classic USB-port
   undervoltage symptom under combined motor+modem load, not an actual
   bad cable. `vcgencmd get_throttled` on the shared-power boot showed
   no undervoltage flag by the time it was checked (the sticky bits
   reset on the following reboot onto the powerbank, so this doesn't
   retroactively prove the mechanism, but the practical fix — separate
   power — reliably resolved it: modem enumerated cleanly every time
   afterward). **Going forward: always run the RPi off its own
   powerbank when field-testing away from wall power**, independent of
   whether the drive battery is on.
2. The PPP service (`ppp-staex-sim.service`, dials via
   `/etc/ppp/peers/staex-sim`) was already enabled from earlier setup
   (§7) and came up on its own once the modem enumerated — no config
   change needed, just stable power. Once `ppp0` was up, the MCC
   tunnels resolved and worked (`backend-server:8080` went from
   connection-refused to real HTTP responses) — **and this is over the
   SIM specifically, confirmed by testing while WiFi was still
   connectivity-dead**, i.e. it genuinely is the WiFi-independent path
   this was built for. Note the Staex M2M SIM appears to reach Staex's
   own infrastructure directly rather than acting as a general-purpose
   internet uplink — a plain `curl http://neverssl.com` over `ppp0`
   still timed out even once MCC itself worked over the same interface,
   so don't use "can it browse the open internet" as the test for
   whether the SIM path is working; test MCC/the backend tunnel
   directly instead.

**Ethernet-cable-as-emergency-console:** with no working network at
all, a direct Ethernet cable between a laptop and the Pi's `eth0` still
gives IPv6 link-local SSH access (`ssh -6 -i <key>
picarx@<fe80::...%interface-index>` — Windows needs the numeric adapter
index appended after `%`, found via `Get-NetAdapter`; the address itself
comes from `ping picarx.local` once, which resolves it via mDNS even
though repeat pings/pure address-based pings are unreliable). This link
was **very flaky when the RPi shared power with the drive battery**
(repeated multi-second dropouts, `nmcli` showed `netplan-eth0` stuck
`activating` — it has no DHCP server on a direct point-to-point cable,
so it cycles retrying) and **became reliable once the RPi moved to its
own powerbank** — another data point for "always give the RPi separate
power in the field."

**Known hardware issue: pan servo (camera) failed in the field.**
After the pan axis was driven repeatedly into its software limit
(±35°, enforced in `picar_server.py`'s `/camera/angle` handler) during
button-mashing while debugging the network issue above, the servo
degraded from "moves but overshoots/wrong-seeming direction" to "does
not move at all" — confirmed not a software issue: commands return
`{"ok": true, ...}` with sane requested values (checked in
`picar-server`'s own log), the I2C bus still enumerates the Robot HAT
normally (`i2cdetect -y 1` → device at `0x14`, unchanged), the
servo's signal cable connector was checked and reseated with no change,
and HAT power was confirmed on. All of that with zero physical movement
means the servo motor/gearbox itself is mechanically dead — not
fixable in the field, needs a replacement pan servo. **Workaround in
use:** camera repositioned by hand to point at the delivery box and
left alone; QR detection and ultrasonic distance don't depend on the
camera being able to pan, so delivery confirmation is unaffected. Do
not keep sending `/camera/angle` commands to the dead axis — the tilt
servo is a separate unit and still works, but pan does not respond at
all now (only the previous 20/0 "found by live testing" default, noted
in `picar_server.py`'s own comment, is no longer valid; there's no
current known-good pan value since the servo doesn't move to any of
them anymore).

## 15. First real end-to-end delivery test (2026-09-17) — two bugs found and fixed

With the car back on its own powerbank and the venue-WiFi/SIM path (§14)
working, ran the actual full flow for the first time since the
buyer/seller split (§11): order via the buyer app, hold the box QR up
to the camera, watch for `confirm_delivery` on-chain.

**Bug 1 — `car_main.py` still pointed at a dead home-network IP.**
`car-trigger.service`'s `PC_SERVER_URL` was `http://192.168.178.61:5000`
— the old home-LAN address of the original combined `order_app_pc.py`,
from before the buyer/seller split. It was never updated when
`buyer_app.py` replaced it, so `car_main.py` had been silently retrying
`/active_order` against an unreachable address ever since — the buyer
app itself worked fine, but the car could never see that an order
existed. Symptom: buyer app shows an order, camera correctly reads the
QR (confirmed via `/seller/proxy/camera/qr` directly), but nothing
happens — `car-trigger`'s own log was the only place this was visible
(`Could not reach PC server: ... 192.168.178.61 ... Connection timed
out`). Fixed by pointing it at the real public buyer app instead:
`Environment=PC_SERVER_URL=https://RoboPay:<BUYER_PASSWORD>@robopay.staexhosting.com/buyer`
— the Basic Auth userinfo in the URL isn't actually required (the
`/active_order`, `/delivered`, `/force_delivery`, `/rpi_log` paths
`car_main.py` calls are all in `buyer_app.py`'s `exempt_paths`, see
§11), but doesn't hurt as a safety net if that ever changes. Also
matters that this URL goes through the gateway (§13) with the `/buyer`
prefix — the gateway strips it before forwarding, so `buyer_app.py`
still sees the plain `/active_order` etc. paths it expects.

**Bug 2 — escrow account never gets closed after a successful
delivery, blocking the next order.** The `DeliveryEscrow` PDA is
one-per-operator (§ program design) and `confirm_delivery` only sets
`status = DELIVERED` — it doesn't close the account. The very next
`create_delivery` then fails with `Allocate: account Address {...}
already in use`, because the old (delivered but unclosed) account is
still sitting on that same PDA. Hit this twice in a row during this
test session before the pattern was obvious. Diagnosed by decoding the
escrow account's raw bytes by hand against the `DeliveryEscrow` struct
layout in `anchor/programs/drone-delivery/src/lib.rs` (`buyer`,
`seller`, `drone_operator`: `Pubkey` × 3, then `amount`: `u64`,
`target_lat`/`target_lon`/`deadline`: `i64` × 3, `status`: `u8`,
`bump`: `u8` — 138 bytes total including the 8-byte Anchor
discriminator) — `getAccountInfo` on the PDA plus this layout tells you
definitively whether a stuck account is `PENDING` (0), `DELIVERED` (1),
or `CANCELLED` (2) without needing any additional tooling. **Fixed
properly, not just patched around:** `car_main.py`'s `_confirm()` now
calls `solana.close_escrow()` (the method already existed in
`rpi/solana_client.py`, just was never called from the Unit C trigger
loop) right after a successful `confirm_delivery`, wrapped in its own
try/except so a close failure never masks the fact that the actual
payment already succeeded — it only affects being ready for the next
order, not this one's outcome.

**One-off manual recovery command** (for if a stuck escrow ever needs
clearing by hand again, e.g. from a test that crashed before the new
auto-close ran): build and send a bare `close_escrow` instruction
(8-byte Anchor discriminator `sha256("global:close_escrow")[:8]`, two
accounts: the escrow PDA as writable non-signer, the drone operator
keypair as writable signer) using
`WALLET_KEYPAIR_PATH` from `car-trigger.service`
(`/home/picarx/.config/solana/picarx_operator.json`) — run it directly
on the car (venv already has `solders`/`requests`), never copy that
keypair off the device.

**Practical note on remote access during this session:** both the MCP
connection and the `staexhosting.com` browser dashboard were
intermittently unusable for several minutes at a time (MCP: "Unexpected
content type: null" on every call; dashboard: loads but renders a blank
white page, reproduced in a fresh incognito window too, so not a
cache/extension issue — likely the same underlying platform hiccup
surfacing two ways). The direct Ethernet-cable link-local SSH path
(§14) kept working as a fallback throughout, including for writing
`car_main.py`'s fix directly onto the RPi when MCP was down for a
period. Confirms the fallback is worth keeping documented and ready,
not just a one-time debugging trick.
