"""HTTP upload helper for pushing captured .bin files to a server."""
from __future__ import annotations

from pathlib import Path

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
