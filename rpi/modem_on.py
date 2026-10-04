import RPi.GPIO as GPIO
import time

POWER_KEY = 6

GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)
GPIO.setup(POWER_KEY, GPIO.OUT)

print("Modem einschalten...")
GPIO.output(POWER_KEY, GPIO.HIGH)
time.sleep(0.1)
GPIO.output(POWER_KEY, GPIO.LOW)
time.sleep(2)
GPIO.output(POWER_KEY, GPIO.HIGH)
time.sleep(8)
print("Fertig")
GPIO.cleanup()
