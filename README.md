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

`pendant --help` is the source of truth for flags; canonical workflows are
below. Addresses are placeholder MACs.

### Capture

```bash
pendant scan                                       # find a pendant in range
pendant info DE:AD:BE:EF:00:00                     # battery, fw, flash range, pubkey

# Sync + decode into per-recording .opus + sidecars in one shot:
pendant sync DE:AD:BE:EF:00:00 -o today.opus

# Variants when the device is in encrypted mode:
pendant sync DE:AD:BE:EF:00:00 -o today.opus --push-key             # new keypair
pendant sync DE:AD:BE:EF:00:00 -o today.opus --key today.opus.key.pem  # existing

# Or split the steps — capture wire-fidelity .bin, decode later:
pendant capture DE:AD:BE:EF:00:00 -o today.bin
pendant decode  today.bin -o today.opus
```

`sync` produces both a wire-fidelity `<base>.bin` AND per-recording
`<base>_rec<NNNN>.opus` files plus `<base>.manifest.json` /
`<base>.events.json` sidecars. `capture` writes only the `.bin`. `decode`
goes the other way — `.bin` to per-recording `.opus`.

### Server upload (optional)

Only needed if you also run a pendant-server alongside this client:

```bash
pendant push today.bin --url http://localhost:8000/v3/pendant-upload-data-ordered
pendant sync ... -o today.opus --upload http://localhost:8000/v3/pendant-upload-data-ordered
pendant enroll-voice DE:AD:BE:EF:00:00 --url http://localhost:8000 --wearer
pendant enroll-voice DE:AD:BE:EF:00:00 --url http://localhost:8000 --name "Sarah"
```

### Diagnostics

```bash
pendant inspect  DE:AD:BE:EF:00:00 -n 5            # raw flash_page hex + entropy
pendant debug    DE:AD:BE:EF:00:00                 # GATT dump + notification trace
pendant set-time DE:AD:BE:EF:00:00                 # push host clock to pendant
```

### First-time pairing (and used pendants)

The pendant accepts **one bond at a time**. A pendant that's never been
used just triggers your OS's pairing dialog on the first connection, and
`bleak` handles the rest:

```bash
pendant pair DE:AD:BE:EF:00:00                     # only if the OS doesn't auto-pair
```

If your pendant was previously bonded — to the official Limitless app, a
different machine, or a previous owner — that bond must be cleared on the
**pendant side** first, otherwise GATT writes are silently dropped:

- If you can still reach the previous app, factory-reset from there.
- Otherwise, long-press the side button per Limitless's documentation to
  factory-reset the device itself.

```bash
pendant unpair DE:AD:BE:EF:00:00                   # ask a paired pendant to drop its bond
```

`unpair` requires the link to *already* be authenticated, so it's the
clean-up-before-retire command, not the recover-from-strange-state one.

### Resetting the pendant

Three levels, ordered from least to most destructive:

| Command | Effect | Preserves |
|---|---|---|
| `clear-storage` | Erases the flash log (audio recordings) | Bond, RAM flags incl. `audio_encryption_enabled` |
| `reset`         | Soft reboot                              | Flash log; clears RAM-only state |
| `factory-reset` | Wipe flash + reboot + clear bond keys    | Nothing — you'll need to re-pair |

Most users hitting an unwanted encryption state want **`reset`**, not
`factory-reset`. The `audio_encryption_enabled` flag is RAM-only — it
survives `clear-storage` but is cleared on any reboot. `reset` flips it
back without losing existing recordings or having to re-pair.

```bash
pendant clear-storage DE:AD:BE:EF:00:00
pendant reset         DE:AD:BE:EF:00:00
pendant factory-reset DE:AD:BE:EF:00:00            # destructive
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
| `FactoryResetPendant` (case 10) | ✓ | Wired via `pendant factory-reset` |
| `ResetDevice` (case 11) | ✓ | Wired via `pendant reset` (RAM-only reboot) |
| `UnpairBluetooth` (case 15) | ✓ | Wired via `pendant unpair` |
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
