"""Parse `StorageBufferMsg.flash_page` bytes.

The `flash_page` value on the wire is a serialised
`FlashPageProto.FlashPage` message:

    message FlashPage {
      uint64 absolute_timestamp_ms = 1;
      uint64 boot_uptime_ms        = 2;
      repeated Chunk chunks        = 3;
    }

Each `Chunk` is a multi-purpose record where exactly one of the
optional fields is populated:

    message Chunk {
      int32 time_offset_ms                    = 1;
      PendantAudioData audio_data             = 2;
      ButtonStatus     button_status          = 3;
      WifiStatus       wifi_status            = 4;
      BleStatus        ble_status             = 5;
      BatteryStatus    battery_status         = 6;
      UsbStatus        usb_status             = 7;
      ImuData          imu_data               = 8;
      RecordingStatus  recording_status       = 9;
      LogOutput        log_output             = 10;
      RuntimeStatus    runtime_status         = 11;
      StorageStatus    storage_status         = 12;
      CpuStatus        cpu_status             = 13;
    }

Recording boundaries are signalled by the inline booleans
`audio_data.did_start_recording` (field 8) and
`audio_data.did_stop_recording` (field 9). `StorageBufferMsg.session`
on the envelope is *not* a recording boundary; the official Limitless
app's BLEFragmentGroup records the largest session ID per group as
metadata, but never splits on it (decompiled `BLEFragmentGroup`
class `f.c` / `BLEFragmentGrouper` class `d.h`).

Schema reconstructed from the decompiled Android app:
  C:\\preservation\\decompiled\\base\\sources\\ai\\limitless\\pendant\\flash_page\\
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .proto import flash_page_pb2


# ---------------------------------------------------------------------------
# Lightweight wrappers around the generated proto classes. We expose the
# fields callers actually need rather than the raw nanopb-style proto
# objects, so the rest of the codebase doesn't depend on protobuf
# internals.

@dataclass
class EncryptedAudio:
    """Encrypted Opus payload, populated for chunks where the pendant
    has an active server pubkey (AES-GCM-128 per chunk)."""
    nonce: bytes = b""                # 12 bytes
    ciphertext: bytes = b""
    authentication_tag: bytes = b""   # 16 bytes
    packet_offsets: tuple[int, ...] = ()


@dataclass
class AudioPayload:
    """The `audio_data` submessage of a Chunk.

    `opus_packets` is the playable Opus bytes after any decryption —
    callers should always use this rather than reaching for the raw
    fields. It's `codec_beamforming_data` for the standard
    configuration and `codec_manual_beamforming_data` when manual
    beamforming is active.

    `did_start_recording` / `did_stop_recording` are the canonical
    recording-boundary markers; on these we split output files.
    """
    opus_packets: bytes = b""
    did_start_recording: bool = False
    did_stop_recording: bool = False
    codec_type: int = 0
    num_frames: int = 0
    degree_of_arrival: int = 0
    # Raw fields, in case the caller wants them (debug PCM dumps etc.).
    pcm_omni_mic_data: bytes = b""
    pcm_directional_mic_data: bytes = b""
    pcm_beamforming_data: bytes = b""
    pcm_manual_beamforming_data: bytes = b""
    raw_codec_beamforming: bytes = b""
    raw_codec_manual_beamforming: bytes = b""
    encrypted: Optional[EncryptedAudio] = None

    @property
    def has_audio(self) -> bool:
        return bool(self.opus_packets) or self.encrypted is not None


@dataclass
class ButtonEvent:
    """Pendant button press, surfaced via `Chunk.button_status`.

    The Button enum values (`shared.proto`):
      0 BUTTON_NOT_PRESSED
      1 BUTTON_SHORT_PRESS               (per the Limitless docs:
                                          "Star a moment" — marks a
                                          conversation moment for
                                          later review in-app)
      2 BUTTON_LONG_PRESS
      3 BUTTON_DOUBLE_PRESS
      4 BUTTON_SHORT_AND_SUPER_LONG_PRESS  (factory-reset gesture)
    """
    button_event: int = 0
    physical_button: bool = False
    num_short_presses_since_boot: int = 0
    num_long_presses_since_boot: int = 0
    num_double_presses_since_boot: int = 0
    button_event_absolute_timestamp_ms: int = 0
    short_press_duration_ms: int = 0
    long_press_duration_ms: int = 0


@dataclass
class RecordingState:
    """Pendant recording-state snapshot, via `Chunk.recording_status`.

    Enum values (`shared.proto`):
      recording_state:  0 NOT_RECORDING / 1 RECORDING / 2 LISTENING
      recording_source: 0 BUTTON / 1 AMBIENT_SOUND (VAD)
    """
    recording_state: int = 0
    recording_source: int = 0
    vad_level: int = 0


@dataclass
class StorageSessionMarker:
    """Storage-session boundary markers, via `Chunk.storage_status`.

    These are the canonical recording-boundary signals the official
    Limitless app uses (`f.a.a()` factory, formerly `m43390a`). For
    each FlashPage, the app scans every Chunk; if any chunk has
    `storage_status.did_stop_storage_session = true`, the page is
    flagged `isLastInSession` and an upload is forced.

    These flags fire on every recording, including VAD-initiated ones —
    unlike `audio_data.did_*_recording` which only fires on button
    events.
    """
    did_start_storage_session: bool = False
    did_stop_storage_session: bool = False


@dataclass
class Chunk:
    """One Chunk pulled out of a FlashPage.

    Most chunks are audio — populated `audio` plus an integer
    `time_offset_ms`. A subset are *status-only* chunks (button
    presses, recording-state changes, storage-session markers, etc.);
    these have no audio and are surfaced via the `*_status` fields.
    """
    time_offset_ms: Optional[int] = None
    audio: Optional[AudioPayload] = None
    button: Optional[ButtonEvent] = None
    recording: Optional[RecordingState] = None
    storage: Optional[StorageSessionMarker] = None
    # Raw bytes for the remaining status submessages — kept opaque so
    # downstream code can decide whether to parse them.
    raw_status_fields: dict[int, bytes] = field(default_factory=dict)

    @property
    def has_audio(self) -> bool:
        return self.audio is not None and self.audio.has_audio

    @property
    def is_storage_session_start(self) -> bool:
        return self.storage is not None and self.storage.did_start_storage_session

    @property
    def is_storage_session_stop(self) -> bool:
        return self.storage is not None and self.storage.did_stop_storage_session

    # Button-only recording markers (set when the user starts/stops via
    # the side button). For VAD-driven recordings these stay False —
    # use `is_storage_session_*` for the canonical boundary.
    @property
    def is_recording_start(self) -> bool:
        return self.audio is not None and self.audio.did_start_recording

    @property
    def is_recording_stop(self) -> bool:
        return self.audio is not None and self.audio.did_stop_recording


@dataclass
class FlashPage:
    """A parsed FlashPage: page-level metadata plus its Chunks."""
    absolute_timestamp_ms: int = 0
    boot_uptime_ms: int = 0
    chunks: list[Chunk] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Parser

def parse_flash_page(flash_page_bytes: bytes) -> FlashPage:
    """Parse a `StorageBufferMsg.flash_page` payload."""
    msg = flash_page_pb2.FlashPage()
    msg.ParseFromString(flash_page_bytes)
    page = FlashPage(
        absolute_timestamp_ms=msg.absolute_timestamp_ms,
        boot_uptime_ms=msg.boot_uptime_ms,
    )
    for proto_chunk in msg.chunks:
        page.chunks.append(_convert_chunk(proto_chunk))
    return page


def _convert_chunk(proto_chunk: "flash_page_pb2.Chunk") -> Chunk:
    chunk = Chunk(time_offset_ms=proto_chunk.time_offset_ms)
    if proto_chunk.HasField("audio_data"):
        chunk.audio = _convert_audio(proto_chunk.audio_data)
    if proto_chunk.HasField("button_status"):
        bs = proto_chunk.button_status
        chunk.button = ButtonEvent(
            button_event=bs.button_event,
            physical_button=bs.physical_button,
            num_short_presses_since_boot=bs.num_short_presses_since_boot,
            num_long_presses_since_boot=bs.num_long_presses_since_boot,
            num_double_presses_since_boot=bs.num_double_presses_since_boot,
            button_event_absolute_timestamp_ms=bs.button_event_absolute_timestamp_ms,
            short_press_duration_ms=bs.short_press_duration_ms,
            long_press_duration_ms=bs.long_press_duration_ms,
        )
    if proto_chunk.HasField("recording_status"):
        rs = proto_chunk.recording_status
        chunk.recording = RecordingState(
            recording_state=rs.recording_state,
            recording_source=rs.recording_source,
            vad_level=rs.vad_level,
        )
    if proto_chunk.HasField("storage_status"):
        ss = proto_chunk.storage_status
        chunk.storage = StorageSessionMarker(
            did_start_storage_session=ss.did_start_storage_session,
            did_stop_storage_session=ss.did_stop_storage_session,
        )
    # The remaining status submessages we keep as raw serialised bytes so
    # downstream code (e.g., a debug events sidecar) can parse them on
    # demand without us baking in every shape today.
    for field_name, raw_field_number in _RAW_STATUS_FIELDS:
        if proto_chunk.HasField(field_name):
            sub = getattr(proto_chunk, field_name)
            chunk.raw_status_fields[raw_field_number] = sub.SerializeToString()
    return chunk


_RAW_STATUS_FIELDS: tuple[tuple[str, int], ...] = (
    ("wifi_status", 4),
    ("ble_status", 5),
    ("battery_status", 6),
    ("usb_status", 7),
    ("imu_data", 8),
    ("log_output", 10),
    ("runtime_status", 11),
    ("cpu_status", 13),
)


def _convert_audio(proto_audio: "flash_page_pb2.PendantAudioData") -> AudioPayload:
    # Pick the playable Opus bytes. The two non-encrypted candidates are
    # `codec_beamforming_data` (standard config) and
    # `codec_manual_beamforming_data` (when manual beamforming is on).
    opus = bytes(proto_audio.codec_beamforming_data) \
        or bytes(proto_audio.codec_manual_beamforming_data)
    encrypted: Optional[EncryptedAudio] = None
    if proto_audio.HasField("encrypted_codec_beamforming_data"):
        eb = proto_audio.encrypted_codec_beamforming_data
        encrypted = EncryptedAudio(
            nonce=bytes(eb.nonce),
            ciphertext=bytes(eb.ciphertext),
            authentication_tag=bytes(eb.authentication_tag),
            packet_offsets=tuple(eb.packet_offsets),
        )
    return AudioPayload(
        opus_packets=opus,
        did_start_recording=proto_audio.did_start_recording,
        did_stop_recording=proto_audio.did_stop_recording,
        codec_type=proto_audio.codec_type,
        num_frames=proto_audio.num_frames,
        degree_of_arrival=proto_audio.degree_of_arrival,
        pcm_omni_mic_data=bytes(proto_audio.pcm_omni_mic_data),
        pcm_directional_mic_data=bytes(proto_audio.pcm_directional_mic_data),
        pcm_beamforming_data=bytes(proto_audio.pcm_beamforming_data),
        pcm_manual_beamforming_data=bytes(proto_audio.pcm_manual_beamforming_data),
        raw_codec_beamforming=bytes(proto_audio.codec_beamforming_data),
        raw_codec_manual_beamforming=bytes(proto_audio.codec_manual_beamforming_data),
        encrypted=encrypted,
    )
