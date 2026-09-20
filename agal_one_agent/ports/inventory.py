"""What the board IS and what it HAS — the hardware report (functions.yaml 1.6.0 reportHardware).

Shape: iot-node.schema.json#/properties/hardware. Every reader takes ``root`` so the
whole report is tested against a fake filesystem; nothing here knows a board by name.
"""

from __future__ import annotations

import glob
import logging
import os
import platform
import re
from typing import Callable, Optional

from .gpiochip import ChipInfo, list_chips

logger = logging.getLogger(__name__)


def _read(path: str, limit: int = 4096) -> str:
    try:
        with open(path, "rb") as f:
            return f.read(limit).replace(b"\0", b"").decode("utf-8", "replace").strip()
    except OSError:
        return ""


def _p(root: str, path: str) -> str:
    return os.path.join(root, path.lstrip("/")) if root != "/" else path


def identity(root: str = "/", agent_version: str = "", installer_version: str = "") -> dict:
    cpuinfo = _read(_p(root, "/proc/cpuinfo"), 65536)
    field = lambda name: (re.search(rf"^{name}\s*:\s*(.+)$", cpuinfo, re.M) or [None, ""])[1].strip()  # noqa: E731
    model = _read(_p(root, "/proc/device-tree/model")) or field("Model") or _read(_p(root, "/sys/class/dmi/id/product_name"))
    serial = _read(_p(root, "/proc/device-tree/serial-number")) or field("Serial") or _read(_p(root, "/etc/machine-id"))
    compatible = _read(_p(root, "/proc/device-tree/compatible"))
    soc = field("Hardware") or (compatible.split(",")[-1] if compatible else "")
    osr = dict(re.findall(r'^([A-Z_]+)="?([^"\n]*)"?$', _read(_p(root, "/etc/os-release"), 8192), re.M))
    mem = re.search(r"^MemTotal:\s+(\d+)\s*kB", _read(_p(root, "/proc/meminfo"), 8192), re.M)
    out: dict = {
        "model": model[:120], "serial": serial[:64], "soc": soc[:64], "arch": platform.machine()[:16],
        "kernel": platform.release()[:64], "python": platform.python_version()[:16],
    }
    os_info = {"id": osr.get("ID", "")[:32], "version": osr.get("VERSION_ID", "")[:32], "prettyName": osr.get("PRETTY_NAME", "")[:120]}
    if any(os_info.values()):
        out["os"] = {k: v for k, v in os_info.items() if v}
    if mem:
        out["memoryMb"] = int(mem.group(1)) // 1024
    if agent_version:
        out["agentVersion"] = agent_version[:32]
    if installer_version:
        out["installerVersion"] = installer_version[:32]
    net = default_interface(root)
    if net:
        out["network"] = net
    return {k: v for k, v in out.items() if v not in ("", None)}


def default_interface(root: str = "/") -> Optional[dict]:
    """The interface of the default route, and what kind of link it is."""
    for line in _read(_p(root, "/proc/net/route"), 65536).splitlines()[1:]:
        cols = line.split()
        if len(cols) > 2 and cols[1] == "00000000":
            name = cols[0]
            kind = ("wifi" if name.startswith("wl") else
                    "ethernet" if name.startswith(("eth", "en")) else
                    "cellular" if name.startswith(("usb", "wwan", "rmnet", "ppp")) else "other")
            return {"interface": name[:24], "kind": kind}
    return None


def inventory(root: str = "/", chips: Optional[Callable[[], list[ChipInfo]]] = None,
              i2c_scan: Optional[Callable[[str], Optional[list[int]]]] = None) -> dict:
    g = lambda pattern: sorted(glob.glob(_p(root, pattern)))  # noqa: E731
    strip = (lambda path: path[len(root.rstrip("/")):] if root != "/" else path)
    found = chips() if chips else list_chips()
    inv: dict = {"gpioChips": [
        {"label": c.label, "lines": c.lines, "usedLines": c.used_lines, **({"lineNames": c.line_names} if c.line_names else {})}
        for c in found if c.lines > 0
    ]}
    i2c = []
    for dev in g("/dev/i2c-*"):
        path = strip(dev)
        entry: dict = {"device": path}
        m = re.search(r"(\d+)$", path)
        if m:
            entry["busId"] = int(m.group(1))
        if i2c_scan:
            answered = i2c_scan(dev)
            if answered is not None:
                entry["addresses"] = answered
        i2c.append(entry)
    inv["i2c"] = i2c
    spi = []
    for dev in g("/dev/spidev*"):
        m = re.search(r"spidev(\d+)\.(\d+)$", dev)
        spi.append({"device": strip(dev), **({"busId": int(m.group(1)), "cs": int(m.group(2))} if m else {})})
    inv["spi"] = spi
    by_id = {os.path.realpath(p): strip(p) for p in g("/dev/serial/by-id/*")}
    serial = []
    for pattern in ("/dev/ttyUSB*", "/dev/ttyACM*", "/dev/ttyAMA*", "/dev/ttyS0", "/dev/ttyHS*", "/dev/serial0"):
        for dev in g(pattern):
            entry = {"device": strip(dev)}
            if os.path.realpath(dev) in by_id:
                entry["byId"] = by_id[os.path.realpath(dev)]
            serial.append(entry)
    inv["serial"] = serial[:16]
    inv["rtc"] = [{"device": strip(d), **({"name": n} if (n := _read(_p(root, f"/sys/class/rtc/{os.path.basename(d)}/name"))) else {})}
                  for d in g("/dev/rtc[0-9]*")][:4]
    adc = []
    for dev in g("/sys/bus/iio/devices/iio:device*"):
        channels = len(glob.glob(os.path.join(dev, "in_voltage*_raw")))
        if channels:
            adc.append({"device": strip(dev), "name": _read(os.path.join(dev, "name"))[:64], "channels": channels})
    inv["adc"] = adc[:8]
    return inv


def scan_i2c(device: str) -> Optional[list[int]]:
    """7-bit addresses that answer on one I2C bus. None when the bus cannot be opened.

    Reads one byte from each address in 0x08–0x77 (what ``i2cdetect -r`` does); an address a
    kernel driver holds answers EBUSY and is reported as present.
    """
    m = re.search(r"(\d+)$", device)
    if not m:
        return None
    try:
        from smbus2 import SMBus  # noqa: PLC0415
    except ImportError:
        return None
    answered: list[int] = []
    try:
        with SMBus(int(m.group(1))) as bus:
            for addr in range(0x08, 0x78):
                try:
                    bus.read_byte(addr)
                    answered.append(addr)
                except OSError as e:
                    if getattr(e, "errno", None) == 16:  # EBUSY: a kernel driver owns it
                        answered.append(addr)
    except OSError as e:
        logger.debug("i2c scan %s: %s", device, e)
        return None
    return answered


def hardware_report(agent_version: str, installer_version: str = "", root: str = "/", scan: bool = True) -> dict:
    report = identity(root, agent_version, installer_version)
    try:
        report["inventory"] = inventory(root, i2c_scan=scan_i2c if scan else None)
    except Exception as e:  # noqa: BLE001 — a partial report is better than none
        logger.warning("inventory failed: %s", e)
        report["inventory"] = {"gpioChips": []}
    return report
