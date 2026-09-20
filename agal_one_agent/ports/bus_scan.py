"""scanBus — what answers on a bus (functions.yaml 1.6.0). A scan only SUGGESTS.

I2C: every 7-bit address. RS-485: a short range of Modbus units, asked for one coil
each; ANY well-formed reply from the unit — data or an exception — means a device
is there. The answer goes back as the ``bus_scan`` ingress message; nothing is
added to the port table by a scan.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

from .inventory import scan_i2c

logger = logging.getLogger(__name__)


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def read_coils_request(unit: int, address: int = 0, count: int = 1) -> bytes:
    body = bytes([unit & 0xFF, 0x01, (address >> 8) & 0xFF, address & 0xFF, (count >> 8) & 0xFF, count & 0xFF])
    c = crc16(body)
    return body + bytes([c & 0xFF, (c >> 8) & 0xFF])


def is_reply_from(unit: int, frame: bytes) -> bool:
    """A reply (function 0x01) or an exception (0x81) from this unit, CRC intact."""
    if len(frame) < 5 or frame[0] != unit or frame[1] not in (0x01, 0x81):
        return False
    n = 5 if frame[1] == 0x81 else 3 + frame[2] + 2
    if len(frame) < n:
        return False
    c = crc16(frame[:n - 2])
    return frame[n - 2] == (c & 0xFF) and frame[n - 1] == ((c >> 8) & 0xFF)


def _open_serial(device: str, serial: dict):
    import serial as pyserial  # noqa: PLC0415
    parity = {"none": pyserial.PARITY_NONE, "even": pyserial.PARITY_EVEN, "odd": pyserial.PARITY_ODD}[serial.get("parity", "none")]
    return pyserial.Serial(device, baudrate=int(serial.get("baud", 9600)), bytesize=int(serial.get("dataBits", 8)),
                           parity=parity, stopbits=int(serial.get("stopBits", 1)), timeout=0.15)


def scan_modbus(device: str, serial: dict, first: int = 1, last: int = 16,
                opener: Callable[[str, dict], Any] = _open_serial, sleep: Callable[[float], None] = time.sleep) -> Optional[list[int]]:
    first, last = max(1, int(first)), min(247, int(last))
    try:
        port = opener(device, serial or {})
    except Exception as e:  # noqa: BLE001
        logger.warning("modbus scan: %s cannot be opened: %s", device, e)
        return None
    answered: list[int] = []
    try:
        for unit in range(first, last + 1):
            try:
                port.reset_input_buffer()
                port.write(read_coils_request(unit))
                if is_reply_from(unit, bytes(port.read(8))):
                    answered.append(unit)
            except Exception as e:  # noqa: BLE001
                logger.debug("modbus scan unit %d: %s", unit, e)
            sleep(0.02)  # 3.5 character times of silence between frames, generously
    finally:
        try:
            port.close()
        except Exception:  # noqa: BLE001
            pass
    return answered


def run_bus_scan(command: dict) -> dict:
    bus = command.get("bus") or {}
    bus_id, kind, device = str(bus.get("id") or ""), bus.get("kind"), bus.get("device")
    out: dict = {"busId": bus_id}
    if not device:
        return {**out, "answered": [], "error": "the bus names no device path"}
    if kind == "i2c":
        found = scan_i2c(str(device))
    elif kind in ("rs485", "uart"):
        out["from"], out["to"] = int(command.get("from") or 1), int(command.get("to") or 16)
        found = scan_modbus(str(device), bus.get("serial") or {}, out["from"], out["to"])
    else:
        return {**out, "answered": [], "error": f"a {kind} bus cannot be scanned"}
    if found is None:
        return {**out, "answered": [], "error": f"{device} could not be opened on this node"}
    return {**out, "answered": found}
