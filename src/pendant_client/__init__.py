"""Limitless Pendant client.

A self-hosted BLE client that connects to a Limitless Pendant
(FCC ID 2BL99-LSPT01) and downloads its recorded audio. Supports
firmware versions starting at v1.1.20.
"""

__version__ = "0.1.0"

# Limitless BLE service / characteristic UUIDs. Verified from the
# Android APK (`p550c.C6735a` constants class) and the GATT scan
# advertisement payload.
SERVICE_UUID         = "632de001-604c-446b-a80f-7963e950f3fb"
WRITE_CHAR_UUID      = "632de002-604c-446b-a80f-7963e950f3fb"  # phone -> pendant
NOTIFY_CHAR_UUID     = "632de003-604c-446b-a80f-7963e950f3fb"  # pendant -> phone

# Standard Battery Service.
BATTERY_SERVICE_UUID = "0000180f-0000-1000-8000-00805f9b34fb"
BATTERY_LEVEL_UUID   = "00002a19-0000-1000-8000-00805f9b34fb"

# MTU the official app negotiates after services discovered. Larger MTU
# means fewer BLE fragments per ServerCommandMsg / PendantAllMsg.
DESIRED_MTU = 498
