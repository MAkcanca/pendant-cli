"""BLE transport: connect, fragment, reassemble.

Responsibilities:
- Negotiate MTU
- Subscribe to the notify characteristic (632de003)
- Outgoing: encode `ServerCommandMsg` proto, fragment into `BLEMessageFromNativeToPendant`
  wrapper messages, write each fragment to the write char (632de002)
- Incoming: decode each notification as a `BLEMessageFromPendantToNative`,
  reassemble payload by fragment seq, decode the inner `PendantAllMsg`,
  match request/response by `request_id` and dispatch to subscribers.

The fragment wrapper protocol is verified against the pendant-side
reassembly handler (`ble_command_recv__decode_dispatch`, FUN_0000e668).
Each fragment carries:

    BLEMessageFromNativeToPendant {
        uint32 index           = 1;   // monotonic message id
        uint32 ble_fragment_seq = 2;  // 0..N-1
        uint32 num_fragments   = 3;
        bytes  payload          = 4;
    }

The pendant accumulates `payload` bytes by index, and once
`ble_fragment_seq + 1 == num_fragments` decodes the concatenated bytes
as a `ServerCommandMsg`. Same shape going the other way (`PendantToNative`).
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

from . import (
    BATTERY_LEVEL_UUID,
    BATTERY_SERVICE_UUID,
    DESIRED_MTU,
    NOTIFY_CHAR_UUID,
    SERVICE_UUID,
    WRITE_CHAR_UUID,
)

log = logging.getLogger(__name__)


class PendantNotPairedError(RuntimeError):
    """Raised when a write fails because the BLE link isn't paired.

    See the message body for resolution instructions. This is a
    user-facing error, not a bug."""
    pass


# ---------------------------------------------------------------------------
# Cleanup-path disconnect tolerance.
#
# When the BLE link drops during a sync, the cleanup commands the client
# tries to send (stop_batch_download, stop_notify, disconnect) fail with
# misleading error messages. We treat any of these as "already
# disconnected" and suppress them on cleanup paths only. The user's data
# has already been written by that point; raising would mask success.
#
# Markers observed in the wild across bleak backends:
#   - bleak.exc.BleakError("Not connected")           (all backends)
#   - WinRT:    OSError(-2147023673, "The operation was canceled by the user")
#   - WinRT:    "Object reference not set to an instance of an object"
#                (race when device drops mid-write)
#   - BlueZ:    "Software caused connection abort" / "No such device"
#   - CoreBluetooth: "The peripheral is not connected"
_DISCONNECT_MARKERS = (
    "not connected",
    "the operation was canceled",
    "the operation was cancelled",
    "operation was canceled",
    "operation was cancelled",
    "-2147023673",
    "the peripheral is not connected",
    "no such device",
    "software caused connection abort",
    "object reference not set",
)


def is_disconnect_exception(exc: BaseException) -> bool:
    """True if an exception looks like 'BLE link already gone' rather
    than a genuine command failure."""
    if not isinstance(exc, (BleakError, OSError, EOFError)):
        return False
    msg = str(exc).lower()
    return any(m in msg for m in _DISCONNECT_MARKERS)


async def suppress_disconnect(awaitable, *, what: str) -> None:
    """Run an awaitable as part of a cleanup path. If it raises a
    'BLE link already gone' error, log at INFO and swallow it. Any
    other Exception is logged at WARNING and also swallowed so it
    cannot mask whatever sent us into cleanup.

    BaseException (KeyboardInterrupt, SystemExit, GeneratorExit) is
    NOT caught — those must propagate."""
    try:
        await awaitable
    except Exception as e:
        if is_disconnect_exception(e):
            log.info("Skipping %s — BLE link already closed (%s: %s)",
                     what, type(e).__name__, e)
        else:
            log.warning(
                "Cleanup step %s failed; continuing (%s: %s)",
                what, type(e).__name__, e,
            )

# Maximum payload bytes per fragment. The ATT MTU is the negotiated MTU
# minus 3 bytes ATT overhead. The fragment wrapper itself adds a few
# proto-encoding bytes (tag + varints for index/seq/num/payload-length).
# Empirically the official app sends ~470-byte payload chunks at MTU=498.
# Fall back to a conservative value if MTU negotiation fails.
DEFAULT_FRAGMENT_PAYLOAD = 470


# ---------------------------------------------------------------------------
# Fragment wrapper: minimal hand-rolled encoder/decoder so we don't have to
# regen the common.proto for every transport-level call. This matches:
#
#   message BLEMessageFromNativeToPendant {
#     uint32 index            = 1;
#     uint32 ble_fragment_seq = 2;
#     uint32 num_fragments    = 3;
#     bytes  payload          = 4;
#   }
#   message BLEMessageFromPendantToNative { ...same shape... }
#
# The wire format is plain protobuf so a hand-rolled encoder is trivial.

def _varint_encode(value: int) -> bytes:
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value & 0x7F)
    return bytes(out)


def _varint_decode(data: bytes, pos: int) -> tuple[int, int]:
    """Returns (value, new_pos)."""
    result = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise ValueError("truncated varint")
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def encode_fragment(index: int, seq: int, num_frags: int, payload: bytes) -> bytes:
    """Encode a BLEMessageFromNativeToPendant message."""
    out = bytearray()
    # field 1 (index, varint)
    out += b"\x08" + _varint_encode(index)
    # field 2 (ble_fragment_seq, varint)
    out += b"\x10" + _varint_encode(seq)
    # field 3 (num_fragments, varint)
    out += b"\x18" + _varint_encode(num_frags)
    # field 4 (payload, length-delimited bytes)
    out += b"\x22" + _varint_encode(len(payload)) + payload
    return bytes(out)


@dataclass
class IncomingFragment:
    index: int
    seq: int
    num_fragments: int
    payload: bytes


def decode_fragment(data: bytes) -> IncomingFragment:
    """Decode one BLEMessageFromPendantToNative message."""
    index = seq = num = 0
    payload = b""
    pos = 0
    while pos < len(data):
        tag, pos = _varint_decode(data, pos)
        wire_type = tag & 0x07
        field_num = tag >> 3
        if wire_type == 0:  # varint
            v, pos = _varint_decode(data, pos)
            if field_num == 1:
                index = v
            elif field_num == 2:
                seq = v
            elif field_num == 3:
                num = v
        elif wire_type == 2:  # length-delimited
            length, pos = _varint_decode(data, pos)
            buf = data[pos:pos + length]
            pos += length
            if field_num == 4:
                payload = buf
        else:
            raise ValueError(f"unexpected wire-type {wire_type} for field {field_num}")
    return IncomingFragment(index=index, seq=seq, num_fragments=num, payload=payload)


# ---------------------------------------------------------------------------

@dataclass
class _Reassembly:
    expected_fragments: int = 0
    received: dict[int, bytes] = field(default_factory=dict)

    def add(self, frag: IncomingFragment) -> Optional[bytes]:
        if frag.num_fragments and self.expected_fragments == 0:
            self.expected_fragments = frag.num_fragments
        self.received[frag.seq] = frag.payload
        if (self.expected_fragments
                and len(self.received) >= self.expected_fragments):
            return b"".join(self.received[i] for i in range(self.expected_fragments))
        return None


class PendantTransport:
    """Owns the BleakClient, fragment reassembly, and the message dispatcher."""

    def __init__(self, client: BleakClient):
        self._client = client
        self._reassembly: dict[int, _Reassembly] = defaultdict(_Reassembly)
        self._next_outgoing_index = 1
        self._fragment_payload = DEFAULT_FRAGMENT_PAYLOAD
        # Subscribers receive each fully-reassembled inbound `PendantAllMsg`
        # (raw bytes — caller decodes).
        self._subscribers: list[Callable[[bytes], Awaitable[None] | None]] = []
        # Raw-fragment subscribers receive every BLE notification verbatim
        # (no reassembly). Used by `pendant debug`.
        self._raw_subscribers: list[Callable[[bytes], Awaitable[None] | None]] = []
        self._notify_started = False
        self._write_with_response = False  # set in __aenter__ from char props

    # ----- connection -----

    @classmethod
    async def find_pendants(cls, timeout: float = 8.0) -> list[BLEDevice]:
        """Scan for advertising pendants. Pendant advertises SERVICE_UUID."""
        return [
            d for d in await BleakScanner.discover(timeout=timeout)
            if (d.metadata or {}).get("uuids", []) is not None
            and SERVICE_UUID.lower() in [
                u.lower() for u in (d.metadata or {}).get("uuids", []) or []
            ]
        ]

    async def __aenter__(self) -> "PendantTransport":
        await self._client.connect()
        # Attempt MTU exchange. Bleak only exposes this on macOS/Linux as a
        # property; on Windows MTU is negotiated automatically.
        try:
            await self._client.exchange_mtu(DESIRED_MTU)
        except (NotImplementedError, AttributeError):
            pass
        try:
            mtu = self._client.mtu_size
        except Exception:
            mtu = None
        # On Windows, mtu_size is read before WinRT finishes MTU negotiation
        # (the OS triggers a "max_pdu_size_changed" event later). Wait briefly
        # and re-read if we got the default value of 23.
        if isinstance(mtu, int) and mtu < 100:
            await asyncio.sleep(0.7)
            try:
                new_mtu = self._client.mtu_size
                if isinstance(new_mtu, int) and new_mtu > mtu:
                    log.info("MTU upgraded after settle: %d -> %d", mtu, new_mtu)
                    mtu = new_mtu
            except Exception:
                pass
        if isinstance(mtu, int) and mtu > 7:
            # ATT MTU minus 3 (opcode+handle) minus a small fragment-header
            # margin. Floor to a friendly value.
            self._fragment_payload = max(150, mtu - 30)
        # Pick write-with-response vs without-response based on the
        # characteristic's actual properties. Some pendant builds expose
        # only one, and using the wrong type silently no-ops on Windows.
        try:
            char = self._client.services.get_characteristic(WRITE_CHAR_UUID)
            props = list(char.properties or [])
            log.info("Write char properties: %s", props)
            if "write" in props:
                self._write_with_response = True
            elif "write-without-response" in props:
                self._write_with_response = False
            else:
                log.warning(
                    "Write characteristic %s has neither 'write' nor "
                    "'write-without-response' in its properties: %s",
                    WRITE_CHAR_UUID, props)
        except Exception as e:
            log.warning("Could not inspect write-char properties: %s", e)
        log.info("Connected; MTU=%s, fragment payload size=%d, write_with_response=%s",
                 mtu, self._fragment_payload, self._write_with_response)
        # The pendant requires an encrypted/authenticated link for writes
        # to 632de002 (responds with GATT error 5 = Insufficient Authentication
        # otherwise). Try to pair if we're not already.
        try:
            paired = await self._client.pair()
            log.info("Pairing result: %s", paired)
        except NotImplementedError:
            log.warning("Bleak backend doesn't expose .pair() — pair the "
                        "pendant via OS Bluetooth settings before retrying.")
        except Exception as e:
            # Some backends raise if already paired — that's fine.
            log.info("Pair attempt: %s (already bonded is OK)", e)
        await self._start_notify()
        return self

    async def __aexit__(self, *exc):
        # Both of these can fail if the device has already disconnected
        # (drops mid-sync, range loss, OS Bluetooth-stack hiccup). The
        # data the caller cared about has already been delivered by the
        # time we get here, so suppress disconnect errors and let the
        # caller's exit code reflect whether the *work* succeeded.
        if self._notify_started:
            await suppress_disconnect(
                self._client.stop_notify(NOTIFY_CHAR_UUID),
                what="stop_notify",
            )
        await suppress_disconnect(
            self._client.disconnect(),
            what="disconnect",
        )

    # ----- notify reception -----

    async def _start_notify(self) -> None:
        async def _on_data(_: int, data: bytearray):
            raw = bytes(data)
            log.debug("notify rx: %d bytes  %s", len(raw), raw[:32].hex())
            # Dispatch raw bytes to any debug subscribers regardless of parse.
            for cb in list(self._raw_subscribers):
                r = cb(raw)
                if asyncio.iscoroutine(r):
                    await r
            try:
                frag = decode_fragment(raw)
            except Exception:
                log.exception("could not decode incoming fragment (%d bytes)", len(raw))
                return
            full = self._reassembly[frag.index].add(frag)
            if full is not None:
                self._reassembly.pop(frag.index, None)
                await self._dispatch(full)

        await self._client.start_notify(NOTIFY_CHAR_UUID, _on_data)
        self._notify_started = True
        log.info("Subscribed to notify char %s", NOTIFY_CHAR_UUID)

    async def _dispatch(self, payload: bytes) -> None:
        for cb in list(self._subscribers):
            r = cb(payload)
            if asyncio.iscoroutine(r):
                await r

    def on_message(self, cb: Callable[[bytes], Awaitable[None] | None]) -> None:
        """Register a callback to receive every reassembled `PendantAllMsg`
        (the raw protobuf bytes — caller decodes)."""
        self._subscribers.append(cb)

    def on_raw_fragment(self, cb: Callable[[bytes], Awaitable[None] | None]) -> None:
        """Register a callback that receives EVERY raw notification (one
        per BLE GATT notify) — no reassembly. For diagnostics."""
        self._raw_subscribers.append(cb)

    # ----- outgoing -----

    async def send_command(self, server_command_bytes: bytes) -> int:
        """Send a serialized `ServerCommandMsg`. Returns the index used."""
        index = self._next_outgoing_index
        self._next_outgoing_index += 1

        chunks = [
            server_command_bytes[i:i + self._fragment_payload]
            for i in range(0, max(1, len(server_command_bytes)), self._fragment_payload)
        ]
        if not chunks:
            chunks = [b""]
        log.debug("send_command index=%d total_payload=%d in %d fragment(s) "
                  "(write_with_response=%s)",
                  index, len(server_command_bytes), len(chunks),
                  self._write_with_response)
        for seq, chunk in enumerate(chunks):
            wire = encode_fragment(index, seq, len(chunks), chunk)
            log.debug("  frag seq=%d/%d wire_len=%d  %s",
                      seq, len(chunks), len(wire), wire[:32].hex())
            try:
                await self._client.write_gatt_char(
                    WRITE_CHAR_UUID, wire, response=self._write_with_response)
            except Exception as e:
                # On Windows, `BleakGATTProtocolError(5, 'Insufficient
                # Authentication')` means the link isn't paired/bonded yet
                # and the pendant won't accept writes. Surface a clearer
                # message so the user knows what to do.
                msg = str(e)
                if "Insufficient Authentication" in msg or "(5," in msg:
                    raise PendantNotPairedError(
                        "Pendant rejected the write because the BLE link is "
                        "not paired. The pendant remembers its previous bond "
                        "(usually the Limitless Android app). Resolve by:\n"
                        "  1) Open the Limitless app, factory-reset the pendant\n"
                        "     (Settings -> Pendant -> Factory Reset). The bond\n"
                        "     is cleared on both sides.\n"
                        "  2) Or: in Windows Settings -> Bluetooth & devices,\n"
                        "     pair 'Pendant' once, then retry this command.\n"
                        "  3) Or: run `pendant pair <address>` to attempt\n"
                        "     pairing programmatically (requires the pendant\n"
                        "     to currently accept new bonds — i.e. its old\n"
                        "     bond must be cleared first)."
                    ) from e
                raise
        return index

    # ----- Direct GATT inspection -----

    async def list_characteristics(self) -> list[dict]:
        """For diagnostics: return a list of every characteristic's UUID
        and properties."""
        out = []
        for svc in self._client.services:
            for ch in svc.characteristics:
                out.append({
                    "service": svc.uuid,
                    "char": ch.uuid,
                    "handle": ch.handle,
                    "properties": list(ch.properties or []),
                    "descriptors": [d.uuid for d in (ch.descriptors or [])],
                })
        return out

    # ----- battery (standard service) -----

    async def read_battery_level(self) -> int:
        """Read the standard Battery Service level (0..100)."""
        b = await self._client.read_gatt_char(BATTERY_LEVEL_UUID)
        if not b:
            raise RuntimeError("empty battery level")
        return int(b[0])
