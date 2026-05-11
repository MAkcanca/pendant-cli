"""HTTP upload helper for pushing captured .bin files to a server."""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Iterable

import httpx


def push_bin(
    *,
    client: httpx.Client,
    bin_path: Path,
    url: str,
    peripheral_id: str,
    timeout: float = 60.0,
) -> dict:
    """POST the .bin to a `/v3/pendant-upload-data-ordered`-style endpoint."""
    body = bin_path.read_bytes()
    resp = client.post(
        url,
        params={"blePeripheralDeviceId": peripheral_id},
        content=body,
        headers={"Content-Type": "application/octet-stream"},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def push_messages(
    *,
    client: httpx.Client,
    messages: Iterable[bytes],
    url: str,
    peripheral_id: str,
    timeout: float = 60.0,
) -> dict:
    """Wrap a sequence of raw PendantAllMsg bytes into a BatchIngestRequest
    and POST it. Used by `pendant sync --upload` after the sync finishes."""
    from .decode import write_batch_ingest_request

    with tempfile.TemporaryDirectory() as td:
        bin_path = Path(td) / "upload.bin"
        write_batch_ingest_request(
            messages=messages,
            ble_identifier=peripheral_id,
            path=bin_path,
        )
        return push_bin(
            client=client,
            bin_path=bin_path,
            url=url,
            peripheral_id=peripheral_id,
            timeout=timeout,
        )
