"""Tests for `decode.py` — BatchIngestRequest round-trip + splitter."""
from __future__ import annotations

from pathlib import Path

import pytest

from pendant_client.decode import (
    RecordingSplitter,
    iter_audio_chunks_from_messages,
    iter_messages_from_bin,
    read_batch_ingest_request_metadata,
    write_batch_ingest_request,
)
from pendant_client.proto import flash_page_pb2, server_pb2


# ---------------------------------------------------------------------------
# BatchIngestRequest round-trip

def _make_storage_buffer_msg(*, page_index: int, flash_page: bytes,
                              ingest_type: int = 2,
                              session: int = 0,
                              run: int = 0,
                              seq: int = 0) -> bytes:
    """Build one PendantAllMsg with a StorageBufferMsg in it."""
    msg = server_pb2.PendantAllMsg()
    msg.storage_buffer.ingest_type = ingest_type
    msg.storage_buffer.session = session
    msg.storage_buffer.run = run
    msg.storage_buffer.seq = seq
    msg.storage_buffer.index = page_index
    msg.storage_buffer.flash_page = flash_page
    return msg.SerializeToString()


def _make_audio_flash_page(opus_bytes: bytes,
                            did_start_recording: bool = False,
                            did_stop_recording: bool = False) -> bytes:
    page = flash_page_pb2.FlashPage()
    c = page.chunks.add()
    c.audio_data.codec_beamforming_data = opus_bytes
    c.audio_data.did_start_recording = did_start_recording
    c.audio_data.did_stop_recording = did_stop_recording
    return page.SerializeToString()


def _make_storage_session_stop_page() -> bytes:
    page = flash_page_pb2.FlashPage()
    c = page.chunks.add()
    c.storage_status.did_stop_storage_session = True
    return page.SerializeToString()


def test_batch_ingest_request_round_trip(tmp_path: Path):
    """write_batch_ingest_request → iter_messages_from_bin should round
    trip the exact bytes."""
    msg_a = _make_storage_buffer_msg(
        page_index=10,
        flash_page=_make_audio_flash_page(b"\xb8" + b"AAAA" * 5),
    )
    msg_b = _make_storage_buffer_msg(
        page_index=11,
        flash_page=_make_audio_flash_page(b"\xb8" + b"BBBB" * 5),
    )
    msg_c = _make_storage_buffer_msg(
        page_index=12,
        flash_page=_make_audio_flash_page(
            b"\xb8" + b"CCCC" * 5, did_stop_recording=True),
    )
    bin_path = tmp_path / "capture.bin"
    n = write_batch_ingest_request(
        messages=[msg_a, msg_b, msg_c],
        ble_identifier="AA:BB:CC:DD:EE:FF",
        path=bin_path,
    )
    assert bin_path.exists()
    assert bin_path.stat().st_size == n
    # Round trip the messages
    recovered = list(iter_messages_from_bin(bin_path))
    assert recovered == [msg_a, msg_b, msg_c]
    # Metadata
    meta = read_batch_ingest_request_metadata(bin_path)
    assert meta["messages"] == 3
    assert meta["ble_identifier"] == "AA:BB:CC:DD:EE:FF"
    assert meta["size_bytes"] == n


def test_batch_ingest_request_empty(tmp_path: Path):
    bin_path = tmp_path / "empty.bin"
    write_batch_ingest_request(
        messages=[], ble_identifier="X", path=bin_path)
    assert list(iter_messages_from_bin(bin_path)) == []
    meta = read_batch_ingest_request_metadata(bin_path)
    assert meta["messages"] == 0


def test_iter_audio_chunks_from_messages_filters_ingest_type():
    """Only chunks with the requested ingest_type yield AudioChunks."""
    msg_real_time = _make_storage_buffer_msg(
        page_index=1, flash_page=_make_audio_flash_page(b"\xb8AAAA"),
        ingest_type=1,
    )
    msg_batch = _make_storage_buffer_msg(
        page_index=2, flash_page=_make_audio_flash_page(b"\xb8BBBB"),
        ingest_type=2,
    )
    msg_command = _make_storage_buffer_msg(
        page_index=3, flash_page=_make_audio_flash_page(b"\xb8CCCC"),
        ingest_type=3,
    )

    # Default accept_types = (1, 2)
    chunks = list(iter_audio_chunks_from_messages(
        [msg_real_time, msg_batch, msg_command]))
    assert len(chunks) == 2
    assert chunks[0].ingest_type == 1
    assert chunks[1].ingest_type == 2

    # Include logs
    chunks = list(iter_audio_chunks_from_messages(
        [msg_real_time, msg_batch, msg_command],
        accept_types=(1, 2, 3)))
    assert len(chunks) == 3


# ---------------------------------------------------------------------------
# Heuristic-C splitter

class _MockWriter:
    def __init__(self, idx: int):
        self.idx = idx
        self.added: list = []
        self.closed = False
        self.opened_with_start_marker = False
        self.closed_with_stop_marker = False

    def add(self, chunk):
        self.added.append(chunk)

    def close(self):
        self.closed = True


def _drive_splitter(chunks: list) -> dict[int, _MockWriter]:
    writers: dict[int, _MockWriter] = {}

    def open_writer(idx, _chunk):
        w = _MockWriter(idx)
        writers[idx] = w
        return w

    splitter = RecordingSplitter(open_writer)
    for c in chunks:
        splitter.feed(c)
    splitter.finalize()
    return writers


def _make_audio_chunk(*, has_audio: bool = True,
                       did_start_recording: bool = False,
                       did_stop_recording: bool = False):
    """Build an `AudioChunk` directly (skipping the wire layer)."""
    from pendant_client.flash_page import AudioPayload, Chunk
    from pendant_client.session import AudioChunk

    audio = None
    if has_audio or did_start_recording or did_stop_recording:
        audio = AudioPayload(
            opus_packets=b"\xb8AAAA" if has_audio else b"",
            did_start_recording=did_start_recording,
            did_stop_recording=did_stop_recording,
        )
    return AudioChunk(
        chunk=Chunk(audio=audio),
        page_index=0, ingest_type=2,
    )


def test_splitter_audio_only_no_markers():
    """No start/stop markers → one writer collects everything, never
    flagged with markers, closed only on finalize."""
    chunks = [
        _make_audio_chunk(),
        _make_audio_chunk(),
        _make_audio_chunk(),
    ]
    ws = _drive_splitter(chunks)
    assert len(ws) == 1
    w = ws[0]
    assert len(w.added) == 3
    assert not w.opened_with_start_marker
    assert not w.closed_with_stop_marker
    assert w.closed


def test_splitter_audio_then_stop_marker():
    """Tail recording (no start marker, ends in did_stop_recording).
    One writer, opened without marker, closed with stop marker."""
    chunks = [
        _make_audio_chunk(),
        _make_audio_chunk(),
        # Marker-only stop (no audio bytes).
        _make_audio_chunk(has_audio=False, did_stop_recording=True),
    ]
    ws = _drive_splitter(chunks)
    assert len(ws) == 1
    w = ws[0]
    assert len(w.added) == 2
    assert not w.opened_with_start_marker
    assert w.closed_with_stop_marker


def test_splitter_preroll_merges_into_explicit_recording():
    """The core heuristic. Pre-roll chunks (no markers) followed by a
    did_start_recording must merge into ONE writer. The writer's
    opened_with_start_marker should be promoted to True after merge."""
    chunks = [
        # 4 pre-roll chunks (typical VAD lookback)
        _make_audio_chunk(),
        _make_audio_chunk(),
        _make_audio_chunk(),
        _make_audio_chunk(),
        # Marker-only start chunk (no audio bytes — typical firmware
        # pattern observed in the wild).
        _make_audio_chunk(has_audio=False, did_start_recording=True),
        # Audio body
        _make_audio_chunk(),
        _make_audio_chunk(),
        _make_audio_chunk(),
        # Marker-only stop chunk
        _make_audio_chunk(has_audio=False, did_stop_recording=True),
    ]
    ws = _drive_splitter(chunks)
    assert len(ws) == 1, f"expected 1 writer (merge), got {len(ws)}"
    w = ws[0]
    assert len(w.added) == 7  # 4 pre-roll + 3 body
    assert w.opened_with_start_marker  # promoted by start-marker absorption
    assert w.closed_with_stop_marker


def test_splitter_two_recordings_back_to_back():
    """A complete recording followed by a new one (with its own
    pre-roll). Should produce 2 writers, both bracketed cleanly."""
    chunks = [
        # rec 0: stand-alone, audio + stop
        _make_audio_chunk(),
        _make_audio_chunk(),
        _make_audio_chunk(has_audio=False, did_stop_recording=True),
        # rec 1: pre-roll + start + body + stop
        _make_audio_chunk(),
        _make_audio_chunk(),
        _make_audio_chunk(has_audio=False, did_start_recording=True),
        _make_audio_chunk(),
        _make_audio_chunk(has_audio=False, did_stop_recording=True),
    ]
    ws = _drive_splitter(chunks)
    assert len(ws) == 2
    assert len(ws[0].added) == 2
    assert ws[0].closed_with_stop_marker
    assert not ws[0].opened_with_start_marker
    assert len(ws[1].added) == 3       # 2 pre-roll + 1 body
    assert ws[1].opened_with_start_marker
    assert ws[1].closed_with_stop_marker


def test_splitter_explicit_recording_with_no_preroll():
    """did_start_recording arrives before any audio. A new writer
    should open with the start-marker flag set, even with no pre-roll
    to absorb."""
    chunks = [
        _make_audio_chunk(has_audio=False, did_start_recording=True),
        _make_audio_chunk(),
        _make_audio_chunk(),
        _make_audio_chunk(has_audio=False, did_stop_recording=True),
    ]
    ws = _drive_splitter(chunks)
    assert len(ws) == 1
    w = ws[0]
    assert len(w.added) == 2
    assert w.opened_with_start_marker
    assert w.closed_with_stop_marker


def test_splitter_consecutive_explicit_starts_split_into_two():
    """If a writer is already explicit-started and a new
    did_start_recording arrives, it's a NEW recording — close the
    prior, open a new one."""
    chunks = [
        _make_audio_chunk(has_audio=False, did_start_recording=True),
        _make_audio_chunk(),
        # Second start without an intervening stop (rare but valid)
        _make_audio_chunk(has_audio=False, did_start_recording=True),
        _make_audio_chunk(),
        _make_audio_chunk(has_audio=False, did_stop_recording=True),
    ]
    ws = _drive_splitter(chunks)
    assert len(ws) == 2
    assert len(ws[0].added) == 1
    assert ws[0].opened_with_start_marker
    assert not ws[0].closed_with_stop_marker  # closed by next start, not stop
    assert ws[0].closed
    assert len(ws[1].added) == 1
    assert ws[1].opened_with_start_marker
    assert ws[1].closed_with_stop_marker


def test_splitter_unfinished_recording_finalized():
    """Stream ends mid-recording (no stop marker). Writer should still
    close cleanly on finalize, just without `closed_with_stop_marker`."""
    chunks = [
        _make_audio_chunk(),
        _make_audio_chunk(),
        # No stop marker — sync was Ctrl+C'd or idle-timed-out
    ]
    ws = _drive_splitter(chunks)
    assert len(ws) == 1
    w = ws[0]
    assert w.closed
    assert not w.closed_with_stop_marker
