"""Tests for pendant push (upload a .bin to a remote ingest endpoint)."""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from pendant_client.upload import push_bin


def test_push_bin_posts_to_ingest_url(tmp_path: Path) -> None:
    bin_path = tmp_path / "x.bin"
    bin_path.write_bytes(b"BINBYTES")

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content
        captured["query"] = dict(request.url.params)
        return httpx.Response(200, json={"batch_id": 7, "bytes": 8})

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as c:
        result = push_bin(
            client=c,
            bin_path=bin_path,
            url="http://srv:8000/v3/pendant-upload-data-ordered",
            peripheral_id="AA:BB",
        )
    assert result == {"batch_id": 7, "bytes": 8}
    assert captured["body"] == b"BINBYTES"
    assert captured["query"]["blePeripheralDeviceId"] == "AA:BB"


def test_push_bin_raises_on_4xx(tmp_path: Path) -> None:
    bin_path = tmp_path / "x.bin"
    bin_path.write_bytes(b"BINBYTES")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "bad"})

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as c:
        with pytest.raises(httpx.HTTPStatusError):
            push_bin(
                client=c, bin_path=bin_path,
                url="http://srv/v3/pendant-upload-data-ordered",
                peripheral_id="AA:BB",
            )
