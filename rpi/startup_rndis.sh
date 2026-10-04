#!/bin/bash
# startup_rndis.sh — Modem einschalten + auf usb0 warten (kein pppd!)
# SSH bleibt stabil weil kein pppd das Routing stört

cd /home/kevkaakvek
source drone-env/bin/activate

echo "=== Modem einschalten ==="
python3 modem_on.py
sleep 15

echo "=== Warten auf 4G (usb0) ==="
for i in $(seq 1 60); do
    if ip link show usb0 > /dev/null 2>&1; then
        sleep 2
        if ping -c 1 8.8.8.8 > /dev/null 2>&1; then
            echo "4G OK! (usb0 aktiv)"
            ip addr show usb0 | grep "inet "
            break
        fi
    fi
    printf "Warte auf usb0... (%d/60)\r" $i
    sleep 2
done

if ! ip link show usb0 > /dev/null 2>&1; then
    echo "FEHLER: usb0 nicht gefunden. RNDIS-Modus aktiv?"
    echo "Einmalig ausführen: echo -e 'AT+CUSBPIDSWITCH=9011,1,1\r' | sudo tee /dev/ttyUSB2"
    exit 1
fi

echo ""
echo "Jetzt Ethernet-Kabel abziehen, dann:"
echo "  bash /home/kevkaakvek/start_tunnel_rndis.sh"
