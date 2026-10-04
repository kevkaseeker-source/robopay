from gpiozero import AngularServo, Button
from time import sleep, monotonic

# hardware
servo = AngularServo(18, min_angle=0, max_angle=180)
door_switch = Button(17, pull_up=True, bounce_time=0.05)

# bench-test geometry (no real door yet)
CLOSED_ANGLE = 20
OPEN_ANGLE = 110
WAIT_TIMEOUT = 8.0


def door_is_open():
    # NC contact: the closed door holds the lever down -> pin pulled low -> is_pressed True
    # door pushed open, lever released -> pin high -> is_pressed False
    return not door_switch.is_pressed


def open_door():
    print("  [servo]  moving to the open position")
    servo.angle = OPEN_ANGLE
    sleep(1.2)


def wait_for_confirmation():
    print("  [switch] waiting for the physical confirmation ...")
    t0 = monotonic()
    while monotonic() - t0 < WAIT_TIMEOUT:
        if door_is_open():
            return True
        sleep(0.05)
    return False


def close_door():
    print("  [servo]  moving back to the closed position")
    servo.angle = CLOSED_ANGLE
    sleep(1.0)


print("letterbox node - bench test")
print("hold the switch lever down with one finger = door is closed")
print("")

while True:
    input("press Enter to simulate the 'arrived' message ...")
    print("[comms]  arrived received")
    open_door()
    ok = wait_for_confirmation()
    if ok:
        print("[result] opened -> this is where the payment would be released")
    else:
        print("[result] no confirmation within the timeout -> no payment")
    sleep(0.5)
    close_door()
    print("")
