from contextlib import closing, contextmanager
import socket
import struct
from threading import Thread, Event, Lock
from collections import namedtuple
import Queue


BASE_FORMAT = '<HHxxxx6sxx6sxxQHxx'
_FORMAT_SIZE = struct.calcsize(BASE_FORMAT)

ALL_BULBS = '\x00' * 6

LIFX_PORT = 56700
BROADCAST_ADDRESS = ('255.255.255.255', LIFX_PORT)

DISCOVERY_PROTOCOL = 0x5400
COMMAND_PROTOCOL = 0x3400

SERVICE_UDP = 0x01
SERVICE_TCP = 0x02

RESP_GATEWAY = 0x03
RESP_POWER_STATE = 0x16
RESP_LIGHT_STATE = 0x6b

REQ_GATEWAY = 0x02
REQ_SET_POWER_STATE = 0x15
REQ_GET_LIGHT_STATE = 0x65
REQ_SET_LIGHT_STATE = 0x66

_PAYLOADS = {
    RESP_GATEWAY: ('<BI', 'service', 'port'),
    RESP_POWER_STATE: ('<H', 'is_on'),
    RESP_LIGHT_STATE: ('<6H32s8s', 'hue', 'sat', 'bright', 'kelvin', 'dim',
                       'power', 'label', 'tags')
}

_SHUTDOWN = object()

EVENT_DISCOVERED = 'discovered'
EVENT_CONNECTED = 'connected'
EVENT_BULBS_FOUND = 'bulbs_found'
EVENT_UNKNOWN = 'unknown'
EVENT_LIGHT_STATE = 'light_state'
EVENT_POWER_STATE = 'power_state'

Header = namedtuple('Header', 'size protocol mac gateway time packet_type')
Bulb = namedtuple('Bulb', 'label mac')
Gateway = namedtuple('Gateway', 'addr port mac')


def parse_packet(data, format=None):
    """
    Parses a Lifx data packet (as a bytestring), returning into a Header object
    for the fields that are common to all data packets, and a bytestring of
    payload data for the type-specific fields (suitable for passing to
    `parse_payload`).
    """
    unpacked = struct.unpack(BASE_FORMAT, data[:_FORMAT_SIZE])
    psize, protocol, mac, gateway, time, ptype = unpacked
    header = Header(psize, protocol, mac, gateway, time, ptype)
    return header, data[_FORMAT_SIZE:]


def parse_payload(data, payload_fmt, *payload_names):
    """
    Parses a bytestring of Lifx payload data (the bytes after the common
    fields), as returned by `parse_packet`. Returns a dictionary where the keys
    are from `payload_names` and the values are the corresponding values from
    the bytestring.
    """
    payload = struct.unpack(payload_fmt, data)
    return dict(zip(payload_names, payload))


def build_packet(packet_type, gateway, bulb, payload_fmt, *payload_args,
                 **kwargs):
    """
    Constructs a Lifx packet, returning a bytestring. The arguments are as
    follows:

    - `packet_type`, an integer
    - `gateway`, a 6-byte string containing the mac address of the gateway bulb
    - `bulb`, a 6-byte string containing either the mac address of the target
      bulb or `ALL_BULBS`
    - `payload_fmt`, a `struct`-compatible string that describes the format
      of the payload part of the packet
    - `payload_args`, the values to use to build the payload part of the packet

    Additionally, the `protocol` keyword argument can be used to override the
    protocol field in the packet.
    """
    protocol = kwargs.get('protocol', COMMAND_PROTOCOL)

    packet_fmt = BASE_FORMAT + payload_fmt
    packet_size = struct.calcsize(packet_fmt)
    return struct.pack(packet_fmt,
                       packet_size,
                       protocol,
                       bulb,
                       gateway,
                       0,  # timestamp
                       packet_type,
                       *payload_args)


def _bytes(packet):
    """
    Returns a human-friendly representation of the bytes in a bytestring.

    >>> _bytes('\x12\x34\x56')
    '123456'
    """
    return ''.join('%02x' % ord(c) for c in packet)


def _unbytes(bytestr):
    """
    Returns a bytestring from the human-friendly string returned by `_bytes`.

    >>> _unbytes('123456')
    '\x12\x34\x56'
    """
    return ''.join(chr(int(bytestr[k:k + 2], 16))
                   for k in range(0, len(bytestr), 2))


def _spawn(func, *args, **kwargs):
    """
    Calls `func(*args, **kwargs)` in a daemon thread, and returns the (started)
    Thread object.
    """
    thr = Thread(target=func, args=args, kwargs=kwargs)
    thr.daemon = True
    thr.start()
    return thr


def _retry(event, attempts, delay):
    """
    An iterator of pairs of (attempt number, event set), checking whether
    `event` is set up to `attempts` number of times, and delaying `delay`
    seconds in between.

    Terminates as soon as `event` is set, or until `attempts` have been made.

    Intended to be used in a loop, as in:

        for num, ok in _retry(event_to_wait_for, 10, 1.0):
            do_async_thing_that_sets_event()
            _log('tried %d time(s) to set event', num)
        if not ok:
            raise Exception('failed to set event')
    """
    event.clear()
    attempted = 0
    while attempted < attempts and not event.is_set():
        yield attempted, event.is_set()
        if event.wait(delay):
            break
    yield attempted, event.is_set()


@contextmanager
def _blocking(lock, state_dict, event, timeout=None):
    """
    A contextmanager that clears `state_dict` and `event`, yields, and waits
    for the event to be set. Clearing an yielding are done within `lock`.

    Used for blocking request/response semantics on the request side, as in:

        with _blocking(lock, state, event):
            send_request()

    The response side would then do something like:

        with lock:
            state['data'] = '...'
            event.set()
    """
    with lock:
        state_dict.clear()
        event.clear()
        yield
    event.wait(timeout)


class Callbacks(object):
    """
    An object to manage callbacks. It exposes a queue to schedule callbacks,
    and a `run` function to be run in a separate thread to consume the queue
    and run the callback functions.
    """
    def __init__(self, logger):
        self._logger = logger
        self._callbacks = {}
        self._queue = Queue.Queue()

    def register(self, event, fn):
        """
        Tell the object to run `fn` whenever a message of type `event` is
        received.
        """
        self._callbacks.setdefault(event, []).append(fn)
        return fn

    def put(self, event, *args, **kwargs):
        """
        Schedule a callback for `event`, passing `args` and `kwargs` to each
        registered callback handler.
        """
        self._queue.put((event, args, kwargs))

    def stop(self):
        """
        Stop processing callbacks (once the queue is empty).
        """
        self._queue.put(_SHUTDOWN)

    def run(self):
        """
        Process all callbacks, until `stop()` is called. Intended to run in
        its own thread.
        """
        while True:
            msg = self._queue.get()
            if msg is _SHUTDOWN:
                break
            event, args, kwargs = msg
            self._logger('<< %s', event)
            for func in self._callbacks.get(event, []):
                func(*args, **kwargs)


class PacketReceiver(object):
    """
    An object to process incoming packets. It parses the data in the packets
    and schedules callbacks (on a `Callback` object) according to the packet's
    type. The `is_shutdown` event can be used to wait for the receiver to shut
    down after calling `stop`.
    """
    def __init__(self, addr, callbacks, buffer_size=65536, timeout=0.5):
        self._addr = addr
        self._shutdown = Event()
        self._callbacks = callbacks
        self._buffer_size = buffer_size
        self._timeout = timeout

    @property
    def is_shutdown(self):
        """
        An `Event` that is set when the receiver starts shutting down.
        """
        return self._shutdown

    def stop(self):
        """
        Stop processing incoming packets.
        """
        self._shutdown.set()

    def run(self):
        """
        Process all incoming packets, until `stop()` is called. Intended to run
        in its own thread.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(self._addr)
        sock.settimeout(self._timeout)
        with closing(sock):
            while not self._shutdown.is_set():
                try:
                    data, addr = sock.recvfrom(self._buffer_size)
                except socket.timeout:
                    continue

                header, rest = parse_packet(data)
                if header.packet_type in _PAYLOADS:
                    payload = parse_payload(rest,
                                            *_PAYLOADS[header.packet_type])
                    self._callbacks.put(header.packet_type,
                                        header, payload, None, addr)
                else:
                    self._callbacks.put(EVENT_UNKNOWN,
                                        header, None, rest, addr)


class PacketSender(object):
    """
    An object to manage outgoing packets. It exposes a queue to send packets,
    and a `run` function to be run in a separate thread to consume the queue
    while maintaining a connection to a gateway.
    """
    def __init__(self):
        self._queue = Queue.Queue()
        self._connected = Event()
        self._gateway = None

    @property
    def is_connected(self):
        """
        An `Event` that is set once the sender has connected to a gateway.
        """
        return self._connected

    def put(self, packet):
        """
        Schedules a packet to be sent to the gateway.
        """
        self._queue.put(packet)

    def stop(self):
        """
        Stop processing outgoing packets (once the queue is empty).
        """
        self._queue.put(_SHUTDOWN)

    def run(self):
        """
        Process all outgoing packets, until `stop()` is called. Intended to run
        in its own thread.
        """
        while True:
            to_send = self._queue.get()
            if to_send is _SHUTDOWN:
                break

            # If we get a gateway object, connect to it. Otherwise, assume
            # it's a bytestring and send it out on the socket.
            if isinstance(to_send, Gateway):
                self._gateway = to_send
                self._connected.set()
            else:
                if not self._gateway:
                    raise SendException('no gateway')
                dest = (self._gateway.addr, self._gateway.port)
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.sendto(to_send, dest)


class Logger(object):
    """
    An object to manage sequential logging.
    """
    def __init__(self, enabled=True):
        self._enabled = enabled
        self._queue = Queue.Queue()

    def __call__(self, msg, *args):
        """
        Queue a log message, formatting `msg` with `args`.
        """
        self._queue.put(msg % args)

    def stop(self):
        """
        Stop processing log messages (once the queue is empty).
        """
        self._queue.put(_SHUTDOWN)

    def run(self):
        """
        Process all log messages, until `stop()` is called. Intended to run
        in its own thread.
        """
        while True:
            msg = self._queue.get()
            if msg is _SHUTDOWN:
                break
            if self._enabled:
                print msg


class ConnectException(Exception):
    """
    An Exception raised when a gateway can't be found or connected to.
    """
    pass


class SendException(Exception):
    """
    An Exception raised when attempting to send data to a connected gateway.
    """
    pass


class Lifx(object):
    """
    Manages connecting to, sending requests to, and receiving responses from
    Lifx bulbs.
    """

    def __init__(self, num_bulbs=None):
        # Number of bulbs to wait for when connecting.
        self.num_bulbs = 1 if num_bulbs is None else num_bulbs

        # Connection/bulb state.
        self.gateway = None
        self.bulbs = {}
        self.power_state = {}
        self.light_state = {}

        # Connection/state events.
        self.gateway_found_event = Event()
        self.bulbs_found_event = Event()
        self.power_state_event = Event()
        self.light_state_event = Event()
        self.lock = Lock()

        # Logging.
        self.logger = Logger(False)

        # Callbacks.
        self.callbacks = Callbacks(self.logger)
        self.callbacks.register(RESP_GATEWAY, self._on_gateway)
        self.callbacks.register(RESP_POWER_STATE, self._on_power_state)
        self.callbacks.register(RESP_LIGHT_STATE, self._on_light_state)

        # Sending and receiving.
        self.receiver = PacketReceiver(('0.0.0.0', LIFX_PORT), self.callbacks)
        self.sender = PacketSender()

    ### Built-in callbacks

    def _on_gateway(self, header, payload, rest, addr):
        """
        Records a discovered gateway, for connecting to later.
        """
        if payload.get('service') == SERVICE_UDP:
            self.gateway = Gateway(addr[0], payload['port'], header.gateway)
            self.gateway_found_event.set()

    def _on_power_state(self, header, payload, rest, addr):
        """
        Records the power (on/off) state of bulbs, and forwards to a high-level
        callback with human-friendlier arguments.
        """
        with self.lock:
            self.power_state[header.mac] = payload
            if len(self.power_state) >= self.num_bulbs:
                self.power_state_event.set()

        self.callbacks.put(EVENT_POWER_STATE, self.get_bulb(header.mac),
                           is_on=bool(payload['is_on']))

    def _on_light_state(self, header, payload, rest, addr):
        """
        Records the light state of bulbs, and forwards to a high-level callback
        with human-friendlier arguments.
        """
        with self.lock:
            label = payload['label'].strip('\x00')
            self.bulbs[header.mac] = bulb = Bulb(label, header.mac)
            if len(self.bulbs) >= self.num_bulbs:
                self.bulbs_found_event.set()

            self.light_state[header.mac] = payload
            if len(self.light_state) >= self.num_bulbs:
                self.light_state_event.set()

        self.callbacks.put(EVENT_LIGHT_STATE, bulb,
                           raw=payload,
                           hue=(payload['hue'] / float(0xffff) * 360) % 360.0,
                           saturation=payload['sat'] / float(0xffff),
                           brightness=payload['bright'] / float(0xffff),
                           kelvin=payload['kelvin'],
                           is_on=bool(payload['power']))

    ### State methods

    def get_bulb(self, mac):
        """
        Returns a Bulb object corresponding to the bulb with the mac address
        `mac` (a 6-byte bytestring).
        """
        return self.bulbs.get(mac, Bulb('Bulb %s' % _bytes(mac), mac))

    ### Sender methods

    def send(self, packet_type, bulb, packet_fmt, *packet_args):
        """
        Builds and sends a packet to one or more bulbs.
        """
        packet = build_packet(packet_type, self.gateway.mac, bulb,
                              packet_fmt, *packet_args)
        self.logger('>> %s', _bytes(packet))
        self.sender.put(packet)

    def set_power_state(self, is_on, bulb=ALL_BULBS, timeout=None):
        """
        Sets the power state of one or more bulbs.
        """
        with _blocking(self.lock, self.power_state, self.light_state_event,
                       timeout):
            self.send(REQ_SET_POWER_STATE,
                      bulb, '2s', '\x00\x01' if is_on else '\x00\x00')
            self.send(REQ_GET_LIGHT_STATE, ALL_BULBS, '')
        return self.power_state

    def set_light_state_raw(self, hue, saturation, brightness, kelvin,
                            bulb=ALL_BULBS, timeout=None):
        """
        Sets the (low-level) light state of one or more bulbs.
        """
        with _blocking(self.lock, self.light_state, self.light_state_event,
                       timeout):
            self.send(REQ_SET_LIGHT_STATE, bulb, 'xHHHHI',
                      hue, saturation, brightness, kelvin, 0)
            self.send(REQ_GET_LIGHT_STATE, ALL_BULBS, '')
        return self.light_state

    def set_light_state(self, hue, saturation, brightness, kelvin,
                        bulb=ALL_BULBS, timeout=None):
        """
        Sets the light state of one or more bulbs.

        Hue is a float from 0 to 360, saturation and brightness are floats from
        0 to 1, and kelvin is an integer.
        """
        raw_hue = int((hue % 360) / 360.0 * 0xffff) & 0xffff
        raw_sat = int(saturation * 0xffff) & 0xffff
        raw_bright = int(brightness * 0xffff) & 0xffff
        return self.set_light_state_raw(raw_hue, raw_sat, raw_bright, kelvin,
                                        bulb, timeout)

    ### Callback helpers

    def on_discovered(self, fn):
        """
        Registers a function to be called when a gateway is discovered.
        """
        return self.callbacks.register(EVENT_DISCOVERED, fn)

    def on_connected(self, fn):
        """
        Registers a function to be called when a gateway connection is made.
        """
        return self.callbacks.register(EVENT_CONNECTED, fn)

    def on_bulbs_found(self, fn):
        """
        Registers a function to be called when the expected number of bulbs are
        found.
        """
        return self.callbacks.register(EVENT_BULBS_FOUND, fn)

    def on_light_state(self, fn):
        """
        Registers a function to be called when light state data is received.
        """
        return self.callbacks.register(EVENT_LIGHT_STATE, fn)

    def on_power_state(self, fn):
        """
        Registers a function to be called when power state data is received.
        """
        return self.callbacks.register(EVENT_POWER_STATE, fn)

    def on_unknown(self, fn):
        """
        Registers a function to be called when packet data is received with a
        type that has no explicitly registered callbacks.
        """
        return self.callbacks.register(EVENT_UNKNOWN, fn)
        # TODO event constants

    def on_packet(self, packet_type):
        """
        Registers a function to be called when packet data is received with a
        specific type.
        """
        def _wrapper(fn):
            return self.callbacks.register(packet_type, fn)
        return _wrapper

    ### Connection methods

    def connect(self, attempts=20, delay=0.5):
        """
        Connects to a gateway, blocking until a connection is made and bulbs
        are found.

        Step 1: send a gateway discovery packet to the broadcast address, wait
        until we've received some info about the gateway.

        Step 2: connect to a discovered gateway, wait until the connection has
        been completed.

        Step 3: ask for info about bulbs, wait until we've found the number of
        bulbs we expect.

        Raises a ConnectException if any of the steps fail.
        """
        # Broadcast discovery packets until we find a gateway.
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        with closing(sock):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            discover_packet = build_packet(REQ_GATEWAY,
                                           ALL_BULBS, ALL_BULBS, '',
                                           protocol=DISCOVERY_PROTOCOL)

            for _, ok in _retry(self.gateway_found_event, attempts, delay):
                sock.sendto(discover_packet, BROADCAST_ADDRESS)
        if not ok:
            raise ConnectException('discovery failed')
        self.callbacks.put(EVENT_DISCOVERED)

        # Tell the sender to connect to the gateway until it does.
        for _, ok in _retry(self.sender.is_connected, 1, 3):
            self.sender.put(self.gateway)
        if not ok:
            raise ConnectException('connection failed')
        self.callbacks.put(EVENT_CONNECTED)

        # Send light state packets to the gateway until we find bulbs.
        for _, ok in _retry(self.bulbs_found_event, attempts, delay):
            self.send(REQ_GET_LIGHT_STATE, ALL_BULBS, '')
        if not ok:
            raise ConnectException('only found %d of %d bulbs' % (
                                   len(self.bulbs), self.num_bulbs))
        self.callbacks.put(EVENT_BULBS_FOUND)

    @contextmanager
    def run(self):
        """
        A context manager starting up threads to send and receive data from a
        gateway and handle callbacks. Yields when a connection has been made,
        and cleans up connections and threads when it's done.
        """
        listener_thr = _spawn(self.receiver.run)
        callback_thr = _spawn(self.callbacks.run)
        sender_thr = _spawn(self.sender.run)
        logger_thr = _spawn(self.logger.run)

        self.connect()
        try:
            yield
        finally:
            self.stop()

            # Wait for the listener to finish.
            listener_thr.join()
            self.callbacks.put('shutdown')

            # Tell the other threads to finish, and wait for them.
            for obj in [self.callbacks, self.sender, self.logger]:
                obj.stop()
            for thr in [callback_thr, sender_thr, logger_thr]:
                thr.join()

    def run_forever(self):
        """
        Starts a connection and blocks until `stop` is called.
        """
        with self.run():
            self.receiver.is_shutdown.wait()

    def stop(self):
        """
        Gracefully terminates a connection.
        """
        self.receiver.stop()
