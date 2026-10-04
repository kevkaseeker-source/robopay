#!/bin/bash
# start_tunnel_rndis.sh — Flask + ngrok via usb0 (RNDIS-Modus, kein pppd)
# Erst ausführen NACHDEM Ethernet abgezogen wurde!

# ── Dein ntfy-Topic — einmal setzen, nie wieder anfassen ──────────────────
NTFY_TOPIC="drone-kevin-2026"
# ──────────────────────────────────────────────────────────────────────────

cd /home/kevkaakvek
source drone-env/bin/activate

# Prüfen ob 4G aktiv ist (usb0 statt ppp0)
if ! ip link show usb0 > /dev/null 2>&1; then
    echo "FEHLER: usb0 nicht aktiv! Erst startup_rndis.sh ausführen."
    exit 1
fi
echo "OK: usb0 aktiv (4G läuft)"

# DNS sicherstellen
echo "nameserver 8.8.8.8" | sudo tee /etc/resolv.conf > /dev/null

# Alte Prozesse beenden
pkill -f order_server.py 2>/dev/null
pkill -f "ngrok http" 2>/dev/null
sleep 2

echo "=== Flask starten ==="
nohup python3 order_server.py > flask.log 2>&1 &
sleep 3

echo "=== ngrok starten ==="
nohup ngrok http 5000 --log=stdout > ngrok.log 2>&1 &
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

# Watchdog
(
    while true; do
        sleep 30
        if ! pgrep -f "ngrok http" > /dev/null; then
            echo "[watchdog $(date '+%H:%M:%S')] ngrok tot, starte neu..." >> /home/kevkaakvek/watchdog.log
            nohup ngrok http 5000 --log=stdout > /home/kevkaakvek/ngrok.log 2>&1 &
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
echo "URL ans Handy — SSH trennen und rausgehen!"
