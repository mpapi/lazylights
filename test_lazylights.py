"""
Unit tests for lazylights.
"""
from nose.tools import eq_

import lazylights
from lazylights import parse_packet, parse_payload, build_packet


OFF_PACKET = lazylights._unbytes("26000034000000000000000000000000"
                                 "99887766554400000000000000000000"
                                 "150000000000")
GATEWAY = '\x99\x88\x77\x66\x55\x44'


def test_parse_packet():
    header, data = parse_packet(OFF_PACKET)
    eq_(0x26, header.size)
    eq_(lazylights.COMMAND_PROTOCOL, header.protocol)
    eq_(lazylights.ALL_BULBS, header.mac)
    eq_(GATEWAY, header.gateway)
    eq_(lazylights.REQ_SET_POWER_STATE, header.packet_type)
    eq_('\x00\x00', data)


def test_parse_payload():
    payload = parse_payload('\x00\x01', '>H', 'is_on')
    eq_(['is_on'], payload.keys())
    eq_(1, payload['is_on'])


def test_build_packet():
    packet = build_packet(lazylights.REQ_SET_POWER_STATE,
                          GATEWAY, lazylights.ALL_BULBS,
                          '2s', '\x00\x00')
    eq_(packet, OFF_PACKET)
