"""Primary network MAC detection for node registration.

The MAC is reported to the cloud on heartbeat and stored on the IoTNode for
registry / ops lookup + duplicate detection. It is NOT an auth boundary
(hardwareSerial is the binding anchor) — RPi MACs are software-mutable — so this
is best-effort and must never block the daemon.
"""
import logging
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Preference order: wired first, then wifi, then common USB/alt names.
_PREFERRED = ("eth0", "wlan0", "end0", "eth1", "enp0s3", "usb0")
_SYS_NET = Path("/sys/class/net")

_cached: Optional[str] = None  # None = not probed; "" = probed, none found


def _read_iface_mac(iface: str) -> Optional[str]:
    try:
        addr = (_SYS_NET / iface / "address").read_text().strip().lower()
        if addr and addr != "00:00:00:00:00:00":
            return addr
    except Exception:  # noqa: BLE001
        return None
    return None


def _from_uuid_getnode() -> Optional[str]:
    """Fallback for non-Linux / containers without /sys. uuid.getnode() sets the
    multicast bit when it had to RANDOMIZE (no real MAC) — treat that as a miss."""
    try:
        node = uuid.getnode()
        if (node >> 40) & 0x1:
            return None
        return ":".join(f"{(node >> (8 * i)) & 0xff:02x}" for i in reversed(range(6)))
    except Exception:  # noqa: BLE001
        return None


def get_primary_mac() -> Optional[str]:
    """Best-effort primary MAC (lowercase colon-separated), cached for process life."""
    global _cached
    if _cached is not None:
        return _cached or None

    mac: Optional[str] = None
    for iface in _PREFERRED:
        mac = _read_iface_mac(iface)
        if mac:
            break
    if not mac and _SYS_NET.is_dir():
        for path in sorted(_SYS_NET.iterdir()):
            if path.name == "lo":
                continue
            mac = _read_iface_mac(path.name)
            if mac:
                break
    if not mac:
        mac = _from_uuid_getnode()

    _cached = mac or ""  # cache misses too, so we scan /sys at most once
    if not mac:
        logger.warning("net_info: could not determine a primary MAC")
    return mac
