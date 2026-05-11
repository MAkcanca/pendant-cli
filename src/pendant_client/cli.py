"""Command-line interface.

    pendant scan
    pendant info <addr>
    pendant sync <addr> -o today.opus
        (capture .bin AND decode it into per-recording .opus files)
    pendant capture <addr> -o today.bin
        (wire-fidelity capture only, matching the official app's
         BatchIngestRequest persistence layer)
    pendant decode today.bin -o today.opus
        (post-process a .bin into per-recording .opus files using
         the pre-roll-merging heuristic — see `decode.py`)
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import struct
import sys
from pathlib import Path
from typing import Optional

import click
from cryptography.hazmat.primitives import serialization

from . import __version__
from .ble import PendantNotPairedError
from .crypto import (
    AudioKeySet,
    generate_keyset,
    keyset_from_existing,
)
from .decode import (
    RecordingSplitter,
    iter_audio_chunks_from_messages,
    iter_messages_from_bin,
    read_batch_ingest_request_metadata,
    write_batch_ingest_request,
)
from .session import AudioChunk, PendantSession, find_pendant


def _user_friendly(coro_factory):
    """Run a coroutine, intercepting PendantNotPairedError into a clean
    Click error rather than a stack trace."""
    try:
        return asyncio.run(coro_factory())
    except PendantNotPairedError as e:
        click.echo(f"\nERROR: {e}", err=True)
        sys.exit(2)


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


# ---------------------------------------------------------------------------
# Opus container helpers
#
# Two outputs supported:
#  * .opus / .ogg: a streamable Ogg Opus file (per RFC 7845)
#  * .pcm: raw concatenated Opus packets (debug; not directly playable)
#
# Pendant emits 20 ms 16-kHz mono Opus packets. We write them into Ogg
# pages each containing a single Opus packet so any Opus player works.

OGG_CAPTURE_PATTERN = b"OggS"


def _crc32_ogg(data: bytes,
                _table: list[int] = []) -> int:  # noqa: B008  (initialised lazily)
    if not _table:
        # Standard CRC-32 polynomial used by Ogg (0x04c11db7), MSB-first,
        # init=0, no XOR, not reflected.
        for i in range(256):
            r = i << 24
            for _ in range(8):
                r = ((r << 1) ^ 0x04C11DB7) if (r & 0x80000000) else (r << 1)
            _table.append(r & 0xFFFFFFFF)
    crc = 0
    for b in data:
        crc = (((crc << 8) & 0xFFFFFFFF) ^ _table[((crc >> 24) ^ b) & 0xFF])
    return crc & 0xFFFFFFFF


class OggOpusWriter:
    """Minimal writer producing a valid .opus file from emitted Opus packets."""

    SAMPLE_RATE = 16_000  # pendant: 16 kHz
    SAMPLES_PER_FRAME = 320  # 20 ms @ 16 kHz

    def __init__(self, path: Path):
        self._fp = path.open("wb")
        self._serial = struct.unpack("<I", os.urandom(4))[0] | 1
        self._page_seq = 0
        self._granule = 0
        self._write_headers()

    def _write_page(self, packet: bytes,
                    *, is_first: bool = False, is_last: bool = False,
                    granule: int) -> None:
        # Segment table: each segment is at most 255 bytes; final segment
        # < 255 indicates packet end.
        n = len(packet)
        segs: list[int] = []
        while n >= 255:
            segs.append(255)
            n -= 255
        segs.append(n)

        header_type = 0
        if is_first:
            header_type |= 0x02  # bos
        if is_last:
            header_type |= 0x04  # eos

        head = bytearray()
        head += OGG_CAPTURE_PATTERN
        head += b"\x00"                                      # version
        head += struct.pack("<B", header_type)
        head += struct.pack("<q", granule)
        head += struct.pack("<I", self._serial)
        head += struct.pack("<I", self._page_seq)
        head += struct.pack("<I", 0)                         # crc placeholder
        head += struct.pack("<B", len(segs))
        head += bytes(segs)
        body = head + packet
        crc = _crc32_ogg(body)
        body = body[:22] + struct.pack("<I", crc) + body[26:]
        self._fp.write(body)
        self._page_seq += 1

    def _write_headers(self) -> None:
        # OpusHead (RFC 7845 §5.1)
        opus_head = (
            b"OpusHead"
            + b"\x01"                         # version
            + b"\x01"                         # channels = 1
            + struct.pack("<H", 0)            # pre-skip
            + struct.pack("<I", self.SAMPLE_RATE)  # input sample rate
            + struct.pack("<h", 0)            # output gain Q7.8
            + b"\x00"                         # mapping family
        )
        self._write_page(opus_head, is_first=True, granule=0)

        # OpusTags (RFC 7845 §5.2)
        vendor = b"limitless-pendant-client"
        opus_tags = (
            b"OpusTags"
            + struct.pack("<I", len(vendor)) + vendor
            + struct.pack("<I", 0)  # 0 user comments
        )
        self._write_page(opus_tags, granule=0)

    def add_packet(self, packet: bytes) -> None:
        if not packet:
            return
        self._granule += self.SAMPLES_PER_FRAME
        self._write_page(packet, granule=self._granule)

    def close(self) -> None:
        # Write a final empty page with eos flag set.
        try:
            self._fp.flush()
            self._fp.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Per-recording output writer
#
# Recording boundaries are the canonical pendant flags inside each audio
# chunk's `audio_data` submessage (FlashPageProto.PendantAudioData):
#   - did_start_recording = true  → first chunk of a new recording
#   - did_stop_recording  = true  → last chunk of the current recording
#
# We open a new file when did_start_recording fires (or implicitly on
# the first audio chunk we see) and close after did_stop_recording.
# Status-only chunks (button presses, recording_status, battery, etc.)
# are NOT written to the audio stream — they're captured in a sidecar
# events JSON.
#
# `StorageBufferMsg.session` is preserved as metadata in the manifest
# but is NOT a split key. Per `p643d/C10455e.java:229` the official
# Limitless app splits on `isLastInSession && hasAudioData`, where
# `isLastInSession` is `audio_data.did_stop_recording`.

class _RecordingWriter:
    """Holds output state for one pendant recording.

    A "recording" is the span between an audio chunk with
    `audio_data.did_start_recording = True` and an audio chunk with
    `audio_data.did_stop_recording = True`. This is the boundary the
    Limitless app uses (`p643d/C10455e.java:229` —
    `isLastInSession && hasAudioData`).
    """

    def __init__(self, base: Path, recording_index: int):
        self.recording_index = recording_index
        suffix = base.suffix or ".opus"
        stem = base.stem
        self.path: Path = base.with_name(
            f"{stem}_rec{recording_index:04d}{suffix}")
        self.writer: Optional[OggOpusWriter] = None
        self.raw_fp = None
        if self.path.suffix.lower() in (".opus", ".ogg"):
            self.writer = OggOpusWriter(self.path)
        else:
            self.raw_fp = self.path.open("wb")

        self.chunk_count: int = 0
        self.opened_with_start_marker: bool = False
        self.closed_with_stop_marker: bool = False
        self.first_absolute_timestamp_ms: int = 0
        self.last_absolute_timestamp_ms: int = 0
        self.first_received_unix_ms: int = 0
        self.last_received_unix_ms: int = 0
        self.first_page_index: Optional[int] = None
        self.last_page_index: Optional[int] = None
        self.ingest_types: set[int] = set()
        self.storage_session_ids: set[int] = set()
        self.runs: set[int] = set()
        self.flash_page_errors: int = 0

    def add(self, chunk: AudioChunk) -> None:
        opus = chunk.opus
        if not opus:
            return
        if self.writer is not None:
            self.writer.add_packet(opus)
        elif self.raw_fp is not None:
            self.raw_fp.write(struct.pack("<I", len(opus)))
            self.raw_fp.write(opus)
        self.chunk_count += 1
        if self.first_absolute_timestamp_ms == 0:
            self.first_absolute_timestamp_ms = chunk.absolute_chunk_timestamp_ms
            self.first_received_unix_ms = chunk.received_at_unix_ms
            self.first_page_index = chunk.page_index
            if chunk.is_recording_start:
                self.opened_with_start_marker = True
        self.last_absolute_timestamp_ms = chunk.absolute_chunk_timestamp_ms
        self.last_received_unix_ms = chunk.received_at_unix_ms
        self.last_page_index = chunk.page_index
        self.ingest_types.add(chunk.ingest_type)
        self.storage_session_ids.add(chunk.storage_session)
        self.runs.add(chunk.run)
        if chunk.flash_page_error:
            self.flash_page_errors += 1
        if chunk.is_recording_stop:
            self.closed_with_stop_marker = True

    def close(self) -> None:
        if self.writer is not None:
            self.writer.close()
        if self.raw_fp is not None:
            try:
                self.raw_fp.flush()
                self.raw_fp.close()
            except Exception:
                pass

    def unlink_if_empty(self) -> bool:
        if self.chunk_count == 0:
            try:
                self.path.unlink(missing_ok=True)
                return True
            except OSError:
                pass
        return False


def _ingest_label(t: int) -> str:
    return {
        0: "UNSPECIFIED",
        1: "REAL_TIME",
        2: "BATCH",
        3: "COMMAND",
    }.get(t, f"UNKNOWN({t})")


_BUTTON_EVENT_LABELS = {
    0: "BUTTON_NOT_PRESSED",
    1: "BUTTON_SHORT_PRESS",   # "Star a moment" per the Limitless docs
    2: "BUTTON_LONG_PRESS",
    3: "BUTTON_DOUBLE_PRESS",
    4: "BUTTON_SHORT_AND_SUPER_LONG_PRESS",  # factory-reset gesture
}


_RECORDING_STATE_LABELS = {0: "NOT_RECORDING", 1: "RECORDING", 2: "LISTENING"}
_RECORDING_SOURCE_LABELS = {0: "BUTTON", 1: "AMBIENT_SOUND"}


def _event_for(chunk: AudioChunk) -> Optional[dict]:
    """Render the non-audio bits of a Chunk as a JSON-friendly event.
    Returns None if there's nothing to log (pure audio / empty chunk)."""
    base = {
        "page_index": chunk.page_index,
        "absolute_chunk_timestamp_ms": chunk.absolute_chunk_timestamp_ms,
        "received_at_unix_ms": chunk.received_at_unix_ms,
        "storage_session": chunk.storage_session,
        "ingest_type": _ingest_label(chunk.ingest_type),
    }
    # Canonical storage-session boundaries — what the official app
    # uses to detect end-of-recording (see f.a.a() in the decompile).
    if chunk.is_storage_session_start or chunk.is_storage_session_stop:
        if chunk.is_storage_session_stop:
            return {**base, "type": "storage_session_stop"}
        return {**base, "type": "storage_session_start"}
    # Button-event-driven recording markers (set only when the user
    # starts/stops via the side button; absent on VAD-driven recordings).
    if chunk.is_recording_start or chunk.is_recording_stop:
        if chunk.is_recording_start:
            return {**base, "type": "recording_start"}
        if chunk.is_recording_stop:
            return {**base, "type": "recording_stop"}
    if chunk.button is not None:
        be = chunk.button
        return {
            **base,
            "type": "button",
            "button_event": _BUTTON_EVENT_LABELS.get(
                be.button_event, str(be.button_event)),
            "physical_button": be.physical_button,
            "short_press_duration_ms": be.short_press_duration_ms,
            "long_press_duration_ms": be.long_press_duration_ms,
            "num_short_presses_since_boot": be.num_short_presses_since_boot,
            "num_long_presses_since_boot": be.num_long_presses_since_boot,
            "num_double_presses_since_boot": be.num_double_presses_since_boot,
            "button_event_absolute_timestamp_ms":
                be.button_event_absolute_timestamp_ms,
        }
    if chunk.recording is not None:
        rs = chunk.recording
        return {
            **base,
            "type": "recording_status",
            "recording_state": _RECORDING_STATE_LABELS.get(
                rs.recording_state, str(rs.recording_state)),
            "recording_source": _RECORDING_SOURCE_LABELS.get(
                rs.recording_source, str(rs.recording_source)),
            "vad_level": rs.vad_level,
        }
    if chunk.chunk.raw_status_fields:
        return {
            **base,
            "type": "raw_status",
            "field_numbers": sorted(chunk.chunk.raw_status_fields),
        }
    return None


def _write_sync_manifest(*, out: Path,
                         recordings: dict[int, "_RecordingWriter"],
                         events: list[dict],
                         di, address: str, total_audio_chunks: int,
                         total_event_chunks: int,
                         include_logs: bool) -> None:
    """Write the sidecar manifest + events JSON. Called from the sync's
    finally block so it runs even on Ctrl+C / connection drop."""
    finished_walltime_ms = int(dt.datetime.now().timestamp() * 1000)

    manifest_recordings = []
    for idx in sorted(recordings):
        rw = recordings[idx]
        approx_duration_ms = (
            rw.chunk_count * OggOpusWriter.SAMPLES_PER_FRAME
            * 1000 // OggOpusWriter.SAMPLE_RATE
        )
        first_iso = dt.datetime.fromtimestamp(
            (rw.first_absolute_timestamp_ms or rw.first_received_unix_ms)
            / 1000).isoformat(timespec="milliseconds")
        last_iso = dt.datetime.fromtimestamp(
            (rw.last_absolute_timestamp_ms or rw.last_received_unix_ms)
            / 1000).isoformat(timespec="milliseconds")
        manifest_recordings.append({
            "recording_index": idx,
            "file": rw.path.name,
            "chunks": rw.chunk_count,
            "approx_duration_ms": approx_duration_ms,
            "opened_with_start_marker": rw.opened_with_start_marker,
            "closed_with_stop_marker": rw.closed_with_stop_marker,
            "ingest_types": sorted(rw.ingest_types),
            "ingest_type_labels": [_ingest_label(t)
                                   for t in sorted(rw.ingest_types)],
            "storage_session_ids": sorted(rw.storage_session_ids),
            "runs": sorted(rw.runs),
            "flash_page_errors": rw.flash_page_errors,
            "first_chunk": {
                "page_index": rw.first_page_index,
                "absolute_timestamp_ms": rw.first_absolute_timestamp_ms,
                "absolute_timestamp_iso": first_iso,
                "received_at_unix_ms": rw.first_received_unix_ms,
            },
            "last_chunk": {
                "page_index": rw.last_page_index,
                "absolute_timestamp_ms": rw.last_absolute_timestamp_ms,
                "absolute_timestamp_iso": last_iso,
                "received_at_unix_ms": rw.last_received_unix_ms,
            },
        })

    manifest = {
        "device": {
            "device_id": di.device_id,
            "serial_num": di.serial_num,
            "firmware_ver": di.firmware_ver,
            "battery_percent": di.battery_percent,
            "oldest_flash_page": di.oldest_flash_page,
            "newest_flash_page": di.newest_flash_page,
        },
        "sync": {
            "address": address,
            "finished_at_unix_ms": finished_walltime_ms,
            "finished_at_iso": dt.datetime.fromtimestamp(
                finished_walltime_ms / 1000).isoformat(timespec="milliseconds"),
            "total_audio_chunks": total_audio_chunks,
            "total_event_chunks": total_event_chunks,
            "total_recordings": len(recordings),
            "include_logs": include_logs,
        },
        "recordings": manifest_recordings,
    }

    manifest_path = out.with_suffix(out.suffix + ".manifest.json") \
        if out.suffix else out.with_name(out.name + ".manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2))

    events_path = out.with_suffix(out.suffix + ".events.json") \
        if out.suffix else out.with_name(out.name + ".events.json")
    events_path.write_text(json.dumps({"events": events}, indent=2))

    click.echo(
        f"\nWrote {total_audio_chunks} audio chunk(s) into "
        f"{len(recordings)} recording(s); {total_event_chunks} event(s):")
    for mr in manifest_recordings:
        markers = (
            ("[" + ("S" if mr["opened_with_start_marker"] else "·")
             + ("E" if mr["closed_with_stop_marker"] else "·") + "]")
        )
        click.echo(
            f"  rec {mr['recording_index']:>4} {markers} "
            f"{mr['chunks']:>5} ch  "
            f"~{mr['approx_duration_ms'] / 1000:>6.1f}s  "
            f"start={mr['first_chunk']['absolute_timestamp_iso']}  "
            f"-> {mr['file']}")
    click.echo(f"Manifest: {manifest_path}")
    click.echo(f"Events:   {events_path}")


# ---------------------------------------------------------------------------

@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("-v", "--verbose", is_flag=True, help="Verbose logging.")
@click.version_option(__version__)
def main(verbose: bool) -> None:
    """Limitless-Pendant client."""
    _setup_logging(verbose)


@main.command()
@click.option("--timeout", default=8.0, show_default=True,
              help="Scan timeout in seconds.")
def scan(timeout: float) -> None:
    """Scan for an advertising pendant."""
    addr = asyncio.run(find_pendant(timeout=timeout))
    if addr is None:
        click.echo("No pendant found.", err=True)
        sys.exit(2)
    click.echo(addr)


@main.command()
@click.argument("address")
@click.option("--handshake/--no-handshake", default=True,
              help="Run the post-pair handshake (SetCurrentTime) before "
                   "querying. Some firmware versions require it.")
def info(address: str, handshake: bool) -> None:
    """Print pendant device info."""
    async def go() -> None:
        async with PendantSession(address) as s:
            try:
                bl = await s._transport.read_battery_level()  # type: ignore[union-attr]
                click.echo(f"Battery (BAS svc):   {bl}%")
            except Exception:
                pass
            if handshake:
                click.echo("Sending SetCurrentTime (post-pair handshake)...")
                await s.handshake()
            di = await s.get_device_info()
            click.echo(f"Device id:           {di.device_id}")
            click.echo(f"Serial number:       {di.serial_num!r}")
            click.echo(f"Firmware version:    {di.firmware_ver}")
            click.echo(f"Battery:             {di.battery_percent}%")
            click.echo(f"Flash pages:         {di.oldest_flash_page} .. {di.newest_flash_page}")
            click.echo(f"Audio enc pubkey:    {di.audio_encryption_pub_key.hex()[:32]}...")

    _user_friendly(go)


@main.command()
@click.argument("address")
def pair(address: str) -> None:
    """Pair (bond) with the pendant at <address>.

    The pendant requires an encrypted BLE link before it will accept
    commands. The pendant only allows ONE bond at a time — if the device
    has been used with the Limitless Android app, that bond must first be
    cleared on the *pendant side* (factory reset via the app, OR `Forget`
    the pendant in Android Bluetooth settings).

    Once the previous bond is cleared, run this command. On Windows this
    will trigger the OS pairing dialog.
    """
    from bleak import BleakClient

    async def go() -> None:
        client = BleakClient(address)
        await client.connect()
        try:
            ok = await client.pair()
            if ok:
                click.echo("Paired successfully.")
            else:
                click.echo("client.pair() returned False — check OS Bluetooth "
                           "settings; you may need to confirm a passkey there.",
                           err=True)
        finally:
            await client.disconnect()

    asyncio.run(go())


@main.command("unpair")
@click.argument("address")
def unpair(address: str) -> None:
    """Tell the pendant to clear its bond (BLE side only).

    NOTE: this requires the link to *already* be authenticated, so this
    only works once you've successfully paired. Useful before retiring a
    pendant or transferring it. Sends ServerCommandMsg.unpair_bluetooth
    (case 15) which the firmware handles via `handle_unpair_bluetooth`.
    """
    from .proto import server_pb2

    async def go() -> None:
        async with PendantSession(address) as s:
            await s._send_oneway(
                unpair_bluetooth=server_pb2.UnpairBluetooth(do_not_reset_device=False),
            )
    _user_friendly(go)
    click.echo("Sent unpair_bluetooth.")


@main.command()
@click.argument("address")
@click.option("-n", "--num-pages", default=5, show_default=True,
              help="Number of flash_page payloads to capture before exiting.")
@click.option("-o", "--out", "out_path", type=click.Path(),
              help="Append raw flash_page bytes (length-prefixed, 4B LE) to "
                   "this file for offline analysis.")
def inspect(address: str, num_pages: int, out_path: Optional[str]) -> None:
    """Capture and dump the first N raw flash_page payloads.

    Useful for diagnosing the encryption / format question:

      - High-entropy random bytes => the pendant is encrypting (the
        previous owner pushed a SetServerPublicKey, the flag is still
        set in RAM).
      - Bytes whose first nonzero offset starts with 0xB8 (and similar
        recognizable Opus TOC patterns) => unencrypted, our heuristic
        is just mis-classifying.
    """
    import statistics

    async def go() -> None:
        async with PendantSession(address) as s:
            click.echo("Sending SetCurrentTime + DownloadFlashPages...")
            await s.handshake()
            await s.begin_batch_download()
            click.echo(f"Capturing up to {num_pages} flash_page(s)...")
            captured = 0
            fp = open(out_path, "ab") if out_path else None
            try:
                while captured < num_pages:
                    try:
                        msg = await asyncio.wait_for(s._inbox.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        click.echo("Idle timeout; stopping.")
                        break
                    oneof = msg.WhichOneof("content")
                    if oneof != "storage_buffer":
                        click.echo(f"  (skipping {oneof})")
                        continue
                    sb = msg.storage_buffer
                    page = bytes(sb.flash_page)
                    if not page:
                        continue
                    captured += 1
                    if fp:
                        fp.write(len(page).to_bytes(4, "little"))
                        fp.write(page)
                    # Resolve ingest_type to a human-readable label
                    ingest_label = {
                        0: "UNSPECIFIED",
                        1: "REAL_TIME (audio)",
                        2: "BATCH (audio)",
                        3: "COMMAND (logs)",
                    }.get(sb.ingest_type, f"UNKNOWN({sb.ingest_type})")
                    # Hex dump first 96 bytes
                    click.echo(f"\n=== flash_page #{captured}  index={sb.index} "
                               f"session={sb.session} run={sb.run} seq={sb.seq} "
                               f"ingest_type={ingest_label} len={len(page)} ===")
                    for off in range(0, min(96, len(page)), 16):
                        chunk = page[off:off+16]
                        hexs = " ".join(f"{b:02x}" for b in chunk)
                        ascii_ = "".join(chr(b) if 0x20 <= b < 0x7f else "." for b in chunk)
                        click.echo(f"  {off:04x}: {hexs:<48}  {ascii_}")
                    if len(page) > 96:
                        click.echo(f"  ... ({len(page) - 96} more bytes)")
                    # Entropy heuristic: byte distribution
                    if len(page) >= 64:
                        # If "encrypted" the bytes will look ~uniform random
                        # (sigma close to sqrt(255^2/12) ~ 73.6). Plaintext
                        # protobuf has skewed bytes (lots of 0x08/0x10/0x22
                        # tags etc.).
                        bytes_list = list(page[:512])
                        try:
                            stdev = statistics.stdev(bytes_list)
                        except statistics.StatisticsError:
                            stdev = 0.0
                        zeros = sum(1 for b in bytes_list if b == 0)
                        click.echo(f"  byte stdev = {stdev:.1f}  zero-count = "
                                   f"{zeros}/{len(bytes_list)}")
                    await s.delete_flash_page(sb.index)
            finally:
                if fp:
                    fp.close()
                await s.stop_batch_download()
            click.echo(f"\nCaptured {captured} flash_page(s).")
            if out_path:
                click.echo(f"Raw bytes saved to {out_path}.")

    _user_friendly(go)


@main.command()
@click.argument("address")
def set_time(address: str) -> None:
    """Set the pendant's clock to current host time."""
    async def go() -> None:
        async with PendantSession(address) as s:
            await s.set_current_time_ms()
    _user_friendly(go)
    click.echo(f"Set pendant clock to {dt.datetime.now().isoformat()}")


def _resolve_paths(out_path: str) -> tuple[Path, Path]:
    """Given the user's `-o today.opus`, return (audio_base, bin_path).

    `audio_base` is the path used as the prefix for per-recording
    `_rec0000.opus` files plus the .manifest.json / .events.json
    sidecars. `bin_path` is where the byte-fidelity capture goes —
    always derived as `<base_stem>.bin` so the .bin is the canonical
    raw artifact.
    """
    out = Path(out_path)
    if out.suffix:
        bin_path = out.with_suffix(".bin")
    else:
        bin_path = out.with_name(out.name + ".bin")
    return out, bin_path


def _decode_chunks_into_files(
    *,
    audio_base: Path,
    chunks_iter,
    address: str,
    di,
    include_logs: bool,
    progress: bool = True,
) -> tuple[int, int, dict[int, "_RecordingWriter"]]:
    """Run heuristic-C splitting over a stream of AudioChunks.

    Returns (n_audio, n_events, recordings). Closes all writers on
    exit and writes the manifest + events sidecars regardless of
    success/error so partial captures are still useful.
    """
    recordings: dict[int, _RecordingWriter] = {}
    events: list[dict] = []
    n_audio = 0
    n_events = 0

    def open_writer(idx: int, chunk: AudioChunk) -> "_RecordingWriter":
        rw = _RecordingWriter(audio_base, idx)
        recordings[idx] = rw
        if progress:
            click.echo(
                f"  [rec {idx}] -> {rw.path.name}  "
                f"(ingest={_ingest_label(chunk.ingest_type)},"
                f" page_index={chunk.page_index},"
                f" rec_start={chunk.is_recording_start})")
        return rw

    splitter = RecordingSplitter(open_writer)
    try:
        for chunk in chunks_iter:
            ev = _event_for(chunk)
            if ev is not None:
                events.append(ev)
                n_events += 1
                if progress and (chunk.button is not None
                                  and chunk.button.button_event == 1):
                    click.echo(
                        f"  [event] STAR A MOMENT  "
                        f"page_index={chunk.page_index}")
            splitter.feed(chunk)
            if chunk.has_audio:
                n_audio += 1
            if progress and (n_audio + n_events) % 500 == 0:
                click.echo(
                    f"  ... {n_audio} audio + {n_events} event "
                    f"chunks across {len(recordings)} recording(s)")
    finally:
        splitter.finalize()
        for rw in recordings.values():
            rw.close()
        pruned = [idx for idx, rw in recordings.items()
                  if rw.unlink_if_empty()]
        for idx in pruned:
            del recordings[idx]
        _write_sync_manifest(
            out=audio_base, recordings=recordings, events=events,
            di=di, address=address,
            total_audio_chunks=n_audio,
            total_event_chunks=n_events,
            include_logs=include_logs,
        )

    return n_audio, n_events, recordings


# ---------------------------------------------------------------------------
# Async generator helpers used by `sync` and `capture`.

class _AsyncBytesTee:
    """Wraps an async iterator of bytes, writing each chunk to a
    streaming sink while still yielding it. Lets sync capture to .bin
    AND feed the audio decoder from the same source without buffering
    everything in memory."""

    def __init__(self, source, sink):
        self._source = source
        self._sink = sink

    def __aiter__(self):
        return self

    async def __anext__(self):
        b = await self._source.__anext__()
        self._sink(b)
        return b


# ---------------------------------------------------------------------------

@main.command()
@click.argument("address")
@click.option("-o", "--out", "out_path", required=True, type=click.Path(),
              help="Output base path. Wire-fidelity capture goes to "
                   "`<base_stem>.bin` (BatchIngestRequest format, matches "
                   "the official Limitless app's `C10459i.m41379i` "
                   "persistence). Per-recording decoded audio goes to "
                   "`<base>_rec<NNNN><ext>` with a `<base>.manifest.json` "
                   "and `<base>.events.json` sidecar. Use .opus/.ogg for "
                   "Ogg Opus output.")
@click.option("--key", "key_path", type=click.Path(exists=True),
              help="Server private key (PEM). If given, decrypt chunks with it.")
@click.option("--push-key", is_flag=True,
              help="If --key not given, generate a fresh keypair, push the "
                   "pubkey to the pendant, and save the private key alongside "
                   "the output file (`<out>.key.pem`).")
@click.option("--idle", default=5.0, show_default=True,
              help="Idle timeout (s) — stop after this long with no chunks.")
@click.option("--no-decode", is_flag=True,
              help="Capture the .bin only; skip the decode step. "
                   "You can run `pendant decode <bin>` later.")
@click.option("--include-logs", is_flag=True,
              help="Also process COMMAND-type flash_pages (Zephyr debug logs). "
                   "Off by default — they're not audio and would corrupt the "
                   "Opus output.")
def sync(address: str, out_path: str, key_path: Optional[str],
         push_key: bool, idle: float, no_decode: bool,
         include_logs: bool) -> None:
    """Download from the pendant — captures wire-fidelity .bin AND
    decodes per-recording .opus files in one shot.

    Two artifacts are produced by default:

    1. `<base_stem>.bin` — wire-fidelity capture (BatchIngestRequest
       proto). This is the canonical raw artifact, matching the
       official Limitless app's persistence. You can re-decode it any
       time with `pendant decode`.

    2. `<base>_rec<NNNN><ext>` files — per-recording decoded audio,
       split using a pre-roll-merging heuristic (see
       `pendant_client/decode.py`).

    The `_rec*` files are derived from the .bin and are best-effort —
    recording boundaries are determined server-side by the official
    Limitless backend; we approximate using the
    `audio_data.did_*_recording` flags.

    Use `--no-decode` to skip step 2 (capture-only mode, equivalent to
    `pendant capture`).
    """

    async def go() -> int:
        audio_base, bin_path = _resolve_paths(out_path)
        async with PendantSession(address) as s:
            di = await s.get_device_info()
            click.echo(f"Connected: serial={di.serial_num} fw={di.firmware_ver} "
                       f"battery={di.battery_percent}% pages={di.oldest_flash_page}..{di.newest_flash_page}")

            keyset: Optional[AudioKeySet] = None
            if key_path:
                keyset = keyset_from_existing(
                    Path(key_path).read_bytes(), di.audio_encryption_pub_key)
                click.echo(f"Loaded server key from {key_path}")
            elif push_key:
                keyset = generate_keyset(di.audio_encryption_pub_key)
                priv_path = audio_base.with_suffix(audio_base.suffix + ".key.pem")
                priv_path.write_bytes(keyset.server_priv.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.PKCS8,
                    encryption_algorithm=serialization.NoEncryption(),
                ))
                click.echo(f"Generated new keypair; saved to {priv_path}")
                await s.set_server_public_key(keyset.server_pub_bytes)
                click.echo("Pushed server pubkey to pendant.")

            # Capture: stream raw PendantAllMsg bytes to the .bin while
            # holding a copy in memory for the decode step. The .bin is
            # the canonical artifact; we read the in-memory copy for
            # decoding to avoid a disk re-read.
            raw_messages: list[bytes] = []
            click.echo(f"Capturing to {bin_path.name}...")
            n_captured = 0
            try:
                async for payload in s.download_raw_pendant_messages(
                        idle_timeout=idle):
                    raw_messages.append(payload)
                    n_captured += 1
                    if n_captured % 500 == 0:
                        click.echo(f"  ... {n_captured} messages captured")
            finally:
                # Always write whatever we got, even on Ctrl+C.
                bytes_written = write_batch_ingest_request(
                    messages=raw_messages,
                    ble_identifier=address,
                    path=bin_path,
                )
                click.echo(
                    f"Captured {n_captured} messages "
                    f"({bytes_written} bytes) -> {bin_path.name}")

        if no_decode:
            return 0

        # Decode: feed the in-memory raw messages through the audio
        # parser + heuristic-C splitter.
        click.echo(f"\nDecoding into {audio_base.name}_rec*{audio_base.suffix} ...")
        accept_types = (1, 2, 3) if include_logs else (1, 2)
        chunks = iter_audio_chunks_from_messages(
            raw_messages, keys=keyset, accept_types=accept_types)
        n_audio, n_events, _ = _decode_chunks_into_files(
            audio_base=audio_base,
            chunks_iter=chunks,
            address=address,
            di=di,
            include_logs=include_logs,
        )
        if n_audio == 0 and n_events == 0:
            click.echo(
                "\nNo audio chunks captured. Possible reasons:\n"
                "  - Pendant just rebooted and no voice activity yet\n"
                "    (recording is VAD-gated; talk near it then retry).\n"
                "  - Audio was filtered as wrong ingest_type — try\n"
                "    `pendant inspect -n 5 <addr>` to see what's there.")
        return 0

    rc = _user_friendly(go) or 0
    sys.exit(rc)


@main.command()
@click.argument("address")
@click.option("-o", "--out", "out_path", required=True, type=click.Path(),
              help="Output .bin path (BatchIngestRequest proto). The "
                   "captured bytes match the official Limitless app's "
                   "`C10459i.m41379i` persistence layer byte-for-byte: "
                   "field 1 = repeated PendantAllMsg, field 2 = "
                   "ble_identifier (= the address).")
@click.option("--idle", default=5.0, show_default=True,
              help="Idle timeout (s) — stop after this long with no chunks.")
def capture(address: str, out_path: str, idle: float) -> None:
    """Capture only — no decoding into per-recording files.

    Wire-fidelity dump of every PendantAllMsg the pendant sends,
    written as a BatchIngestRequest proto. This is the lossless raw
    artifact; you can decode it later with `pendant decode <bin>`,
    feed it to a self-hosted backend, or replay it for testing.
    """
    async def go() -> int:
        out = Path(out_path)
        async with PendantSession(address) as s:
            click.echo(f"Capturing to {out.name}...")
            raw_messages: list[bytes] = []
            n_captured = 0
            try:
                async for payload in s.download_raw_pendant_messages(
                        idle_timeout=idle):
                    raw_messages.append(payload)
                    n_captured += 1
                    if n_captured % 500 == 0:
                        click.echo(f"  ... {n_captured} messages captured")
            finally:
                bytes_written = write_batch_ingest_request(
                    messages=raw_messages,
                    ble_identifier=address,
                    path=out,
                )
                click.echo(
                    f"Captured {n_captured} messages "
                    f"({bytes_written} bytes) -> {out}")
        return 0

    rc = _user_friendly(go) or 0
    sys.exit(rc)


@main.command()
@click.argument("bin_path", type=click.Path(exists=True))
@click.option("-o", "--out", "out_path", required=True, type=click.Path(),
              help="Output base path for per-recording .opus files and "
                   "manifest/events sidecars.")
@click.option("--key", "key_path", type=click.Path(exists=True),
              help="Server private key (PEM) and pendant pubkey (DER, "
                   "65 bytes uncompressed) — needed only if the .bin "
                   "contains encrypted audio. Without it, encrypted "
                   "audio chunks are skipped.")
@click.option("--pendant-pubkey", "pendant_pubkey_path",
              type=click.Path(exists=True),
              help="Pendant public key (65 raw bytes uncompressed P-256). "
                   "Required when --key is given.")
@click.option("--include-logs", is_flag=True,
              help="Also process COMMAND-type flash_pages.")
def decode(bin_path: str, out_path: str, key_path: Optional[str],
           pendant_pubkey_path: Optional[str], include_logs: bool) -> None:
    """Decode a captured .bin into per-recording .opus files.

    The .bin is a `BatchIngestRequest` proto produced by
    `pendant capture` (or `pendant sync`). This command applies the
    pre-roll-merging heuristic from `decode.py` and writes the same
    `_rec<NNNN>.opus`, manifest.json, and events.json files that
    `sync` produces — but offline, from a saved capture.

    Decoupling capture from decode lets you re-run the splitter with
    different policies without re-syncing the pendant.
    """
    src = Path(bin_path)
    audio_base = Path(out_path)

    meta = read_batch_ingest_request_metadata(src)
    click.echo(f"Reading {src.name}: {meta['messages']} messages, "
               f"{meta['size_bytes']} bytes, "
               f"ble_identifier={meta['ble_identifier']!r}")

    keyset: Optional[AudioKeySet] = None
    if key_path:
        if not pendant_pubkey_path:
            click.echo("--key requires --pendant-pubkey", err=True)
            sys.exit(2)
        keyset = keyset_from_existing(
            Path(key_path).read_bytes(),
            Path(pendant_pubkey_path).read_bytes(),
        )

    # Build a minimal device-info stand-in for the manifest. We don't
    # have the real device_info offline, only the BLE identifier from
    # the .bin. The manifest will reflect that.
    class _OfflineDeviceInfo:
        device_id = 0
        serial_num = ""
        firmware_ver = ""
        battery_percent = 0
        oldest_flash_page = 0
        newest_flash_page = 0
    di = _OfflineDeviceInfo()

    accept_types = (1, 2, 3) if include_logs else (1, 2)
    chunks = iter_audio_chunks_from_messages(
        iter_messages_from_bin(src),
        keys=keyset,
        accept_types=accept_types,
    )
    n_audio, n_events, _ = _decode_chunks_into_files(
        audio_base=audio_base,
        chunks_iter=chunks,
        address=meta["ble_identifier"] or "",
        di=di,
        include_logs=include_logs,
    )
    if n_audio == 0 and n_events == 0:
        click.echo("(No audio chunks decoded — capture appears empty "
                   "or all chunks were status-only.)")


@main.command()
@click.argument("address")
@click.option("--seconds", default=20, show_default=True,
              help="How long to listen for raw notifications.")
@click.option("--probe/--no-probe", default=True,
              help="After listing chars, fire one GetDeviceInfo and "
                   "watch for any inbound traffic.")
def debug(address: str, seconds: int, probe: bool) -> None:
    """Diagnostic: connect, list characteristics, dump raw notifications.

    Use this when `info` or `sync` time out. Prints:
      1. All services + characteristics + properties (the write char's
         properties tell you whether encryption is required and what
         write types are supported).
      2. Every raw BLE notification verbatim (hex), to see whether the
         pendant is sending anything at all.
      3. Optionally fires a GetDeviceInfo and waits for any reply.
    """
    from .ble import PendantTransport
    from .proto import server_pb2

    async def go() -> None:
        from bleak import BleakClient
        client = BleakClient(address)
        async with PendantTransport(client) as t:
            click.echo("=== GATT structure ===")
            for c in await t.list_characteristics():
                click.echo(f"  service {c['service']}")
                click.echo(f"    char  {c['char']}  handle={c['handle']}  "
                           f"props={c['properties']}")

            received: list[bytes] = []

            def on_raw(b: bytes) -> None:
                received.append(b)
                click.echo(f"  notify {len(b):>3}B  {b.hex()}")

            t.on_raw_fragment(on_raw)
            click.echo(f"\n=== Listening for raw notifications for {seconds}s ===")

            if probe:
                click.echo("Sending GetDeviceInfo (request_id=1)...")
                cmd = server_pb2.ServerCommandMsg()
                cmd.request_data.request_id = 1
                cmd.get_device_info.CopyFrom(server_pb2.GetDeviceInfo())
                wire = cmd.SerializeToString()
                click.echo(f"  encoded ServerCommandMsg ({len(wire)}B): {wire.hex()}")
                await t.send_command(wire)

            await asyncio.sleep(seconds)

            click.echo(f"\n=== {len(received)} raw notification(s) received ===")
            if not received:
                click.echo(
                    "  Possible causes:\n"
                    "  - Bonding/encryption: the pendant requires a paired link.\n"
                    "    On Windows, pair via Settings > Bluetooth, or factory-\n"
                    "    reset the pendant first (long-press its button per the\n"
                    "    Limitless docs, or use the official app once to clear\n"
                    "    the previous bond).\n"
                    "  - Write characteristic dropped the request silently.\n"
                    "    Compare the props for 632de002 above against what the\n"
                    "    Limitless app uses; if you see 'authenticated-' flags\n"
                    "    the link must be encrypted before writes succeed.\n"
                    "  - Pendant is bonded to a different device. The pendant\n"
                    "    accepts incoming connections from anyone but rejects\n"
                    "    GATT writes from non-bonded peers."
                )

    asyncio.run(go())


@main.command("clear-storage")
@click.argument("address")
@click.confirmation_option(prompt="Erase all flash storage on the pendant?")
def clear_storage(address: str) -> None:
    """Wipe the pendant's flash log (FCB only, no reboot).

    Does NOT clear the audio_encryption_enabled RAM flag — for that
    you need `factory-reset` or `reset-device`.
    """
    async def go() -> None:
        async with PendantSession(address) as s:
            from .proto import server_pb2
            await s._send_oneway(
                clear_pendant_storage=server_pb2.ClearPendantStorage(),
            )
    _user_friendly(go)
    click.echo("Sent clear_pendant_storage.")


@main.command("factory-reset")
@click.argument("address")
@click.confirmation_option(prompt=(
    "Factory-reset the pendant?\n"
    "  This wipes flash storage AND triggers a system reboot ~500ms\n"
    "  later. The reboot clears the audio_encryption_enabled RAM flag,\n"
    "  so subsequent audio is plaintext Opus.\n"
    "  Bond keys are also cleared — you'll need to re-pair afterwards.\n"
    "  Continue?"))
def factory_reset(address: str) -> None:
    """Factory-reset the pendant (wipe + reboot).

    Sends ServerCommandMsg.factory_reset_pendant (case 10). The pendant
    will disconnect ~500 ms later, so this command intentionally doesn't
    wait for any response.
    """
    async def go() -> None:
        async with PendantSession(address) as s:
            await s.factory_reset()
            # Give the firmware a moment to schedule the reset before we
            # tear down the connection.
            await asyncio.sleep(0.3)

    try:
        _user_friendly(go)
    except Exception as e:
        # The pendant may drop the link mid-disconnect; that's expected.
        click.echo(f"(connection ended: {e})", err=True)
    click.echo("Sent factory_reset_pendant. The device will reboot in ~500ms.")
    click.echo("Wait ~5 seconds, then re-pair (Windows Bluetooth Settings or "
               "`pendant pair <addr>`).")


@main.command("reset")
@click.argument("address")
@click.confirmation_option(prompt=(
    "Soft-reset the pendant (reboot only, no flash wipe)?"))
def reset_device(address: str) -> None:
    """Soft reset the pendant (reboot, keep flash storage).

    Sends ServerCommandMsg.reset_device (case 11). Use this if you want
    to clear the audio_encryption_enabled RAM flag without losing
    already-recorded flash pages. (Though if those pages were recorded
    while encryption was on with someone else's key, you still can't
    decrypt them.)
    """
    async def go() -> None:
        async with PendantSession(address) as s:
            await s.reset_device()
            await asyncio.sleep(0.3)

    try:
        _user_friendly(go)
    except Exception as e:
        click.echo(f"(connection ended: {e})", err=True)
    click.echo("Sent reset_device. The device will reboot shortly.")


if __name__ == "__main__":
    main()
