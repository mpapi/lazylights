# Lazylights 

Lazylights is a Python API for controlling Lifx bulbs.

# Quick start

To install,

```shell
pip install git+https://github.com/mpapi/lazylights
```

Then, in Python,

```python
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
```


# Features

* connection management
* high- and low-level interfaces for sending and receiving data
* callback-based, non-blocking, and blocking APIs
* no dependencies other than Python


# Documentation

Not much here yet, sadly, but the code is fairly well-documented. See the
docstrings, or check out the examples directory.


# Hacking

```shell
pip install -r dev_requirements.txt
flake8 *.py && nosetests
```

# Credits

The [`lifxjs` Protocol wiki page][lifxjs_protocol] was particularly helpful in
the creation of this package.


# License

Licensed under the MIT license. See the LICENSE file for the full text.


[lifxjs_protocol]: https://github.com/magicmonkey/lifxjs/blob/master/Protocol.md
