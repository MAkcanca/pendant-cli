"""Decode a captured `BatchIngestRequest` .bin into audio chunks.

This module is the post-processing layer. The capture path
(`pendant_client.session.PendantSession.download_raw_pendant_messages`)
writes byte-fidelity `BatchIngestRequest` files matching the official
Limitless app's `C10459i.m41379i`. This module reads them back and
applies our own client-side splitting heuristic — pre-roll merging —
to produce playable per-recording `.opus` files.

The protocol does *not* tell us where recording boundaries are. The
official app forwards every chunk to the backend and lets the backend
split. Our heuristic is documented below in
`split_recordings_with_preroll_merge`.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable, Iterator, Optional

from .crypto import AudioKeySet, decrypt_audio
from .flash_page import parse_flash_page
from .proto import server_pb2

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Wire-fidelity BatchIngestRequest writer/reader.
#
# We hand-roll the protobuf encoding so the captured PendantAllMsg bytes
# survive byte-for-byte. Round-tripping through `BatchIngestRequest.
# parseFrom`/`SerializeToString` produces semantically equivalent bytes
# but not necessarily byte-identical (proto3 doesn't promise canonical
# encoding for repeated submessages).

_TAG_FIELD_1_LENDELIM = 0x0A   # (1 << 3) | wire_type=2
_TAG_FIELD_2_LENDELIM = 0x12   # (2 << 3) | wire_type=2


def _encode_varint(n: int) -> bytes:
    out = bytearray()
    while n > 0x7F:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n & 0x7F)
    return bytes(out)


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
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


def write_batch_ingest_request(
    *,
    messages: Iterable[bytes],
    ble_identifier: str,
    path: Path,
) -> int:
    """Write a `BatchIngestRequest` proto to `path`, byte-perfectly
    preserving each `PendantAllMsg`'s wire bytes.

    Schema (`server.proto`):
        message BatchIngestRequest {
          repeated PendantAllMsg messages = 1;
          string ble_identifier            = 2;
        }

    Returns the number of bytes written.
    """
    # We stream incrementally so the full BatchIngestRequest never has
    # to fit in memory.
    n_msgs = 0
    total = 0
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("wb") as fp:
        for msg_bytes in messages:
            fp.write(bytes([_TAG_FIELD_1_LENDELIM]))
            fp.write(_encode_varint(len(msg_bytes)))
            fp.write(msg_bytes)
            n_msgs += 1
            total += 1 + len(_encode_varint(len(msg_bytes))) + len(msg_bytes)
        if ble_identifier:
            id_bytes = ble_identifier.encode("utf-8")
            fp.write(bytes([_TAG_FIELD_2_LENDELIM]))
            fp.write(_encode_varint(len(id_bytes)))
            fp.write(id_bytes)
            total += 1 + len(_encode_varint(len(id_bytes))) + len(id_bytes)
        fp.flush()
    tmp.replace(path)
    log.info("Wrote BatchIngestRequest: %d messages, %d bytes -> %s",
             n_msgs, total, path)
    return total


def iter_messages_from_bin(path: Path) -> Iterator[bytes]:
    """Read a `BatchIngestRequest` .bin and yield each `PendantAllMsg`'s
    raw wire bytes. The bytes are returned verbatim (they were written
    verbatim by `write_batch_ingest_request`)."""
    data = path.read_bytes()
    pos = 0
    while pos < len(data):
        tag, pos = _read_varint(data, pos)
        wire_type = tag & 0x07
        field_number = tag >> 3
        if wire_type == 0:
            _, pos = _read_varint(data, pos)
        elif wire_type == 1:
            pos += 8
        elif wire_type == 2:
            length, pos = _read_varint(data, pos)
            chunk = data[pos:pos + length]
            pos += length
            if field_number == 1:
                yield bytes(chunk)
            # field 2 = ble_identifier, ignore here
        elif wire_type == 5:
            pos += 4
        else:
            raise ValueError(f"unsupported wire type {wire_type} in BIR")


def read_batch_ingest_request_metadata(path: Path) -> dict:
    """Quick stats about a .bin without yielding every message — used
    by the CLI for the decode summary line."""
    n_messages = 0
    ble_identifier = ""
    data = path.read_bytes()
    pos = 0
    while pos < len(data):
        tag, pos = _read_varint(data, pos)
        wire_type = tag & 0x07
        field_number = tag >> 3
        if wire_type == 2:
            length, pos = _read_varint(data, pos)
            if field_number == 1:
                n_messages += 1
            elif field_number == 2:
                ble_identifier = data[pos:pos + length].decode(
                    "utf-8", errors="replace")
            pos += length
        elif wire_type == 0:
            _, pos = _read_varint(data, pos)
        elif wire_type == 1:
            pos += 8
        elif wire_type == 5:
            pos += 4
        else:
            raise ValueError(f"unsupported wire type {wire_type}")
    return {
        "messages": n_messages,
        "ble_identifier": ble_identifier,
        "size_bytes": len(data),
    }


# ---------------------------------------------------------------------------
# Audio decoding from captured PendantAllMsg bytes.

def iter_audio_chunks_from_messages(
    messages: Iterable[bytes],
    keys: Optional[AudioKeySet] = None,
    accept_types: tuple[int, ...] = (1, 2),  # REAL_TIME, BATCH
):
    """Parse a stream of raw `PendantAllMsg` bytes into `AudioChunk`s.

    Used by:
      - The live download path (`PendantSession.download`) — single msg per call
      - The offline decode CLI — many msgs from a captured .bin

    Filters by `StorageBufferMsg.ingest_type` (REAL_TIME / BATCH by
    default). Decrypts in-place if a key is provided and the chunk has
    `encrypted_codec_beamforming_data`.
    """
    # Local import to avoid the cycle with session.py.
    from .session import AudioChunk

    for payload in messages:
        msg = server_pb2.PendantAllMsg()
        try:
            msg.ParseFromString(payload)
        except Exception as e:
            log.warning("Skipping malformed PendantAllMsg: %s", e)
            continue
        if msg.WhichOneof("content") != "storage_buffer":
            continue
        sb = msg.storage_buffer
        if sb.ingest_type not in accept_types:
            continue
        page_bytes = bytes(sb.flash_page)
        if not page_bytes:
            continue
        try:
            page = parse_flash_page(page_bytes)
        except Exception as e:
            log.warning("Skipping malformed FlashPage on page %d: %s",
                        sb.index, e)
            continue
        for chunk in page.chunks:
            if (chunk.audio is not None
                    and chunk.audio.encrypted is not None):
                if keys is not None:
                    try:
                        chunk.audio.opus_packets = decrypt_audio(
                            chunk.audio, keys.aes_key)
                    except Exception as e:
                        log.warning(
                            "decrypt_audio failed on page %d: %s",
                            sb.index, e)
                else:
                    log.debug(
                        "Encrypted chunk on page %d but no key provided "
                        "— skipping audio bytes", sb.index)
            yield AudioChunk(
                chunk=chunk,
                storage_session=sb.session,
                run=sb.run,
                page_seq_uptime_ms=sb.seq,
                page_index=sb.index,
                ingest_type=sb.ingest_type,
                flash_page_error=sb.flash_page_error,
                absolute_timestamp_ms=page.absolute_timestamp_ms,
                boot_uptime_ms=page.boot_uptime_ms,
                received_at_unix_ms=0,  # unknown offline; CLI fills it
            )


# ---------------------------------------------------------------------------
# Heuristic C: pre-roll merging.
#
# The pendant emits chunks in this typical pattern around a recording:
#
#    <pre-roll audio>          # ring-buffer flush, no flag
#    did_start_recording=true  # marker chunk (no audio bytes)
#    <main audio>              # the recording body
#    did_stop_recording=true   # marker chunk (no audio bytes)
#
# Naively splitting on the start-flag arrival creates 2 files per
# recording (4-7 chunk pre-roll + main body). The pre-roll is logically
# part of the recording — VAD lookback so the start of speech isn't
# clipped. So when did_start_recording arrives at a writer that does
# *not* yet have an explicit start, we MERGE: keep adding to the same
# writer and mark it as having an explicit start now.
#
# Otherwise, a did_start_recording arriving at a writer with an explicit
# start means a new recording is beginning — close + reopen.

class RecordingSplitter:
    """Stateful pre-roll-merging splitter.

    Drive it by calling `feed(chunk)` for each AudioChunk in stream
    order. Writers are created via the `open_writer(index, chunk)`
    callback you pass at construction. Writers should expose
    `add(chunk)`, `close()`, `opened_with_start_marker: bool`, and
    `closed_with_stop_marker: bool` — see `cli._RecordingWriter`.
    """

    def __init__(self, open_writer):
        self._open_writer = open_writer
        self._current = None
        self._current_has_explicit_start = False
        self._next_index = 0

    @property
    def current(self):
        return self._current

    def _open(self, chunk, *, has_explicit_start: bool):
        writer = self._open_writer(self._next_index, chunk)
        self._next_index += 1
        self._current = writer
        self._current_has_explicit_start = has_explicit_start
        if has_explicit_start:
            writer.opened_with_start_marker = True
        return writer

    def _close(self, *, with_stop_marker: bool = False):
        if self._current is None:
            return
        if with_stop_marker:
            self._current.closed_with_stop_marker = True
        self._current.close()
        self._current = None
        self._current_has_explicit_start = False

    def feed(self, chunk) -> None:
        # Phase 1: handle did_start_recording. If we already have an
        # explicit-start writer, this is a new recording. Otherwise the
        # current (pre-roll-only) writer absorbs the start marker.
        if chunk.is_recording_start:
            if self._current is not None and self._current_has_explicit_start:
                self._close()
            if self._current is not None:
                self._current_has_explicit_start = True
                self._current.opened_with_start_marker = True
            else:
                # Lazy: a start marker with no preceding audio. Don't
                # open a writer until audio arrives — but remember that
                # the next-opened writer should be flagged as opened
                # with a start marker. We piggyback on the
                # `_pending_start_marker` flag.
                self._pending_start_marker = True
                return

        # Phase 2: audio output.
        if chunk.has_audio:
            if self._current is None:
                pending = getattr(self, "_pending_start_marker", False)
                self._open(chunk, has_explicit_start=pending)
                if pending:
                    self._pending_start_marker = False
            self._current.add(chunk)

        # Phase 3: did_stop_recording closes.
        if chunk.is_recording_stop and self._current is not None:
            self._close(with_stop_marker=True)

    def finalize(self) -> None:
        """Close any open writer (sync ended, no further chunks)."""
        if self._current is not None:
            self._current.close()
            self._current = None
            self._current_has_explicit_start = False
