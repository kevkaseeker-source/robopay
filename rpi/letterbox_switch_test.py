from gpiozero import Button
from signal import pause

sw = Button(17, pull_up=True, bounce_time=0.05)

print("switch test started")
print("is_pressed right now =", sw.is_pressed)
print("now press and hold the switch lever, then let go")
print("press Ctrl+C to stop")

sw.when_pressed = lambda: print("state changed -> is_pressed = True")
sw.when_released = lambda: print("state changed -> is_pressed = False")

pause()
