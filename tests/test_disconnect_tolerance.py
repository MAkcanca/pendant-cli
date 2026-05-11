"""Tests for the BLE disconnect-tolerance helpers in ble.py.

These cover the case where a sync completes successfully and writes
its .bin, but the BLE link drops during the cleanup commands (stop
batch download / stop notify / disconnect). Previously this surfaced
as a misleading 'operation canceled by the user' traceback that
masked an otherwise-successful run."""
from __future__ import annotations

import asyncio
import logging

import pytest
from bleak.exc import BleakError

from pendant_client.ble import (
    is_disconnect_exception,
    suppress_disconnect,
)


# ---------------------------------------------------------------------------
# is_disconnect_exception — classifier
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("exc", [
    BleakError("Not connected"),
    BleakError("not connected"),                       # case-insensitive
    OSError(-2147023673, "The operation was canceled by the user"),
    OSError("Operation was cancelled"),                # British
    BleakError("The peripheral is not connected"),     # macOS CoreBluetooth
    OSError("Software caused connection abort"),       # BlueZ
    BleakError("No such device"),
    BleakError("Object reference not set to an instance of an object"),
])
def test_recognises_disconnect_errors(exc):
    assert is_disconnect_exception(exc), \
        f"{type(exc).__name__}({exc!r}) should be recognised as a disconnect"


@pytest.mark.parametrize("exc", [
    BleakError("Insufficient authentication"),         # genuine pairing issue
    BleakError("GATT operation failed: 0x0e"),         # genuine GATT error
    ValueError("invalid input"),                       # not a BLE/OS error
    RuntimeError("foo"),
    asyncio.TimeoutError("idle"),
])
def test_does_not_recognise_real_errors(exc):
    assert not is_disconnect_exception(exc), \
        f"{type(exc).__name__}({exc!r}) should NOT be classified as disconnect"


def test_does_not_recognise_baseexception_subclasses():
    """KeyboardInterrupt / SystemExit / GeneratorExit must NEVER be
    classified as a disconnect, or suppress_disconnect would swallow
    them and break Ctrl+C / shutdown semantics."""
    for exc in (KeyboardInterrupt(), SystemExit(0), GeneratorExit()):
        assert not is_disconnect_exception(exc)


# ---------------------------------------------------------------------------
# suppress_disconnect — runtime behavior
# ---------------------------------------------------------------------------

async def _raises(exc):
    raise exc


async def _succeeds():
    return None


@pytest.mark.asyncio
async def test_suppresses_disconnect_with_info_log(caplog):
    caplog.set_level(logging.INFO, logger="pendant_client.ble")
    await suppress_disconnect(_raises(BleakError("Not connected")),
                              what="stop_notify")
    record = next(r for r in caplog.records if "Skipping stop_notify" in r.message)
    assert record.levelno == logging.INFO


@pytest.mark.asyncio
async def test_suppresses_non_disconnect_with_warning_log(caplog):
    """Non-disconnect errors during cleanup are still swallowed (to
    avoid masking the original exception that put us in cleanup)
    but logged at WARNING, not INFO."""
    caplog.set_level(logging.INFO, logger="pendant_client.ble")
    await suppress_disconnect(_raises(ValueError("something else")),
                              what="stop_notify")
    record = next(r for r in caplog.records
                  if "Cleanup step stop_notify failed" in r.message)
    assert record.levelno == logging.WARNING


@pytest.mark.asyncio
async def test_passes_through_on_success():
    """The happy path must not log anything."""
    # If this raises or hangs, the helper has a bug.
    await suppress_disconnect(_succeeds(), what="stop_notify")


@pytest.mark.asyncio
async def test_does_not_suppress_keyboard_interrupt():
    """Ctrl+C must propagate through cleanup."""
    with pytest.raises(KeyboardInterrupt):
        await suppress_disconnect(_raises(KeyboardInterrupt()),
                                  what="stop_notify")


@pytest.mark.asyncio
async def test_does_not_suppress_system_exit():
    """sys.exit() must propagate through cleanup."""
    with pytest.raises(SystemExit):
        await suppress_disconnect(_raises(SystemExit(2)),
                                  what="stop_notify")
