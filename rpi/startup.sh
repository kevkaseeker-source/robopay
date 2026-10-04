#!/bin/bash
# Schritt 1: Modem + 4G starten (Ethernet kann noch drin sein)
cd /home/kevkaakvek
source drone-env/bin/activate

echo "=== Modem einschalten ==="
python3 modem_on.py
sleep 12

echo "=== 4G verbinden ==="
sudo pppd call 4g &
sleep 20

echo "=== 4G-Verbindung prüfen ==="
ping -c 3 -I ppp0 8.8.8.8 && echo "4G OK!" || echo "4G Fehler!"

echo ""
echo "Jetzt Ethernet-Kabel abziehen, dann:"
echo "  bash /home/kevkaakvek/start_tunnel.sh"
