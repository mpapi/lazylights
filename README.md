# Lazylights 

Lazylights is a Python API for controlling Lifx bulbs.


# Requirements

* Python 2
* One or more Lifx bulbs, updated to the 2.0 firmware


# Quick start

To install,

```shell
pip install git+https://github.com/mpapi/lazylights
```

Then, in Python,

```python
import lazylights
import time

bulbs = lazylights.find_bulbs(expected_bulbs=2)

lazylights.set_power(bulbs, True)
time.sleep(1)
lazylights.set_power(bulbs, False)
```


# Documentation

Lazylights provides no-dependencies Python module with a minimal API for
interacting with Lifx bulbs. Before the 2.0 firmware update, discovering and
controlling bulbs was more complex than it is now, and this module had a lot
more in it. Now, there are four core functions: 

* `find_bulbs(expected_bulbs=None, timeout=1)`

  This discovers bulbs on your local network. It returns a set of `Bulb`
  objects once it's found `expected_bulbs` or `timeout` seconds have elapsed
  (whichever comes first).


* `get_state(bulbs, timeout=1)`

  Takes a sequence of `Bulb` objects and returns a list of `State` objects,
  which you can inspect to find the current parameters for each bulb.

  It returns as soon as it has received state from each of `bulbs` or `timeout`
  seconds have elapsed (whichever comes first).

* `set_state(bulbs, hue, saturation, brightness, kelvin, fade, raw=False)`

  Takes a sequence of `Bulb` objects and sets their state:

  * `hue` is an integer from 0 to 360, where 0/360 is red
  * `saturation` is a float from 0 to 1, where 1 is fully saturated; if 0,
    and `kelvin` is set, uses the whiteness scale instead of colors
  * `brightness` is a float from 0 to 1, where 1 is brightest
  * `kelvin` is an integer from 2000 (warmest) to 8000 (coolest)
  * `fade` is a transition time in milliseconds, where 0 is instant
  * `raw`, if True, uses raw values for hue, saturation, and brightness --
    integers from 0 to 65535

* `set_power(bulbs, is_on)`

  Takes a sequence of `Bulb` objects and turns them on or off.

Lazylights does not currently support any kind of remote access through the
Lifx Cloud, though I have been using an SSH tunnel for this purpose for many
months, with great success.

There currently are no public higher-level functions, or a command-line
interface, though it's likely that those will be added over time (especially
the latter).


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
