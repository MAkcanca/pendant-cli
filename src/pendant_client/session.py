"""High-level pendant operations: pair, info, sync.

Wraps `PendantTransport` and the proto codec for the common workflows.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import AsyncIterator, Optional

from bleak import BleakClient, BleakScanner

from .ble import PendantTransport
from .crypto import AudioKeySet, decrypt_audio
from .flash_page import (  # noqa: F401  (re-exported for callers)
    AudioPayload,
    ButtonEvent,
    Chunk as FlashPageChunk,
    EncryptedAudio,
    FlashPage,
    RecordingState,
    StorageSessionMarker,
    parse_flash_page,
)

# Generated proto modules. Run `python scripts/gen_proto.py` first.
try:
    from .proto import server_pb2
    from .proto import shared_pb2  # noqa: F401  (used transitively)
except ImportError as e:
    raise ImportError(
        "Proto bindings not generated. Run:\n"
        "    python -m pip install -e \".[dev]\"\n"
        "    python scripts/gen_proto.py\n"
        f"  ({e})"
    ) from e

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers for building/decoding the outer ServerCommandMsg / PendantAllMsg

def _next_request_id() -> int:
    # Match the official app's pattern (atomic increment from 1). Any
    # positive int works; the device just echoes it back in ResponseData.
    _next_request_id._n = getattr(_next_request_id, "_n", 0) + 1
    return _next_request_id._n


def build_command(**oneof_kwargs) -> bytes:
    """Build a `ServerCommandMsg` with one oneof set, plus a fresh request_id.

    Usage:
        build_command(get_device_info=server_pb2.GetDeviceInfo())
    """
    if len(oneof_kwargs) != 1:
        raise ValueError("expected exactly one oneof field")
    msg = server_pb2.ServerCommandMsg()
    msg.request_data.request_id = _next_request_id()
    field, value = next(iter(oneof_kwargs.items()))
    getattr(msg, field).CopyFrom(value)
    return msg.SerializeToString()


def parse_pendant_all(b: bytes) -> server_pb2.PendantAllMsg:
    msg = server_pb2.PendantAllMsg()
    msg.ParseFromString(b)
    return msg


# ---------------------------------------------------------------------------
# Pendant scan & connect

async def find_pendant(timeout: float = 8.0) -> Optional[str]:
    """Scan for an advertising pendant and return its MAC / address."""
    devices = await BleakScanner.discover(timeout=timeout, return_adv=True)
    for dev, adv in devices.values():
        uuids = (adv.service_uuids or [])
        if any(u.lower().startswith("632de001") for u in uuids):
            log.info("Found pendant: %s (%s)", dev.address, dev.name)
            return dev.address
    return None


# ---------------------------------------------------------------------------
# Session

@dataclass
class DeviceInfo:
    device_id: int
    serial_num: str
    firmware_ver: str
    battery_percent: int
    audio_encryption_pub_key: bytes
    oldest_flash_page: int
    newest_flash_page: int
    raw: server_pb2.DeviceInfoMsg


@dataclass
class AudioChunk:
    """One Chunk pulled from a FlashPage, with its enclosing metadata.

    `chunk` holds the parsed `flash_page.Chunk` (audio + status fields).
    The fields surrounding it identify *which* page the chunk came
    from — a Chunk on its own carries no page-level identifying info.

    The `chunk.audio.did_start_recording` /
    `chunk.audio.did_stop_recording` flags are the canonical
    recording-boundary markers. Per the decompiled Limitless app
    (`p643d/C10455e.java:229`), the official client splits on
    `isLastInSession` (= `did_stop_recording`) AND `hasAudioData`,
    not on the StorageBufferMsg.session field.
    """
    chunk: FlashPageChunk

    # StorageBufferMsg envelope (server.proto:281-289).
    # Kept as metadata only — we don't split recordings on these.
    storage_session: int = 0
    run: int = 0
    page_seq_uptime_ms: int = 0
    page_index: int = 0
    ingest_type: int = 0
    flash_page_error: int = 0

    # FlashPage page-level fields.
    absolute_timestamp_ms: int = 0    # wall-clock when the page was sealed
    boot_uptime_ms: int = 0            # pendant uptime when the page was sealed

    # Client-side wall-clock at receipt — fallback for sessions where
    # the pendant clock hasn't been synced.
    received_at_unix_ms: int = 0

    # Convenience proxies onto the inner Chunk.

    @property
    def opus(self) -> bytes:
        if self.chunk.audio is None:
            return b""
        return self.chunk.audio.opus_packets

    @property
    def has_audio(self) -> bool:
        return self.chunk.has_audio

    @property
    def is_recording_start(self) -> bool:
        return self.chunk.is_recording_start

    @property
    def is_recording_stop(self) -> bool:
        return self.chunk.is_recording_stop

    @property
    def is_storage_session_start(self) -> bool:
        return self.chunk.is_storage_session_start

    @property
    def is_storage_session_stop(self) -> bool:
        return self.chunk.is_storage_session_stop

    @property
    def button(self) -> Optional[ButtonEvent]:
        return self.chunk.button

    @property
    def recording(self) -> Optional[RecordingState]:
        return self.chunk.recording

    @property
    def storage(self) -> Optional[StorageSessionMarker]:
        return self.chunk.storage

    @property
    def absolute_chunk_timestamp_ms(self) -> int:
        """Wall-clock estimate for *this* chunk: page wall-clock plus
        the chunk's `time_offset_ms` within the page."""
        offset = self.chunk.time_offset_ms or 0
        return self.absolute_timestamp_ms + max(offset, 0)


class PendantSession:
    """High-level interface for one connection. Use as an async context
    manager:

        async with PendantSession(address) as s:
            info = await s.get_device_info()
            async for opus in s.download(aes_key):
                ...
    """

    def __init__(self, address: str):
        self._address = address
        self._client = BleakClient(address)
        self._transport: Optional[PendantTransport] = None
        # request_id -> Future to deliver the matching response
        self._pending: dict[int, asyncio.Future[server_pb2.PendantAllMsg]] = {}
        # Pending content-type matchers — if the firmware doesn't echo
        # request_id on certain responses (case 14 = get_device_info
        # appears not to), match by oneof name as a fallback.
        # name -> Future
        self._pending_by_oneof: dict[str, asyncio.Future[server_pb2.PendantAllMsg]] = {}
        # Background queue of *unsolicited* messages (storage_buffer notifications, etc.).
        # Each entry is (raw_payload_bytes, parsed_msg). The raw bytes let
        # us write byte-identical BatchIngestRequest captures matching the
        # official Limitless app's `C10459i.m41379i` persistence path.
        self._inbox: asyncio.Queue[
            tuple[bytes, server_pb2.PendantAllMsg]
        ] = asyncio.Queue()

    async def __aenter__(self) -> "PendantSession":
        self._transport = await PendantTransport(self._client).__aenter__()
        self._transport.on_message(self._on_inbound)
        return self

    async def __aexit__(self, *exc):
        if self._transport is not None:
            await self._transport.__aexit__(*exc)

    # ----- inbound dispatch -----

    def _on_inbound(self, payload: bytes) -> None:
        try:
            msg = parse_pendant_all(payload)
        except Exception:
            log.exception("Could not decode PendantAllMsg (%d bytes)", len(payload))
            return
        oneof = msg.WhichOneof("content")
        rid = msg.response_data.request_id if msg.HasField("response_data") else 0
        log.debug("inbound PendantAllMsg: oneof=%s response_data.request_id=%s",
                  oneof, rid)

        # Primary match: explicit request_id
        if rid:
            fut = self._pending.pop(rid, None)
            if fut is not None and not fut.done():
                fut.set_result(msg)
                return

        # Fallback: match by oneof name (in case firmware doesn't echo request_id
        # for some commands — observed for case 14 = get_device_info).
        if oneof:
            fut = self._pending_by_oneof.pop(oneof, None)
            if fut is not None and not fut.done():
                fut.set_result(msg)
                return

        # Otherwise: treat as unsolicited (heartbeats, storage_buffer chunks).
        # Keep the raw payload bytes alongside the parsed message so callers
        # can do wire-fidelity capture.
        self._inbox.put_nowait((payload, msg))

    async def _request(self, expected_oneof: Optional[str] = None,
                        **oneof_kwargs) -> server_pb2.PendantAllMsg:
        if self._transport is None:
            raise RuntimeError("session not entered")
        # Build command, capture request_id for matching.
        cmd = server_pb2.ServerCommandMsg()
        cmd.request_data.request_id = _next_request_id()
        field, value = next(iter(oneof_kwargs.items()))
        getattr(cmd, field).CopyFrom(value)

        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._pending[cmd.request_data.request_id] = fut

        # Also register a content-type fallback. The expected response
        # oneof name is given by the caller (e.g. "device_info" for
        # get_device_info). Some firmware response paths don't include
        # response_data, so we need this fallback.
        if expected_oneof and expected_oneof not in self._pending_by_oneof:
            self._pending_by_oneof[expected_oneof] = fut

        await self._transport.send_command(cmd.SerializeToString())
        try:
            return await asyncio.wait_for(fut, timeout=15.0)
        finally:
            # Clean up if we registered a fallback that didn't fire.
            if expected_oneof and self._pending_by_oneof.get(expected_oneof) is fut:
                self._pending_by_oneof.pop(expected_oneof, None)

    async def _send_oneway(self, **oneof_kwargs) -> None:
        """Send a command without expecting a request/response match."""
        if self._transport is None:
            raise RuntimeError("session not entered")
        cmd = server_pb2.ServerCommandMsg()
        cmd.request_data.request_id = _next_request_id()
        cmd.request_data.do_not_ack = True
        field, value = next(iter(oneof_kwargs.items()))
        getattr(cmd, field).CopyFrom(value)
        await self._transport.send_command(cmd.SerializeToString())

    # ----- specific commands -----

    async def factory_reset(self) -> None:
        """Tell the pendant to factory-reset and reboot.

        Sends ServerCommandMsg.factory_reset_pendant (case 10). The
        firmware handler is `handle_factory_reset_pendant` (FUN_0000eb1c)
        which schedules a system reboot ~500 ms after the command. The
        reboot clears the encryption RAM flag (`audio_encryption_enabled`
        at RAM 0x20017018, BSS-init), so all subsequent recordings will
        be plaintext Opus.

        Note: this also clears flash storage (case 25 path) AND any
        bond keys, so you'll need to re-pair afterwards.
        """
        await self._send_oneway(
            factory_reset_pendant=server_pb2.FactoryResetPendant(),
        )

    async def reset_device(self) -> None:
        """Soft reset (reboot, no flash wipe). Sends ServerCommandMsg.reset_device
        (case 11). Useful for clearing the encryption RAM flag without losing
        already-recorded flash content."""
        await self._send_oneway(
            reset_device=server_pb2.ResetDevice(),
        )

    async def get_device_info(self) -> DeviceInfo:
        resp = await self._request(
            expected_oneof="device_info",
            get_device_info=server_pb2.GetDeviceInfo(),
        )
        di = resp.device_info
        return DeviceInfo(
            device_id=di.device_id,
            serial_num=di.serial_num,
            firmware_ver=di.firmware_ver,
            battery_percent=di.battery_percent,
            audio_encryption_pub_key=bytes(di.device_audio_encryption_pub_key),
            oldest_flash_page=di.oldest_flash_page,
            newest_flash_page=di.newest_flash_page,
            raw=di,
        )

    async def handshake(self) -> None:
        """Run the same post-pair sequence the official app uses.

        Order matches `transitionToPaired()` in the Android app:
          1) SetCurrentTime
          2) DownloadFlashPages(batch_mode_enabled=true)

        Some pendant firmware versions only respond to query commands
        once this handshake has completed. Calling this is harmless
        even if the device is already happy."""
        await self.set_current_time_ms()
        # Note: the Android app sets batch_mode here. We let the caller
        # decide whether to start the actual download via `download()`,
        # so don't kick off batch mode unconditionally here.

    async def set_current_time_ms(self, ts_ms: Optional[int] = None) -> None:
        if ts_ms is None:
            ts_ms = int(time.time() * 1000)
        await self._send_oneway(
            set_current_time=server_pb2.SetCurrentTime(unix_timestamp_ms=ts_ms),
        )

    async def set_server_public_key(self, server_pub_uncompressed: bytes) -> None:
        """Push a server public key to the pendant. After the pendant
        receives this, it derives a session AES key and starts encrypting
        each audio chunk with it."""
        await self._send_oneway(
            set_audio_encryption_server_public_key=
            server_pb2.SetServerPublicKey(server_pub_key=server_pub_uncompressed),
        )

    async def start_recording(self) -> None:
        await self._send_oneway(start_recording=server_pb2.StartRecording())

    async def stop_recording(self) -> None:
        await self._send_oneway(stop_recording=server_pb2.StopRecording())

    async def begin_batch_download(self) -> None:
        await self._send_oneway(
            download_flash_pages=server_pb2.DownloadFlashPages(
                batch_mode_enabled=True,
                real_time_mode_enabled=False,
            ),
        )

    async def stop_batch_download(self) -> None:
        await self._send_oneway(
            download_flash_pages=server_pb2.DownloadFlashPages(
                batch_mode_enabled=False,
                real_time_mode_enabled=False,
            ),
        )

    async def delete_flash_page(self, older_or_equal_index: int) -> None:
        """Tell the pendant it can free flash pages up to and including
        the given index."""
        await self._send_oneway(
            delete_flash_page=server_pb2.DeleteFlashPage(
                older_than_or_equal_to_index=older_or_equal_index,
            ),
        )

    async def get_device_status(self) -> server_pb2.DeviceStatus:
        resp = await self._request(
            expected_oneof="device_status",
            get_device_status=server_pb2.GetDeviceStatus(),
        )
        return resp.device_status

    # ----- audio download -----
    #
    # The pendant uses StorageBufferMsg.flash_page for TWO different
    # payloads, distinguished by `ingest_type`:
    #
    #   ingest_type=DEVICE_INGEST_TYPE_REAL_TIME (1) — encrypted Opus
    #   ingest_type=DEVICE_INGEST_TYPE_BATCH     (2) — encrypted Opus
    #   ingest_type=DEVICE_INGEST_TYPE_COMMAND   (3) — Zephyr debug logs
    #                                                 (NOT audio; framed as
    #                                                  a different proto schema
    #                                                  carrying log records)
    #
    # The Limitless backend ingests both — they presumably attach the logs
    # to the user's session for diagnostics. Our client filters to the
    # audio types only by default; pass a different `accept_types` set to
    # also collect logs.

    AUDIO_INGEST_TYPES = (1, 2)  # REAL_TIME, BATCH

    async def download_raw_pendant_messages(
        self,
        idle_timeout: float = 5.0,
    ) -> AsyncIterator[bytes]:
        """Drain pendant-side flash. Yields the raw bytes of every
        `PendantAllMsg` the pendant sends.

        Wire-fidelity capture matching the official Limitless app's
        `C10459i.m41379i` persistence path — the bytes yielded here can
        be re-wrapped into a `BatchIngestRequest` byte-for-byte the way
        the app would have. Use this for capture; use `iter_audio_chunks`
        in `decode.py` if you also want decoded `AudioChunk` objects.

        Acks each storage_buffer page via `delete_flash_page`. Returns
        when no message has arrived for `idle_timeout` seconds.
        """
        from .ble import suppress_disconnect

        await self.begin_batch_download()
        try:
            while True:
                try:
                    payload, msg = await asyncio.wait_for(
                        self._inbox.get(), timeout=idle_timeout)
                except asyncio.TimeoutError:
                    log.info("No more chunks (idle for %.1fs); stopping.",
                             idle_timeout)
                    break
                yield payload
                if msg.WhichOneof("content") == "storage_buffer":
                    # Acking a delete on a disconnected link is just lost
                    # work, not a failure — the pendant will redeliver
                    # the page next sync if it didn't see the ack.
                    await suppress_disconnect(
                        self.delete_flash_page(msg.storage_buffer.index),
                        what="delete_flash_page",
                    )
        finally:
            # If the link dropped mid-stream, the polite "stop batch"
            # write fails harmlessly here; the bytes we already captured
            # are intact in the caller's buffer.
            await suppress_disconnect(
                self.stop_batch_download(),
                what="stop_batch_download",
            )

    async def download(
        self,
        keys: Optional[AudioKeySet] = None,
        idle_timeout: float = 5.0,
        accept_types: tuple[int, ...] = AUDIO_INGEST_TYPES,
    ) -> AsyncIterator[AudioChunk]:
        """Drain audio from flash. Yields one `AudioChunk` per Opus packet.

        Convenience wrapper around `download_raw_pendant_messages` that
        also parses each PendantAllMsg into AudioChunks. Prefer
        `download_raw_pendant_messages` for capture-and-decode workflows
        so you can keep the raw wire bytes alongside the parsed view.
        """
        from .decode import iter_audio_chunks_from_messages

        async for payload in self.download_raw_pendant_messages(
                idle_timeout=idle_timeout):
            received_at_ms = int(time.time() * 1000)
            for chunk in iter_audio_chunks_from_messages(
                    [payload], keys=keys, accept_types=accept_types):
                chunk.received_at_unix_ms = received_at_ms
                yield chunk
