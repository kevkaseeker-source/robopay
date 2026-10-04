#!/bin/bash
# Erst ausführen NACHDEM Ethernet abgezogen wurde!
# Flask + ngrok starten nur über ppp0 (4G) → stabil draußen

# ── Dein ntfy-Topic — einmal setzen, nie wieder anfassen ──────────────────
NTFY_TOPIC="drone-kevin-2026"
# ──────────────────────────────────────────────────────────────────────────

cd /home/kevkaakvek
source drone-env/bin/activate

# Prüfen ob 4G aktiv ist
if ! ip link show ppp0 > /dev/null 2>&1; then
    echo "FEHLER: ppp0 nicht aktiv! Erst startup.sh ausführen und 4G abwarten."
    exit 1
fi
echo "OK: ppp0 aktiv (4G laeuft)"

# Ethernet-Routen entfernen, ppp0 als Default setzen
echo "=== Routing bereinigen ==="
# Alle Ethernet-Default-Routen löschen (eth0/eth1/enp*)
for iface in eth0 eth1 enp0s3 enp1s0 enp2s0 wlan0 wlan1; do
    sudo ip route del default dev $iface 2>/dev/null && echo "  Alte Route via $iface entfernt"
done
# ppp0 als einziger Default
if ! ip route show default dev ppp0 | grep -q ppp0; then
    sudo ip route add default dev ppp0 2>/dev/null && echo "  ppp0 als Default gesetzt" || true
fi
# DNS sicherstellen
echo "nameserver 8.8.8.8" | sudo tee /etc/resolv.conf > /dev/null
echo "OK: Routing → ppp0 only, DNS → 8.8.8.8"

# Alte Prozesse beenden
pkill -f order_server.py 2>/dev/null
pkill -f "ngrok http" 2>/dev/null
sleep 2

echo "=== Flask starten ==="
nohup python3 order_server.py > flask.log 2>&1 &
sleep 3

echo "=== ngrok starten ==="
nohup ngrok http --domain=starless-morality-cranium.ngrok-free.dev 5000 --log=stdout > ngrok.log 2>&1 &
sleep 6

_get_url() {
    for i in 1 2 3 4 5; do
        URL=$(curl -s http://localhost:4040/api/tunnels | python3 -c "
import sys, json
try:
    url = json.load(sys.stdin)['tunnels'][0]['public_url']
    print(url)
except:
    pass
" 2>/dev/null)
        if [ -n "$URL" ]; then
            echo "$URL"
            echo "$URL" > /home/kevkaakvek/ngrok_url.txt
            return 0
        fi
        sleep 4
    done
    echo "FEHLER: ngrok URL nicht gefunden"
}

_push_url() {
    local url="$1"
    local msg="$2"
    curl -s \
        -H "Title: Drone App" \
        -H "Priority: high" \
        -d "$msg $url" \
        "https://ntfy.sh/$NTFY_TOPIC" > /dev/null
}

echo ""
echo "=== OEFFENTLICHE URL ==="
URL=$(_get_url)
echo "$URL"
_push_url "$URL" "App-URL:"
echo "-> Push-Notification gesendet an ntfy.sh/$NTFY_TOPIC"
echo ""

# Watchdog: prueft alle 30s ob ngrok/Flask noch laufen, startet neu falls nicht
(
    while true; do
        sleep 30
        if ! pgrep -f "ngrok http" > /dev/null; then
            echo "[watchdog $(date '+%H:%M:%S')] ngrok tot, starte neu..." >> /home/kevkaakvek/watchdog.log
            nohup ngrok http --domain=starless-morality-cranium.ngrok-free.dev 5000 --log=stdout > /home/kevkaakvek/ngrok.log 2>&1 &
            sleep 8
            NEW_URL=$(_get_url)
            echo "[watchdog $(date '+%H:%M:%S')] Neue URL: $NEW_URL" >> /home/kevkaakvek/watchdog.log
            _push_url "$NEW_URL" "NEUE URL (ngrok neugestartet):"
        fi
        if ! pgrep -f order_server.py > /dev/null; then
            echo "[watchdog $(date '+%H:%M:%S')] Flask tot, starte neu..." >> /home/kevkaakvek/watchdog.log
            nohup python3 order_server.py > /home/kevkaakvek/flask.log 2>&1 &
        fi
    done
) &
WATCHDOG_PID=$!
echo "Watchdog laeuft (PID $WATCHDOG_PID)"
echo "URL ans Handy schicken — dann SSH trennen und rausgehen!"
