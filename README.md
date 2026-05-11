# Limitless Pendant client

Self-hosted client for the Limitless Pendant (FCC ID `2BL99-LSPT01`).
Connects to the device over BLE, drains its flash log of recorded audio
fragments, optionally decrypts them, and writes a streamable Ogg Opus
file you can play in any Opus-aware player.

Built from static analysis of the official Android app and the firmware
v1.1.20 image.

## Status

Parsers + crypto are unit-tested with synthetic data and the full
capture/decode pipeline has been exercised against a real pendant
running firmware v1.1.20. Platform variance to watch out for:

- BLE bonding requirements on your specific OS (`bleak` handles most of
  this but the platform behaviour varies)
- The `idle_timeout` for `download` may need tuning for slow flash drain

## Install

```bash
cd client
python -m venv .venv
.venv/Scripts/activate              # Windows; macOS/Linux: source .venv/bin/activate
pip install -e ".[dev,opus]"
python scripts/gen_proto.py         # generate proto bindings into src/pendant_client/proto/
pytest                              # offline tests should pass
```

## Usage

```bash
# Find a pendant in the area
pendant scan

# Print info (battery, fw, flash page range, encryption pubkey)
pendant info DE:AD:BE:EF:00:00

# Sync everything as Ogg Opus, no encryption (pendant must be in plaintext mode)
pendant sync DE:AD:BE:EF:00:00 -o today.opus

# Sync with a fresh keypair: pushes pubkey to pendant, saves priv key alongside
pendant sync DE:AD:BE:EF:00:00 -o today.opus --push-key

# Sync using an existing private key from a previous run
pendant sync DE:AD:BE:EF:00:00 -o today.opus --key today.opus.key.pem

# Wipe flash on the device (factory-reset of storage)
pendant clear-storage DE:AD:BE:EF:00:00
```

## Project layout

```
client/
├── proto/                          # source .proto files (extracted from the APK)
├── scripts/gen_proto.py            # generate Python bindings into src/.../proto/
├── src/pendant_client/
│   ├── __init__.py                 # UUIDs + MTU constants
│   ├── ble.py                      # BLE transport: connect, fragment, reassemble
│   ├── flash_page.py               # generic protobuf wire-format parser
│   ├── crypto.py                   # ECDH-P256 + HKDF-SHA256 + AES-GCM-128
│   ├── session.py                  # high-level: get_info, set_time, sync, …
│   ├── cli.py                      # `pendant ...` command-line entry point
│   └── proto/                      # auto-generated, .gitignore'd
└── tests/
    ├── test_fragment_reassembly.py
    ├── test_crypto_round_trip.py
    └── test_flash_page_parser.py
```

## What this client implements

| Pendant feature | Supported | Notes |
|---|---|---|
| BLE pair + connect | ✓ | Cross-platform via `bleak` |
| MTU exchange (498) | ✓ | Falls back gracefully if OS can't negotiate |
| Fragment reassembly | ✓ | `BLEMessageFromNativeToPendant`/`...ToNative` proto wrapper |
| `GetDeviceInfo` (case 14) | ✓ | Returns serial, fw, battery, flash range, pubkey |
| `GetDeviceStatus` (case 21) | ✓ | |
| `SetCurrentTime` (case 6) | ✓ | |
| `SetServerPublicKey` (case 28) | ✓ | Push the keypair you generated |
| `StartRecording` / `StopRecording` (17/18) | ✓ | |
| `DownloadFlashPages` (case 8) batch mode | ✓ | The main "give me audio" command |
| `DeleteFlashPage` (case 7) | ✓ | Auto-issued after each chunk |
| `ClearPendantStorage` (case 25) | ✓ | Factory-reset of flash log |
| `FactoryResetPendant` (case 10) | not exposed | Easy to add — see `session.py` |
| WiFi sync mode | not implemented | Doesn't exist in firmware v1.1.20 |
| Real-time mode (BLE_REALTIME) | not implemented | Inert in firmware v1.1.20 |
| Vapi voice-agent feature | out of scope | Lives in the phone app, not the device |

## What you'll need a real device for

The unit tests cover everything that can be checked offline, but the
following can only be verified against a paired pendant:

1. **BLE pairing flow.** macOS/Linux/Windows handle bonding differently;
   `bleak` smooths most of it but the first connection may need an OS
   pairing dialog.
2. **Inner-submessage field numbers.** The `flash_page.py` parser uses
   a content heuristic (12-byte field => nonce, 16-byte field => tag,
   biggest length-delimited field => ciphertext). Confirm against a
   real pendant chunk. If the firmware ever produces a chunk where the
   ciphertext is exactly 12 or 16 bytes the heuristic mis-classifies;
   in practice ciphertext is hundreds of bytes.
3. **`download()` idle timeout.** The default 5 seconds is a guess —
   tune via `--idle` once you see how fast pages stream.

## Why this is a small surface

The pendant only speaks BLE. It has no WiFi client, no HTTP client, no
TLS, no socket layer in firmware v1.1.20 — every WiFi-related proto
handler is inert. All you need is BLE + the proto bindings, hence the
small code size.

## License

MIT.
