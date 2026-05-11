"""Verify fragment encode/decode round-trips and reassembly logic."""
from __future__ import annotations

from pendant_client.ble import (
    IncomingFragment,
    _Reassembly,
    decode_fragment,
    encode_fragment,
)


def test_encode_round_trip_short_payload():
    payload = b"hello world"
    wire = encode_fragment(index=42, seq=0, num_frags=1, payload=payload)
    f = decode_fragment(wire)
    assert f.index == 42
    assert f.seq == 0
    assert f.num_fragments == 1
    assert f.payload == payload


def test_encode_round_trip_large_index():
    # Index can grow over time; verify a multi-byte varint round-trips.
    payload = b"x" * 400
    wire = encode_fragment(index=12345, seq=2, num_frags=5, payload=payload)
    f = decode_fragment(wire)
    assert (f.index, f.seq, f.num_fragments) == (12345, 2, 5)
    assert f.payload == payload


def test_reassembly_in_order():
    r = _Reassembly()
    chunks = [b"AAA", b"BBB", b"CCC"]
    out = None
    for i, c in enumerate(chunks):
        out = r.add(IncomingFragment(index=1, seq=i, num_fragments=3, payload=c))
        if i < 2:
            assert out is None
    assert out == b"AAABBBCCC"


def test_reassembly_out_of_order():
    r = _Reassembly()
    chunks = [b"AAA", b"BBB", b"CCC"]
    indices = [2, 0, 1]
    out = None
    for i in indices:
        out = r.add(IncomingFragment(index=1, seq=i, num_fragments=3, payload=chunks[i]))
    assert out == b"AAABBBCCC"


def test_reassembly_single_fragment():
    r = _Reassembly()
    out = r.add(IncomingFragment(index=99, seq=0, num_fragments=1, payload=b"hi"))
    assert out == b"hi"
