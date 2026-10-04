#!/bin/bash
# Install crash-surviving logging on the car. Run on the RPi from the repo:
#   sudo bash car/logging/install.sh
# Idempotent - safe to re-run after pulling changes.
set -euo pipefail
cd "$(dirname "$0")"

mkdir -p /etc/systemd/journald.conf.d
install -m 0644 90-robopay-persistent.conf /etc/systemd/journald.conf.d/90-robopay-persistent.conf
mkdir -p /var/log/journal
systemd-tmpfiles --create --prefix /var/log/journal
systemctl restart systemd-journald
journalctl --flush

install -m 0755 power_monitor.sh /usr/local/bin/robopay-power-monitor
install -m 0644 power-monitor.service /etc/systemd/system/power-monitor.service
systemctl daemon-reload
systemctl enable --now power-monitor.service
systemctl restart power-monitor.service

echo "--- journald:"
journalctl --header -n0 2>/dev/null | grep -m1 "File path" || true
journalctl --disk-usage
echo "--- power-monitor:"
systemctl --no-pager --lines=3 status power-monitor.service || true
echo
echo "After the next crash/reboot, read the previous run with:"
echo "  journalctl -b -1 -u power-monitor -u picar-server -u car-trigger"
echo "  journalctl -b -1 -k | grep -i -E 'volt|throttl'"
