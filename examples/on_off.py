from lazylights import Lifx
import time

lifx = Lifx(num_bulbs=2)  # so it knows how many to wait for when connecting

@lifx.on_connected
def _connected():
    print "Connected!"

with lifx.run():
    lifx.set_power_state(True)
    time.sleep(1)
    lifx.set_power_state(False)
