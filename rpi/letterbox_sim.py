#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Smart Letterbox Node - Simulation (no hardware)
================================================
Run this before the real hardware (servo + micro switch) arrives,
to verify the full logic flow:

    receive "arrived" -> open door -> confirm door open -> report

Run on the Raspberry Pi:
    python3 letterbox_sim.py

Press Enter each time to simulate one delivery arrival.
Later, only open_door() and check_door_open() need to be replaced
with GPIO control; the rest of the flow stays unchanged.
"""

import time

# ---------- The two functions below will be replaced by the real hardware version ----------

def open_door():
    """Drive the servo to push the door open (simulated: log + rotation delay)."""
    print("  [servo] Command received, rotating 90 degrees ...")
    time.sleep(1.5)                      # simulated rotation time of the real servo
    print("  [servo] Rotation complete, door is open.")
    return True

def check_door_open():
    """Read the micro switch to confirm the door is really open (simulated: always open)."""
    print("  [micro switch] Checking door state ...")
    time.sleep(0.3)
    print("  [micro switch] Door is open.")
    return True

# ---------- The flow below is the core logic and stays unchanged later ----------

def wait_for_arrival():
    """Wait for the 'arrived' message. Simulation: Enter key stands in for the network message."""
    input("  [waiting] Press Enter to simulate an 'arrived' message from the delivery vehicle ...")
    print("  [comms] Received 'arrived' (parcel has arrived) message.")

def report_opened(ok):
    """Report the opening result; the payment step is gated on it."""
    if ok:
        print("  [comms] Sending 'opened' confirmation to the delivery vehicle.")
        print("  [payment] Triggering payment settlement (reserved: Solana escrow release).")
    else:
        print("  [comms] Sending 'failed' report (door did not open), waiting to retry.")

def main():
    print("=" * 46)
    print("  Smart Letterbox Node (simulation) started")
    print("=" * 46)
    while True:
        print("-" * 46)
        wait_for_arrival()          # 1. receive arrival message
        ok = open_door()            # 2. servo opens the door
        if ok:
            ok = check_door_open()  # 3. micro switch confirms the door is open
        report_opened(ok)           # 4. report the result
        print()

if __name__ == "__main__":
    main()
