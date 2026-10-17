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


class BiasNotApplied(PortUnavailable):
    """The input line is open, but without the bias its port asks for: the kernel or the library cannot set it.
    Raised by :meth:`LineIO.read` only to a caller that needs the bias (``require_bias``) — a safety input,
    whose reading from a floating line is no reading."""


# A port's `pull` (asset.schema.json PortTransport, iot-node NodePort) → the line bias of python-periphery's
# character-device GPIO, which sets the line request's bias flag (GPIOHANDLE_REQUEST_BIAS_PULL_UP / _PULL_DOWN /
# _DISABLE, or the v2 GPIO_V2_LINE_FLAG_BIAS_*; the kernel has them since 5.5). `none` is stated wiring — an
# external resistor — so the internal pull is switched off; a port with no `pull` is requested with no bias flag
# (the line keeps whatever bias it has). programs/README.md §7.2.
PULL_TO_BIAS = {"up": "pull_up", "down": "pull_down", "none": "disable"}


@dataclass
class _Held:
    direction: str                  # "in" | "out"
    handle: Any
    pull: Optional[str] = None      # the pull it was requested with (inputs)
    bias_error: Optional[str] = None  # why that pull is NOT on the line (None: applied, or none asked for)


class LineIO:
    """Open lines by ``(chip label, line)`` and keep them open: one request per line for the life of the agent.

    ``opener(chip_path, line, direction)`` returns an object with ``read() -> bool``,
    ``write(bool)`` and ``close()`` — ``periphery.GPIO`` in production, a fake in tests.
    ``direction`` is ``"in"``, or ``"high"`` / ``"low"``: an output requested AT that level.
    Never ``"out"`` — python-periphery requests that LOW, and an active-low relay board
    (HW-281 class) reads LOW as ON: a relay click before the first write.

    An input whose port states a ``pull`` is requested with it as the line's bias: the opener is then
    called as ``opener(chip_path, line, "in", bias=<python-periphery bias>)`` (``pull_up``, ``pull_down``,
    ``disable``). When that request fails — a kernel older than 5.5, a library that has no bias, a chip
    that refuses it — the line is requested once more with no bias, a warning names the line, and the
    line remembers why (:meth:`bias_error`): a reading from it is a reading from a floating line.
    """

    def __init__(self, chips: Optional[Callable[[], list[ChipInfo]]] = None,
                 opener: Optional[Callable[..., Any]] = None):
        self._chips = chips or (lambda: list_chips(with_lines=False))
        self._opener = opener or self._periphery_open
        self._paths: dict[str, str] = {}
        self._lines: dict[tuple[str, int], _Held] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _periphery_open(chip_path: str, line: int, direction: str, bias: Optional[str] = None):
        from periphery import GPIO  # noqa: PLC0415 — pure Python, installed by the join script
        if bias is None:
            return GPIO(chip_path, int(line), direction)
        # python-periphery >= 2.1 (setup.py pins >= 2.4.1): three positional arguments select the character
        # device (CdevGPIO), whose `bias` is "default" | "pull_up" | "pull_down" | "disable". On a kernel older
        # than 5.5 it raises GPIOError before any request is made.
        return GPIO(chip_path, int(line), direction, bias=bias)

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

    def _line(self, label: str, line: int, direction: str, level: bool = False, pull: Optional[str] = None) -> _Held:
        """``direction`` is "in" or "out"; an output (re)opened here is requested at ``level``, an input with
        ``pull`` as its bias. An input already open is kept when no pull is asked for or the same one is; a
        different pull (a program that rewired the port) requests the line again with it."""
        key = (label, int(line))
        with self._lock:
            held = self._lines.get(key)
            if held and held.direction == direction and (direction != "in" or pull is None or held.pull == pull):
                return held
            if held:
                held.handle.close()
                del self._lines[key]
            path = self.chip_path(label)
            request = direction if direction == "in" else ("high" if level else "low")
            bias = PULL_TO_BIAS.get(pull) if (direction == "in" and pull is not None) else None
            bias_error: Optional[str] = None
            try:
                if direction == "in" and pull is not None and bias is None:
                    raise ValueError(f"unknown pull {pull!r} (up, down or none)")
                handle = self._opener(path, int(line), request) if bias is None else self._opener(path, int(line), request, bias=bias)
            except PortUnavailable:
                raise
            except Exception as e:  # noqa: BLE001 — EBUSY (held by the kernel or another process), EINVAL (no such line)
                if pull is None:
                    raise PortUnavailable(f"{label} line {line} cannot be opened as {direction}: {e}") from e
                # Was it the bias? Ask once more with none: a line that then opens is open WITHOUT the pull.
                try:
                    handle = self._opener(path, int(line), request)
                except Exception as e2:  # noqa: BLE001
                    raise PortUnavailable(f"{label} line {line} cannot be opened as {direction}: {e2}") from e2
                bias_error = f"{type(e).__name__}: {e}"[:160]
                logger.warning("line %d of %s: pull %s could not be applied (%s); the line is open with no bias "
                               "and may float", int(line), label, pull, bias_error)
            held = _Held(direction, handle, pull if direction == "in" else None, bias_error)
            self._lines[key] = held
            return held

    def write(self, label: str, line: int, value: bool, active_low: bool = False) -> None:
        level = (not bool(value)) if active_low else bool(value)
        # A line opened by this write starts at the level it is given — the first write after
        # start-up, after a port test let go of the line, or after it was read as an input.
        self._line(label, line, "out", level).handle.write(level)

    def read(self, label: str, line: int, active_low: bool = False, pull: Optional[str] = None,
             require_bias: bool = False) -> bool:
        """The line's logical level. ``pull`` (``up`` / ``down`` / ``none``) is the bias the line is requested
        with. ``require_bias``: raise :class:`BiasNotApplied` instead of reading a line the pull is not on."""
        held = self._line(label, line, "in", pull=pull)
        if require_bias and held.bias_error is not None:
            raise BiasNotApplied(f"line {int(line)} of {label}: pull {held.pull} could not be applied ({held.bias_error})")
        level = bool(held.handle.read())
        return (not level) if active_low else level

    def held_direction(self, label: str, line: int) -> Optional[str]:
        """``"out"`` or ``"in"`` while this agent holds the line requested that way; None while it holds no request for it.
        0.4.1 (contracts v1.15.4): a port test leaves a line as it found it — one held as an output (a line a reload dropped
        is kept driven at off) is written off and kept after the test, never let go of to its pull."""
        with self._lock:
            held = self._lines.get((label, int(line)))
        return held.direction if held is not None else None

    def bias_error(self, label: str, line: int) -> Optional[str]:
        """Why the pull an open input line was asked for is not on it; None when it is (or none was asked)."""
        with self._lock:
            held = self._lines.get((label, int(line)))
        return held.bias_error if held is not None else None

    def release(self, label: str, line: int) -> None:
        with self._lock:
            held = self._lines.pop((label, int(line)), None)
        if held:
            try:
                held.handle.close()
            except Exception as e:  # noqa: BLE001
                logger.debug("closing %s:%d: %s", label, line, e)

    def close(self) -> None:
        with self._lock:
            for held in self._lines.values():
                try:
                    held.handle.close()
                except Exception:  # noqa: BLE001
                    pass
            self._lines.clear()
