#!/bin/bash
# Einmalig ausführen auf RPi4 via SSH
# Richtet automatischen Start von Modem, 4G, Flask und ngrok ein

echo "=== Autostart Setup ==="

# 1. Autostart-Script für Modem + 4G
cat > /home/kevkaakvek/autostart_4g.sh << 'EOF'
#!/bin/bash
sleep 30
source /home/kevkaakvek/drone-env/bin/activate
python3 /home/kevkaakvek/modem_on.py
sleep 12
/usr/sbin/pppd call 4g
EOF
chmod +x /home/kevkaakvek/autostart_4g.sh

# 2. Systemd-Service für Modem + 4G
sudo tee /etc/systemd/system/drone-4g.service > /dev/null << 'EOF'
[Unit]
Description=Drone 4G Autostart
After=multi-user.target

[Service]
Type=forking
ExecStart=/bin/bash /home/kevkaakvek/autostart_4g.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

# 3. ip-up Hook — startet Flask + ngrok wenn 4G verbunden ist
sudo tee /etc/ppp/ip-up.d/start-drone > /dev/null << 'EOF'
#!/bin/bash
# Läuft automatisch wenn pppd Verbindung aufbaut

# Alte Prozesse beenden
pkill -f order_server.py 2>/dev/null
pkill -f ngrok 2>/dev/null
sleep 2

# Flask starten
nohup /home/kevkaakvek/drone-env/bin/python3 /home/kevkaakvek/order_server.py \
    > /home/kevkaakvek/flask.log 2>&1 &
sleep 3

# ngrok starten
nohup /usr/bin/ngrok http 5000 --log=stdout \
    > /home/kevkaakvek/ngrok.log 2>&1 &
sleep 6

# URL in Datei speichern
curl -s http://localhost:4040/api/tunnels | \
/home/kevkaakvek/drone-env/bin/python3 -c "
import sys, json
try:
    url = json.load(sys.stdin)['tunnels'][0]['public_url']
    with open('/home/kevkaakvek/ngrok_url.txt', 'w') as f:
        f.write(url + '\n')
    print('ngrok URL:', url)
except Exception as e:
    print('URL error:', e)
" >> /home/kevkaakvek/ngrok_start.log 2>&1
EOF
sudo chmod +x /etc/ppp/ip-up.d/start-drone

# 4. Systemd-Service aktivieren
sudo systemctl daemon-reload
sudo systemctl enable drone-4g.service

echo ""
echo "=== Setup abgeschlossen ==="
echo "Ab jetzt nach dem Boot:"
echo "  1. ~2 Minuten warten"
echo "  2. SSH einloggen"
echo "  3. cat /home/kevkaakvek/ngrok_url.txt"
echo "  4. URL ans Handy schicken → SSH trennen → rausgehen"
