"""testPort — the commissioning check of ONE port (functions.yaml 1.6.0).

The command carries the port's own transport, so a port can be tested before any
card or program exists: the first check on a new board is one uncommitted DIO
(what is wired to it for the test is the installer's own — contracts v1.10.3).
``pulse`` switches an output on for a moment and off again; ``read`` watches an
input. The answer goes back as the ``port_test`` ingress message.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from .analog import scaled
from .gpiochip import LineIO, PortUnavailable

logger = logging.getLogger(__name__)
MAX_PULSE_S = 5
MAX_READ_S = 30


def _num(v: float) -> str:
    return f"{v:.3f}" if abs(v) < 10 else f"{v:.2f}" if abs(v) < 100 else f"{v:.1f}"


def _read_analog(command: dict, transport: dict, analog, sleep: Callable[[float], None]) -> dict:
    """Watch an AI port: what the program would see (in the port's unit) and the volts behind it."""
    unit = str(command.get("unit") or "").strip()
    seconds = max(1, min(MAX_READ_S, int(command.get("seconds") or 5)))
    values: list[float] = []
    volts: list[float] = []
    waited = 0.0
    while waited < seconds:
        v = analog.read_volts(transport, fresh=True)
        if v is not None:
            volts.append(v)
            values.append(scaled(v, transport.get("transform")))
        sleep(0.4)
        waited += 0.5  # a window (~0.1 s) plus the pause
    if not values:
        where = f"0x{int(transport.get('addr', 0)):02x} on I2C-{transport.get('busId', 1)}"
        return {"result": "failed", "detail": f"nothing answered at {where} — check the wiring and the address"}
    last = values[-1]
    u = f" {unit}" if unit else ""
    how = "AC RMS" if (transport.get("measure") or {}).get("mode") == "ac_rms" else "steady value"
    return {"result": "passed", "value": round(last, 4),
            "detail": f"{_num(last)}{u} now; {_num(min(values))}–{_num(max(values))}{u} over {seconds} s "
                      f"({how}, {_num(volts[-1])} V at the channel)"}


def run_port_test(command: dict, lines: LineIO, busy: Optional[Callable[[str, int], bool]] = None,
                  sleep: Callable[[float], None] = time.sleep, analog=None) -> dict:
    """Returns {portId, result: passed|failed, detail?, value?}. Never raises."""
    port_id = str(command.get("portId") or "")
    action = command.get("action")
    transport = command.get("transport") or {}
    out: dict = {"portId": port_id}
    try:
        if analog is not None and analog.supports(transport):
            if action != "read":
                return {**out, "result": "failed", "detail": "an analog input is watched, not pulsed"}
            return {**out, **_read_analog(command, transport, analog, sleep)}
        if transport.get("kind") != "gpio" or not transport.get("chip") or transport.get("line") is None:
            return {**out, "result": "failed",
                    "detail": f"this agent cannot test a {transport.get('driver') or transport.get('kind', 'unknown')} port yet — "
                              "header pins and ADS1115 channels only"}
        label, line = str(transport["chip"]), int(transport["line"])
        active_low = bool(transport.get("activeLow") or command.get("activeLow"))
        if busy and busy(label, line):
            return {**out, "result": "failed", "detail": "the running program is driving this port — pause automation first"}
        if action == "pulse":
            seconds = max(1, min(MAX_PULSE_S, int(command.get("seconds") or 2)))
            try:
                lines.write(label, line, True, active_low)
                sleep(seconds)
            finally:
                # Off, then let go of the line: a tested port is left exactly as it was found.
                try:
                    lines.write(label, line, False, active_low)
                finally:
                    lines.release(label, line)
            return {**out, "result": "passed", "detail": f"on for {seconds} s, then off"}
        if action == "read":
            seconds = max(1, min(MAX_READ_S, int(command.get("seconds") or 10)))
            first = lines.read(label, line, active_low)
            last, changes, waited = first, 0, 0.0
            while waited < seconds:
                sleep(0.1)
                waited += 0.1
                now = lines.read(label, line, active_low)
                if now != last:
                    changes += 1
                    last = now
            lines.release(label, line)
            return {**out, "result": "passed", "value": bool(last),
                    "detail": f"{'on' if last else 'off'} after {seconds} s; changed {changes} time(s)"}
        return {**out, "result": "failed", "detail": f"unknown action {action!r}"}
    except PortUnavailable as e:
        return {**out, "result": "failed", "detail": str(e)[:200]}
    except Exception as e:  # noqa: BLE001
        logger.exception("port test %s failed", port_id)
        return {**out, "result": "failed", "detail": f"{type(e).__name__}: {e}"[:200]}
