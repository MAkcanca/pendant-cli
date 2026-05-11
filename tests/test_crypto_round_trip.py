"""Verify our crypto round-trips against itself.

This tests:
- generate_keyset → produces valid AES key, server pub
- import_pendant_pubkey accepts uncompressed P-256 points
- decrypt_audio recovers plaintext from an EncryptedAudio payload

It does NOT test against a real firmware-generated chunk (that would
need a captured pendant transmission)."""
from __future__ import annotations

import os

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from pendant_client.crypto import (
    decrypt_audio,
    export_server_pubkey,
    generate_keyset,
    import_pendant_pubkey,
    keyset_from_existing,
)
from pendant_client.flash_page import AudioPayload, EncryptedAudio


def _make_pendant_pubkey() -> bytes:
    """Generate a fake pendant pubkey for tests."""
    priv = ec.generate_private_key(ec.SECP256R1())
    return export_server_pubkey(priv)


def test_generate_keyset_produces_16_byte_key():
    pendant_pub = _make_pendant_pubkey()
    keys = generate_keyset(pendant_pub)
    assert len(keys.aes_key) == 16
    assert len(keys.server_pub_bytes) == 65
    assert keys.server_pub_bytes[0] == 0x04


def test_decrypt_audio_round_trip():
    pendant_pub = _make_pendant_pubkey()
    keys = generate_keyset(pendant_pub)

    # Pretend we're the firmware: encrypt some plaintext with the AES key.
    plaintext = b"sample opus packet bytes" * 6
    nonce = os.urandom(12)
    aead = AESGCM(keys.aes_key)
    ciphertext_with_tag = aead.encrypt(nonce, plaintext, associated_data=None)
    # AESGCM appends the 16-byte tag to ciphertext; firmware stores them
    # as separate proto fields (EncryptedBytes.ciphertext / .authentication_tag).
    cipher = ciphertext_with_tag[:-16]
    tag = ciphertext_with_tag[-16:]

    audio = AudioPayload(
        encrypted=EncryptedAudio(
            nonce=nonce,
            ciphertext=cipher,
            authentication_tag=tag,
        ),
    )
    recovered = decrypt_audio(audio, keys.aes_key)
    assert recovered == plaintext


def test_decrypt_audio_passthrough_when_unencrypted():
    audio = AudioPayload(opus_packets=b"\x00\x01\x02opus_bytes")
    # No EncryptedAudio attached → returned verbatim, no key needed.
    out = decrypt_audio(audio, aes_key=b"")
    assert out == b"\x00\x01\x02opus_bytes"


def test_import_pendant_pubkey_validates_format():
    import pytest
    with pytest.raises(ValueError):
        import_pendant_pubkey(b"\x00" * 65)        # not 0x04 prefix
    with pytest.raises(ValueError):
        import_pendant_pubkey(b"\x04" * 33)        # wrong length
    # but a real one should work:
    p = _make_pendant_pubkey()
    import_pendant_pubkey(p)


def test_keyset_from_existing_round_trip():
    """Persist a server private key, re-derive the AES key, confirm equal."""
    from cryptography.hazmat.primitives import serialization

    pendant_pub = _make_pendant_pubkey()
    keys1 = generate_keyset(pendant_pub)
    pem = keys1.server_priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    keys2 = keyset_from_existing(pem, pendant_pub)
    assert keys1.aes_key == keys2.aes_key
