"""GPIO lines through the kernel character device, addressed by chip LABEL + line.

Pure Python: chip and line information comes from two ioctls, line I/O from
``python-periphery`` (also pure Python — nothing compiles on any board or
architecture). Everything that touches the kernel is injectable, so the rules
here are tested on a machine with no GPIO at all.
"""

from __future__ import annotations

import glob
import logging
import os
import struct
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# <linux/gpio.h>
GPIO_GET_CHIPINFO_IOCTL = 0x8044B401      # struct gpiochip_info {char name[32]; char label[32]; __u32 lines;}
GPIO_GET_LINEINFO_IOCTL = 0xC048B402      # v1: {__u32 offset; __u32 flags; char name[32]; char consumer[32];}
GPIO_V2_GET_LINEINFO_IOCTL = 0xC100B405   # v2: {char name[32]; char consumer[32]; __u32 offset; __u32 num_attrs; __u64 flags; …}
_LINE_USED = 0x1                          # GPIOLINE_FLAG_KERNEL == GPIO_V2_LINE_FLAG_USED


@dataclass
class ChipInfo:
    path: str
    name: str
    label: str
    lines: int
    used_lines: list[int] = field(default_factory=list)
    line_names: list[str] = field(default_factory=list)


def _cstr(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("utf-8", "replace")


def _default_ioctl(path: str, request: int, buf: bytearray) -> None:
    import fcntl  # noqa: PLC0415 — not available on every platform the tests run on
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        fcntl.ioctl(fd, request, buf, True)
    finally:
        os.close(fd)


def read_chip(path: str, ioctl: Callable[[str, int, bytearray], None] = _default_ioctl, with_lines: bool = True) -> ChipInfo:
    """Name, label and line count of one chip; per line, its name and whether the kernel holds it."""
    buf = bytearray(68)
    ioctl(path, GPIO_GET_CHIPINFO_IOCTL, buf)
    name, label, lines = _cstr(buf[0:32]), _cstr(buf[32:64]), struct.unpack_from("=I", buf, 64)[0]
    chip = ChipInfo(path=path, name=name, label=label or name, lines=int(lines))
    if not with_lines:
        return chip
    names: list[str] = []
    for offset in range(chip.lines):
        used, line_name = False, ""
        try:
            v2 = bytearray(256)
            struct.pack_into("=I", v2, 64, offset)
            ioctl(path, GPIO_V2_GET_LINEINFO_IOCTL, v2)
            line_name = _cstr(v2[0:32])
            used = bool(struct.unpack_from("=Q", v2, 72)[0] & _LINE_USED)
        except OSError:
            try:
                v1 = bytearray(72)
                struct.pack_into("=I", v1, 0, offset)
                ioctl(path, GPIO_GET_LINEINFO_IOCTL, v1)
                used = bool(struct.unpack_from("=I", v1, 4)[0] & _LINE_USED)
                line_name = _cstr(v1[8:40])
            except OSError as e:
                logger.debug("line info %s:%d unavailable: %s", path, offset, e)
        names.append(line_name)
        if used:
            chip.used_lines.append(offset)
    if any(names):
        chip.line_names = names
    return chip


def list_chips(dev_glob: str = "/dev/gpiochip*", ioctl: Callable[[str, int, bytearray], None] = _default_ioctl,
               with_lines: bool = True) -> list[ChipInfo]:
    chips: list[ChipInfo] = []
    seen: set[str] = set()
    for path in sorted(glob.glob(dev_glob), key=lambda p: (len(p), p)):
        # Raspberry Pi OS keeps /dev/gpiochip4 as a LINK to gpiochip0 for older software: the
        # same chip under two names (the first real board reported it twice, 2026-09-20).
        real = os.path.realpath(path)
        if real in seen:
            continue
        seen.add(real)
        try:
            chips.append(read_chip(path, ioctl, with_lines))
        except OSError as e:
            logger.debug("gpiochip %s unreadable: %s", path, e)
    return chips


class PortUnavailable(Exception):
    """The port cannot be opened on this node: no such chip label, no such line, or the kernel holds it."""


class LineIO:
    """Open lines by ``(chip label, line)`` and keep them open: one request per line for the life of the agent.

    ``opener(chip_path, line, direction)`` returns an object with ``read() -> bool``,
    ``write(bool)`` and ``close()`` — ``periphery.GPIO`` in production, a fake in tests.
    """

    def __init__(self, chips: Optional[Callable[[], list[ChipInfo]]] = None,
                 opener: Optional[Callable[[str, int, str], Any]] = None):
        self._chips = chips or (lambda: list_chips(with_lines=False))
        self._opener = opener or self._periphery_open
        self._paths: dict[str, str] = {}
        self._lines: dict[tuple[str, int], tuple[str, Any]] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _periphery_open(chip_path: str, line: int, direction: str):
        from periphery import GPIO  # noqa: PLC0415 — pure Python, installed by the join script
        return GPIO(chip_path, int(line), direction)

    def chip_path(self, label: str) -> str:
        with self._lock:
            if label not in self._paths:
                self._paths = {c.label: c.path for c in self._chips()}
            path = self._paths.get(label)
        if not path:
            raise PortUnavailable(f"this node has no GPIO chip labelled {label}")
        return path

    def has(self, label: str) -> bool:
        try:
            self.chip_path(label)
            return True
        except PortUnavailable:
            return False

    def _line(self, label: str, line: int, direction: str):
        key = (label, int(line))
        with self._lock:
            held = self._lines.get(key)
            if held and held[0] == direction:
                return held[1]
            if held:
                held[1].close()
                del self._lines[key]
            try:
                handle = self._opener(self.chip_path(label), int(line), direction)
            except PortUnavailable:
                raise
            except Exception as e:  # noqa: BLE001 — EBUSY (held by the kernel or another process), EINVAL (no such line)
                raise PortUnavailable(f"{label} line {line} cannot be opened as {direction}: {e}") from e
            self._lines[key] = (direction, handle)
            return handle

    def write(self, label: str, line: int, value: bool, active_low: bool = False) -> None:
        level = (not bool(value)) if active_low else bool(value)
        self._line(label, line, "out").write(level)

    def read(self, label: str, line: int, active_low: bool = False, pull: Optional[str] = None) -> bool:
        level = bool(self._line(label, line, "in").read())
        return (not level) if active_low else level

    def release(self, label: str, line: int) -> None:
        with self._lock:
            held = self._lines.pop((label, int(line)), None)
        if held:
            try:
                held[1].close()
            except Exception as e:  # noqa: BLE001
                logger.debug("closing %s:%d: %s", label, line, e)

    def close(self) -> None:
        with self._lock:
            for _, handle in self._lines.values():
                try:
                    handle.close()
                except Exception:  # noqa: BLE001
                    pass
            self._lines.clear()
