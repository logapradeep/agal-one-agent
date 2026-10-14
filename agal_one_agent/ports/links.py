"""Which card a port is linked to — the node's own check on a switch command (RBAC-01).

A card document names its node and its ports itself, so the cloud is not the only thing
that must check that a command for a card may move a port: the node checks it too. What the
node knows of its port table is what the cloud stamped into the running program — every port
a card's automation block reads or drives, with that card's ``assetId`` (the backend writes a
binding's transport from the node's port table, ADR-024). So:

* on a node that **runs a port table** (the cloud reported a ``portsVersion`` above 0, or the
  program names a port by chip label + line), ``setPower`` / ``setPortValue`` are carried out
  only for a port linked to the card the command names; anything else is refused with a
  failed ack that says why, and the legacy GPIO executor is never reached;
* ``testPort`` is refused when it names a card the port is not linked to; a free port is
  tested as before (the first check on a new board), and a linked port tested without a card
  named is a node-level check the cloud authorised (node edit access), so it runs and is logged;
* a node **without a port table** keeps the legacy GPIO path, logged, except onto a GPIO the
  running program drives for another card — that would be a second writer.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from ..blocks.io import PortRef, port_identity

logger = logging.getLogger(__name__)


def _said(v) -> str:
    """What a command said, as it goes back in an ack: the command's own words, kept short."""
    return str(v)[:64]


class LinkGuard:
    def __init__(self, links: Optional[Callable[[], list[PortRef]]] = None):
        self._links = links or (lambda: [])
        self.ports_version = 0

    def use_runtime(self, runtime) -> None:
        self._links = runtime.port_links if runtime is not None else (lambda: [])

    def note_ports_version(self, version) -> None:
        """The port table's version, from the hardware report's answer or a ``syncPorts``."""
        try:
            v = int(version)
        except (TypeError, ValueError):
            return
        self.ports_version = max(self.ports_version, v)

    def runs_port_table(self, links: Optional[list[PortRef]] = None) -> bool:
        links = self._links() if links is None else links
        return self.ports_version > 0 or any(p.line is not None for p in links)

    # ------------------------------------------------------------ setPower

    def check_switch(self, command: dict, gpio: Optional[int] = None) -> Optional[str]:
        """``None`` when a setPower / setPortValue may be carried out, else why not (in words).
        ``gpio`` is the Broadcom number the legacy executor would write (from the node's pins)."""
        links = self._links()
        aid = command.get("assetId") or None
        key = command.get("sourceKey") or "device.power"
        mine = [p for p in links if p.asset_id == aid and p.source_key == key]
        said_key, said_card = _said(key), _said(aid)
        if self.runs_port_table(links):
            if aid is None:
                return "refused: this node runs a port table and the command names no card"
            if not mine:
                return f"refused: {said_key} of card {said_card} is not linked to a port of this node"
            if not any(p.transport.get("direction") == "out" for p in mine):
                return f"refused: {said_key} of card {said_card} is an input on this node"
            return None
        if gpio is None and command.get("gpioNumber") is not None:
            try:
                gpio = int(command["gpioNumber"])
            except (TypeError, ValueError):
                gpio = None
        if gpio is not None:
            for p in links:
                if p.transport.get("direction") == "out" and p.gpio == gpio and (p.asset_id, p.source_key) != (aid, key):
                    return f"refused: GPIO {gpio} is driven by the running program for another card"
        return None

    def check_boot_command(self, command: dict, gpio: Optional[int] = None) -> Optional[str]:
        """Boot reconciliation writes through the legacy executor: the same check, and never a
        port the running program drives — the program decides after a restart (R-21)."""
        refusal = self.check_switch(command, gpio)
        if refusal:
            return refusal
        aid, key = command.get("assetId"), command.get("sourceKey") or "device.power"
        if any(p.asset_id == aid and p.source_key == key and p.transport.get("direction") == "out" for p in self._links()):
            return "skipped: the running program drives this port"
        return None

    # ------------------------------------------------------------ testPort

    def check_port_test(self, command: dict) -> Optional[str]:
        links = self._links()
        if not self.runs_port_table(links):
            return None
        port_id = _said(command.get("portId") or "this port")
        here = port_identity(command.get("transport") or {})
        linked = [p for p in links if port_identity(p.transport) == here]
        aid = command.get("assetId") or None
        if aid is not None:
            if not linked:
                return f"refused: {port_id} is not linked to card {_said(aid)} on this node"
            if all(p.asset_id != aid for p in linked):
                return f"refused: {port_id} is linked to another card on this node"
            return None
        if linked:
            logger.info("testPort %s names no card; the port is linked to %s — a node-level check",
                        port_id, ", ".join(sorted({p.asset_id for p in linked})))
        return None


def refuse(mqtt_client, command: dict, refusal: str) -> None:
    """The failed ack for a refused command, in the words of the refusal."""
    logger.warning("%s %s: %s", command.get("type"), command.get("commandId"), refusal)
    mqtt_client.publish_command_ack(command.get("commandId", ""), "failed", error=refusal)


def handle_switch(command: dict, guard: LinkGuard, runtime, mqtt_client,
                  legacy: Callable[[dict], None], gpio: Optional[int] = None) -> None:
    """setPower / setPortValue on the node. Checked first; then a port the program owns goes
    through the runtime, the single writer (an app toggle becomes an interruption, R-23). On
    a node with a port table that is the only way to an output; on a node without one, a port
    the program does not own goes to the legacy executor, logged."""
    refusal = guard.check_switch(command, gpio=gpio)
    if refusal:
        refuse(mqtt_client, command, refusal)
        return
    command_id = command.get("commandId", "")
    if runtime is not None and runtime.nab is not None and \
            runtime.owns_port(command.get("assetId"), command.get("sourceKey") or "device.power"):
        mqtt_client.publish_command_ack(command_id, "acknowledged")
        result = runtime.apply_command(command)
        if result.get("accepted"):
            mqtt_client.publish_command_ack(command_id, "completed", applied_value=command.get("value"))
        else:
            mqtt_client.publish_command_ack(command_id, "failed", error=result.get("reason"))
        return
    logger.warning("legacy GPIO path: %s for card %s, GPIO %s (this node has no port table)",
                   command.get("type"), command.get("assetId"), command.get("gpioNumber") if gpio is None else gpio)
    legacy(command)


def refuse_port_test(command: dict, guard: LinkGuard, http_reporter, mqtt_client) -> bool:
    """True (and the failed test reported + acked) when a testPort must not run."""
    refusal = guard.check_port_test(command)
    if not refusal:
        return False
    http_reporter.report_port_test(command.get("commandId", ""),
                                   {"portId": command.get("portId"), "result": "failed", "detail": refusal})
    refuse(mqtt_client, command, refusal)
    return True
