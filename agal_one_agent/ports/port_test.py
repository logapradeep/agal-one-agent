"""testPort — the commissioning check of ONE port (functions.yaml 1.6.0).

The command carries the port's own transport, so a port can be tested before any
card or program exists: the first check on a new board is an LED on DIO1.
``pulse`` switches an output on for a moment and off again; ``read`` watches an
input. The answer goes back as the ``port_test`` ingress message.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from .gpiochip import LineIO, PortUnavailable

logger = logging.getLogger(__name__)
MAX_PULSE_S = 5
MAX_READ_S = 30


def run_port_test(command: dict, lines: LineIO, busy: Optional[Callable[[str, int], bool]] = None,
                  sleep: Callable[[float], None] = time.sleep) -> dict:
    """Returns {portId, result: passed|failed, detail?, value?}. Never raises."""
    port_id = str(command.get("portId") or "")
    action = command.get("action")
    transport = command.get("transport") or {}
    out: dict = {"portId": port_id}
    try:
        if transport.get("kind") != "gpio" or not transport.get("chip") or transport.get("line") is None:
            return {**out, "result": "failed",
                    "detail": f"this agent cannot test a {transport.get('kind', 'unknown')} port yet — header pins only"}
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
