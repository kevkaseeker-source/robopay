from gpiozero import AngularServo
from time import sleep

servo = AngularServo(18, min_angle=0, max_angle=180)

print("servo test started")
print("watch the servo horn - it should move 30 -> 90 -> 150 degrees")
print("press Ctrl+C to stop")
print("")

while True:
    print("angle 30")
    servo.angle = 30
    sleep(1.5)

    print("angle 90")
    servo.angle = 90
    sleep(1.5)

    print("angle 150")
    servo.angle = 150
    sleep(1.5)
