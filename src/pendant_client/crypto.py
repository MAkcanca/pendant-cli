"""Audio encryption / decryption.

Mirrors the pendant-side encryption pipeline:

- ECC P-256 keypair on each side
- Shared secret = ECDH(server_priv, pendant_pub) = ECDH(pendant_priv, server_pub)
- AES-128 key = HKDF-SHA256(shared, salt=None, info=None, length=16)
- Each chunk encrypted with AES-GCM-128, 12-byte nonce, 16-byte tag, no AAD

PSA constants verified in the firmware:
- DAT_0000d68c = 0x09020109 = PSA_ALG_KEY_AGREEMENT(ECDH, HKDF(SHA-256))
- DAT_0000d694 = 0x05500200 = PSA_ALG_GCM
- DAT_0000d698 = 0x00802400 = AES-128

This module does NOT push the server pubkey to the pendant — that's done
via a `ServerCommandMsg.set_audio_encryption_server_public_key` message.
See `session.py`.

For a self-hosted client you can skip encryption entirely by NEVER
calling `set_server_public_key()` on a fresh pendant: the
`audio_encryption_enabled` flag stays at 0 and the device writes
plaintext Opus to flash. (Decryption code below still works either
way — `decrypt_chunk` returns the chunk's ciphertext bytes verbatim if
the chunk has no nonce/tag.)
"""
from __future__ import annotations

from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .flash_page import AudioPayload


# ----- key import / derivation -----

def import_pendant_pubkey(uncompressed_65_bytes: bytes) -> ec.EllipticCurvePublicKey:
    """Import a P-256 public key from the 65-byte uncompressed form
    (`0x04 || X || Y`) returned in `DeviceInfoMsg.device_audio_encryption_pub_key`."""
    if len(uncompressed_65_bytes) != 65 or uncompressed_65_bytes[0] != 0x04:
        raise ValueError(
            f"expected 65-byte uncompressed P-256 point starting with 0x04, "
            f"got {len(uncompressed_65_bytes)} bytes")
    return ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), uncompressed_65_bytes)


def export_server_pubkey(server_priv: ec.EllipticCurvePrivateKey) -> bytes:
    """Export the server public key as 65-byte uncompressed form, suitable
    for sending in `SetServerPublicKey.server_pub_key`."""
    pub = server_priv.public_key()
    return pub.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )


@dataclass(frozen=True)
class AudioKeySet:
    """Keypair + derived AES key for one pendant. The AES key is what the
    official backend stores per-device, and what's used to decrypt every
    audio chunk for that pendant for the lifetime of the pairing."""
    server_priv: ec.EllipticCurvePrivateKey
    server_pub_bytes: bytes
    pendant_pub_bytes: bytes
    aes_key: bytes  # 16 bytes


def generate_keyset(pendant_pub_bytes: bytes) -> AudioKeySet:
    """Generate a fresh server keypair and derive the audio AES key
    given the pendant's public key. Mirrors
    `_deriveKeysForAudioEncryption` in the official JS client."""
    pendant_pub = import_pendant_pubkey(pendant_pub_bytes)
    server_priv = ec.generate_private_key(ec.SECP256R1())
    shared = server_priv.exchange(ec.ECDH(), pendant_pub)
    aes_key = HKDF(
        algorithm=hashes.SHA256(),
        length=16,
        salt=None,
        info=None,
    ).derive(shared)
    return AudioKeySet(
        server_priv=server_priv,
        server_pub_bytes=export_server_pubkey(server_priv),
        pendant_pub_bytes=pendant_pub_bytes,
        aes_key=aes_key,
    )


def keyset_from_existing(server_priv_pem: bytes,
                         pendant_pub_bytes: bytes) -> AudioKeySet:
    """Re-derive the AES key from an existing server keypair (e.g. one
    you persisted between sessions)."""
    server_priv = serialization.load_pem_private_key(server_priv_pem, password=None)
    if not isinstance(server_priv, ec.EllipticCurvePrivateKey):
        raise TypeError("expected EC private key")
    pendant_pub = import_pendant_pubkey(pendant_pub_bytes)
    shared = server_priv.exchange(ec.ECDH(), pendant_pub)
    aes_key = HKDF(
        algorithm=hashes.SHA256(),
        length=16,
        salt=None,
        info=None,
    ).derive(shared)
    return AudioKeySet(
        server_priv=server_priv,
        server_pub_bytes=export_server_pubkey(server_priv),
        pendant_pub_bytes=pendant_pub_bytes,
        aes_key=aes_key,
    )


# ----- chunk decryption -----

def decrypt_audio(audio: AudioPayload, aes_key: bytes) -> bytes:
    """Return the Opus packet bytes for one audio chunk.

    If the chunk has an `encrypted_codec_beamforming_data` (per the
    `EncryptedBytes` proto: nonce / ciphertext / authentication_tag),
    AES-GCM-128 decrypt it. Otherwise return the existing
    `opus_packets` verbatim.
    """
    enc = audio.encrypted
    if enc is None:
        return audio.opus_packets
    if not aes_key:
        raise ValueError(
            "chunk has encrypted_codec_beamforming_data but no AES key "
            "was provided")
    if len(aes_key) != 16:
        raise ValueError(f"expected 16-byte AES-128 key, got {len(aes_key)}")
    if len(enc.nonce) != 12:
        raise ValueError(
            f"expected 12-byte nonce in EncryptedBytes, got {len(enc.nonce)}")
    if len(enc.authentication_tag) != 16:
        raise ValueError(
            f"expected 16-byte authentication_tag in EncryptedBytes, "
            f"got {len(enc.authentication_tag)}")
    return AESGCM(aes_key).decrypt(
        nonce=enc.nonce,
        data=enc.ciphertext + enc.authentication_tag,
        associated_data=None,
    )
