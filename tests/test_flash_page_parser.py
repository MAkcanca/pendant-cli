"""Verify parse_flash_page against the schema reconstructed from the
official Limitless app:

    message FlashPage {
      uint64 absolute_timestamp_ms = 1;
      uint64 boot_uptime_ms        = 2;
      repeated Chunk chunks        = 3;
    }

    message Chunk {
      int32 time_offset_ms                    = 1;
      PendantAudioData audio_data             = 2;
      ButtonStatus     button_status          = 3;
      ...
      RecordingStatus  recording_status       = 9;
      ...
    }

    message PendantAudioData {
      bytes  codec_beamforming_data    = 4;   // The Opus packet(s).
      bool   did_start_recording       = 8;
      bool   did_stop_recording        = 9;
      EncryptedBytes encrypted_codec_beamforming_data = 13;
    }
"""
from __future__ import annotations

from pendant_client.flash_page import (
    AudioPayload,
    ButtonEvent,
    Chunk,
    FlashPage,
    parse_flash_page,
)
from pendant_client.proto import flash_page_pb2, shared_pb2


def _build_audio_only_page(*, opus: bytes,
                           did_start_recording: bool = False,
                           did_stop_recording: bool = False,
                           absolute_timestamp_ms: int = 0,
                           boot_uptime_ms: int = 0,
                           time_offset_ms: int = 0) -> bytes:
    page = flash_page_pb2.FlashPage(
        absolute_timestamp_ms=absolute_timestamp_ms,
        boot_uptime_ms=boot_uptime_ms,
    )
    chunk = page.chunks.add()
    chunk.time_offset_ms = time_offset_ms
    chunk.audio_data.codec_beamforming_data = opus
    chunk.audio_data.did_start_recording = did_start_recording
    chunk.audio_data.did_stop_recording = did_stop_recording
    return page.SerializeToString()


# --- core schema tests -----------------------------------------------------

def test_empty_page():
    fp = parse_flash_page(b"")
    assert isinstance(fp, FlashPage)
    assert fp.chunks == []
    assert fp.absolute_timestamp_ms == 0
    assert fp.boot_uptime_ms == 0


def test_audio_chunk_decoded():
    opus = b"\xb8" + bytes(range(40))
    raw = _build_audio_only_page(
        opus=opus,
        absolute_timestamp_ms=1_700_000_000_000,
        boot_uptime_ms=12_345,
        time_offset_ms=200,
    )
    fp = parse_flash_page(raw)
    assert fp.absolute_timestamp_ms == 1_700_000_000_000
    assert fp.boot_uptime_ms == 12_345
    assert len(fp.chunks) == 1
    chunk = fp.chunks[0]
    assert chunk.time_offset_ms == 200
    assert chunk.audio is not None
    assert chunk.audio.opus_packets == opus
    assert chunk.audio.opus_packets[0] == 0xB8
    assert chunk.has_audio
    assert not chunk.is_recording_start
    assert not chunk.is_recording_stop
    assert chunk.audio.encrypted is None


def test_recording_start_flag():
    opus = b"\xb8" + b"\x01" * 40
    raw = _build_audio_only_page(opus=opus, did_start_recording=True)
    fp = parse_flash_page(raw)
    chunk = fp.chunks[0]
    assert chunk.is_recording_start
    assert not chunk.is_recording_stop


def test_recording_stop_flag():
    opus = b"\xb8" + b"\x02" * 40
    raw = _build_audio_only_page(opus=opus, did_stop_recording=True)
    fp = parse_flash_page(raw)
    chunk = fp.chunks[0]
    assert not chunk.is_recording_start
    assert chunk.is_recording_stop


def test_multiple_chunks_per_page_with_boundaries():
    page = flash_page_pb2.FlashPage(
        absolute_timestamp_ms=42, boot_uptime_ms=1000,
    )
    for i, (start, stop) in enumerate([
        (True,  False),   # opening chunk
        (False, False),
        (False, False),
        (False, True),    # closing chunk
    ]):
        c = page.chunks.add()
        c.time_offset_ms = i * 20
        c.audio_data.codec_beamforming_data = b"\xb8" + bytes([i] * 30)
        c.audio_data.did_start_recording = start
        c.audio_data.did_stop_recording = stop
    fp = parse_flash_page(page.SerializeToString())
    assert len(fp.chunks) == 4
    assert fp.chunks[0].is_recording_start
    assert fp.chunks[-1].is_recording_stop
    assert all(c.has_audio for c in fp.chunks)


def test_button_status_chunk_no_audio():
    page = flash_page_pb2.FlashPage()
    c = page.chunks.add()
    c.time_offset_ms = 12
    c.button_status.button_event = shared_pb2.BUTTON_SHORT_PRESS  # "Star a moment"
    c.button_status.physical_button = True
    c.button_status.num_short_presses_since_boot = 3
    c.button_status.short_press_duration_ms = 100
    fp = parse_flash_page(page.SerializeToString())
    assert len(fp.chunks) == 1
    chunk = fp.chunks[0]
    assert not chunk.has_audio
    assert chunk.audio is None
    assert chunk.button is not None
    assert chunk.button.button_event == shared_pb2.BUTTON_SHORT_PRESS
    assert chunk.button.physical_button is True
    assert chunk.button.num_short_presses_since_boot == 3
    assert chunk.button.short_press_duration_ms == 100


def test_recording_status_chunk_no_audio():
    page = flash_page_pb2.FlashPage()
    c = page.chunks.add()
    c.recording_status.recording_state = shared_pb2.RECORDING
    c.recording_status.recording_source = shared_pb2.AMBIENT_SOUND
    c.recording_status.vad_level = 0x42
    fp = parse_flash_page(page.SerializeToString())
    chunk = fp.chunks[0]
    assert not chunk.has_audio
    assert chunk.recording is not None
    assert chunk.recording.recording_state == shared_pb2.RECORDING
    assert chunk.recording.recording_source == shared_pb2.AMBIENT_SOUND
    assert chunk.recording.vad_level == 0x42


def test_other_status_chunk_kept_as_raw_bytes():
    """Status submessages we haven't yet modeled (battery, etc.) are
    surfaced via raw_status_fields so the events sidecar can still log
    them."""
    page = flash_page_pb2.FlashPage()
    c = page.chunks.add()
    c.battery_status.voltage = 3700
    c.battery_status.soc = 85
    fp = parse_flash_page(page.SerializeToString())
    chunk = fp.chunks[0]
    assert not chunk.has_audio
    assert 6 in chunk.raw_status_fields  # field 6 = battery_status
    # And the bytes round-trip back to a BatteryStatus.
    bs = shared_pb2.BatteryStatus()
    bs.ParseFromString(chunk.raw_status_fields[6])
    assert bs.voltage == 3700
    assert bs.soc == 85


def test_storage_session_marker():
    """StorageStatus.did_*_storage_session is the canonical recording-
    boundary signal. Fires on every recording (button or VAD)."""
    # Stop marker on its own (no audio in the chunk).
    page = flash_page_pb2.FlashPage()
    c = page.chunks.add()
    c.storage_status.did_stop_storage_session = True
    fp = parse_flash_page(page.SerializeToString())
    chunk = fp.chunks[0]
    assert not chunk.has_audio
    assert chunk.is_storage_session_stop
    assert not chunk.is_storage_session_start
    assert chunk.storage is not None
    assert chunk.storage.did_stop_storage_session is True
    assert chunk.storage.did_start_storage_session is False
    # And it's NOT routed through raw_status_fields — we parse it.
    assert 12 not in chunk.raw_status_fields

    # Start marker.
    page2 = flash_page_pb2.FlashPage()
    c2 = page2.chunks.add()
    c2.storage_status.did_start_storage_session = True
    fp2 = parse_flash_page(page2.SerializeToString())
    chunk2 = fp2.chunks[0]
    assert chunk2.is_storage_session_start
    assert not chunk2.is_storage_session_stop


def test_audio_data_did_recording_flags_independent_of_storage_session():
    """audio_data.did_*_recording fires only on button-driven
    recordings; it must NOT be confused with the storage-session
    boundary which is what the app uses for splitting."""
    page = flash_page_pb2.FlashPage()
    c = page.chunks.add()
    c.audio_data.codec_beamforming_data = b"\xb8" + b"\x00" * 30
    c.audio_data.did_stop_recording = True
    fp = parse_flash_page(page.SerializeToString())
    chunk = fp.chunks[0]
    assert chunk.is_recording_stop          # audio-data flag
    assert not chunk.is_storage_session_stop  # storage flag — separate


def test_encrypted_audio_chunk():
    page = flash_page_pb2.FlashPage()
    c = page.chunks.add()
    eb = c.audio_data.encrypted_codec_beamforming_data
    eb.nonce = b"\x11" * 12
    eb.ciphertext = b"\x33" * 100
    eb.authentication_tag = b"\x22" * 16
    eb.packet_offsets.extend([0, 50])
    fp = parse_flash_page(page.SerializeToString())
    chunk = fp.chunks[0]
    assert chunk.audio is not None
    enc = chunk.audio.encrypted
    assert enc is not None
    assert enc.nonce == b"\x11" * 12
    assert enc.ciphertext == b"\x33" * 100
    assert enc.authentication_tag == b"\x22" * 16
    assert enc.packet_offsets == (0, 50)
    # Plaintext opus_packets is empty until decrypt_audio() runs.
    assert chunk.audio.opus_packets == b""


def test_real_device_byte_pattern():
    """Reproduces the byte pattern observed from a live pendant: a page
    containing a single audio chunk (`time_offset_ms` = -1 — int32
    sentinel, NOT a session marker), with a 42-byte Opus packet starting
    at the 0xB8 TOC."""
    opus = bytes.fromhex(
        "b815b3af613f27e399a01466d71c24caec10689d"
        "3c0878d87ed1d15ab6548538d34c4a33da0670b9a647"
    )
    assert len(opus) == 42 and opus[0] == 0xB8
    page = flash_page_pb2.FlashPage(
        absolute_timestamp_ms=1341, boot_uptime_ms=1341,
    )
    c = page.chunks.add()
    c.time_offset_ms = -1
    c.audio_data.codec_beamforming_data = opus
    fp = parse_flash_page(page.SerializeToString())
    assert fp.absolute_timestamp_ms == 1341
    assert fp.boot_uptime_ms == 1341
    chunk = fp.chunks[0]
    assert chunk.time_offset_ms == -1   # NOT a session marker — just signed -1
    assert chunk.has_audio
    assert chunk.audio.opus_packets == opus
