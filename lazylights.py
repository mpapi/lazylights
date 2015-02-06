from collections import namedtuple
from contextlib import closing, contextmanager
import socket
import struct
import time


_BASE_FORMAT = '<HHxxxx6sxx6sxxQHxx'
_FORMAT_SIZE = struct.calcsize(_BASE_FORMAT)

_SOCKET_BUFFER_SIZE = 65536

ALL_BULBS = '\x00' * 6

LIFX_PORT = 56700
ADDR_BROADCAST = ('255.255.255.255', LIFX_PORT)
ADDR_LISTEN = ('0.0.0.0', LIFX_PORT)

PROTOCOL_DISCOVERY = 0x3400
PROTOCOL_COMMAND = 0x1400

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
    RESP_LIGHT_STATE: ('<6H32s8s', 'hue', 'saturation', 'brightness',
                       'kelvin', 'dim', 'power', 'label', 'tags'),
}

Header = namedtuple('Header', 'size protocol mac gateway time packet_type')
Bulb = namedtuple('Bulb', 'gateway_mac mac addr')
State = namedtuple('State', """
    bulb hue saturation brightness kelvin power label
    """)


def parse_packet(data, format=None):
    """
    Parses a Lifx data packet (as a bytestring), returning a Header object for
    the fields that are common to all data packets, and a bytestring of payload
    data for the type-specific fields (suitable for passing to
    `parse_payload`).
    """
    unpacked = struct.unpack(_BASE_FORMAT, data[:_FORMAT_SIZE])
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
    - `gateway`, a 6-byte string containing the mac address of the gateway
      (as contained in the response to a `REQ_GATEWAY` -- for the 2.0 firmware
      update, this appears to always be "LIFXV2")
    - `bulb`, a 6-byte string containing the mac address of the target bulb
    - `payload_fmt`, a `struct`-compatible string that describes the format
      of the payload part of the packet
    - `payload_args`, the values to use to build the payload part of the packet

    Additionally, the `protocol` keyword argument can be used to override the
    protocol field in the packet.
    """
    protocol = kwargs.get('protocol', PROTOCOL_COMMAND)

    packet_fmt = _BASE_FORMAT + payload_fmt
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


@contextmanager
def _listening_socket(timeout=0.1):
    """
    Creates a UDP socket for receiving packets, bound to the listening address,
    with a floating-point `timeout` in seconds. On exit, the socket is closed.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.bind(ADDR_LISTEN)
    sock.settimeout(timeout)
    with closing(sock):
        yield sock


@contextmanager
def _sending_socket(broadcast=False):
    """
    Creates a UDP socket for sending packets. If `broadcast` is True, the
    socket is set up for sending to a broadcast address (e.g. for bulb
    discovery). On exit, the socket is closed.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    if broadcast:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    with closing(sock):
        yield sock


def _send(bulbs, packet_type, packet_fmt, *packet_args):
    """
    Builds a packet and sends it to each bulb in `bulbs`.
    """
    with _sending_socket() as sock:
        for bulb in bulbs:
            packet = build_packet(packet_type, bulb.gateway_mac, bulb.mac,
                                  packet_fmt, *packet_args)
            sock.sendto(packet, bulb.addr)


def set_power(bulbs, is_on):
    """
    Sets the power of `bulbs`, turning them on if `is_on` is True, and off
    otherwise.
    """
    _send(bulbs, REQ_SET_POWER_STATE, '2s',
          '\xff\xff' if is_on else '\x00\x00')


def set_state(bulbs, hue, saturation, brightness, kelvin, fade, raw=False):
    """
    Sets the state of `bulbs`.

    If `raw` is True, hue, saturation, and brightness should be integers in the
    range of 0x0000 to 0xffff. Otherwise, hue should be a float from 0.0 to
    360.0 (where 360.0/0.0 is red), and saturation and brightness are floats
    from 0.0 to 1.0 (0 being least saturated and least bright)..

    `kelvin` is an integer from 2000 to 8000, where 2000 is the warmest and
    8000 is the coolest. If this is non-zero, the white spectrum is used
    instead of the color spectrum (hue and saturation are be ignored).

    `fade` is an integer number of milliseonds over which to transition the
    state change, carried out by the bulbs.
    """
    if not raw:
        hue = int((hue % 360) / 360.0 * 0xffff) & 0xffff
        saturation = int(saturation * 0xffff) & 0xffff
        brightness = int(brightness * 0xffff) & 0xffff

    _send(bulbs, REQ_SET_LIGHT_STATE, 'xHHHHI',
          hue, saturation, brightness, kelvin, fade)


def _recv(timeout=1, only=None):
    """
    A generator function that produces packets by starting up a listening
    socket. It will generate packets for no more than `timeout` seconds, and if
    `only` is given, only packets with that header type are returned.

    The generator produces tuples of (sender's address, `Header`, dictionary of
    payload fields), combining the address with the reults of `parse_payload`.
    """
    with _listening_socket() as sock:
        start_time = time.time()
        while True:
            try:
                data, addr = sock.recvfrom(_SOCKET_BUFFER_SIZE)
                header, rest = parse_packet(data)

                if header.packet_type in _PAYLOADS and \
                        (not only or header.packet_type == only):
                    fields = _PAYLOADS[header.packet_type]
                    payload = parse_payload(rest, *fields)
                    yield addr, header, payload
            except socket.timeout:
                pass

            now = time.time()
            if now - start_time > timeout:
                break


def find_bulbs(expected_bulbs=None, send_every=0.5, timeout=1):
    """
    Queries the local network for bulbs, and returns a `set` of `Bulb` objects.

    It will return after `timeout` seconds, or after `expected_bulbs` are found
    (if not None), whichever happens first.

    `send_every` is used to control how frequently discovery packets are sent.
    """
    discover_packet = build_packet(REQ_GATEWAY, ALL_BULBS, ALL_BULBS, '',
                                   protocol=PROTOCOL_DISCOVERY)
    bulbs = set()

    with _sending_socket(broadcast=True) as sock:
        sock.sendto(discover_packet, ADDR_BROADCAST)
        discover_sent = time.time()

        for addr, header, payload in _recv(timeout=timeout, only=RESP_GATEWAY):
            bulbs.add(Bulb(header.gateway, header.mac, addr))
            if len(bulbs) == expected_bulbs:
                return bulbs

            now = time.time()
            if now - discover_sent > send_every:
                sock.sendto(discover_packet, ADDR_BROADCAST)
                discover_sent = now

    return bulbs


def get_state(bulbs, timeout=1):
    """
    Asks `bulbs` for their state, returning a list of `State` objects.

    Returns after `timeout` seconds, or responses were obtained from all of
    `bulbs`, whichever happens first.
    """
    _send(bulbs, REQ_GET_LIGHT_STATE, '')

    bulbs = dict((bulb.addr, bulb) for bulb in bulbs)
    states = set()
    for addr, header, payload in _recv(timeout=timeout, only=RESP_LIGHT_STATE):
        if addr not in bulbs:
            continue
        states.add(State(bulb=bulbs[addr],
                         **dict((key, val) for key, val in payload.items()
                                if key in State._fields)))
        if len(states) == len(bulbs):
            break
    return list(states)


def refresh(expected_bulbs=None, timeout=1):
    """
    Wraps `find_bulbs` and `get_state`, returning a list of `State` objects.

    Returns after `expected_bulbs` unique bulbs are found, or `timeout`
    seconds, whichever happens first.
    """
    bulbs = find_bulbs(expected_bulbs=expected_bulbs, timeout=timeout)
    if not bulbs:
        return []
    return get_state(bulbs, timeout=timeout)
