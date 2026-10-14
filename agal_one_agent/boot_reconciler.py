"""Boot reconciliation — fetches desired state from cloud and applies to hardware."""

import logging
import threading
from typing import Callable, Optional

from .config import AgentConfig
from .http_reporter import HttpReporter
from .command_executor import execute
from .mqtt_client import AgalOneMqttClient

logger = logging.getLogger(__name__)


def reconcile(
    config: AgentConfig,
    mqtt_client: AgalOneMqttClient,
    http_reporter: HttpReporter,
    guard: Optional[Callable[[dict], Optional[str]]] = None,
) -> None:
    """Fetch desired pin states from backend and apply them.

    Called on daemon startup (after MQTT connects) and on MQTT reconnection.
    Runs in a background thread to avoid blocking the main loop. ``guard`` answers why a
    command must not be applied (``ports.links.LinkGuard.check_boot_command``) or None.
    """
    def _run():
        logger.info("Boot reconciliation: fetching desired state from cloud")
        commands = http_reporter.fetch_boot_state(config.node.auth_token)

        if not commands:
            logger.info("Boot reconciliation: no commands to apply")
            return

        applied = 0
        for cmd in commands:
            try:
                # Build a command dict compatible with command_executor.execute()
                synthetic_command = {
                    "commandId": f"boot_{cmd.get('assetId', 'unknown')}",
                    "type": "setPower",
                    "assetId": cmd.get("assetId"),
                    "sourceKey": cmd.get("sourceKey", "device.power"),
                    "pinNumber": cmd.get("pinNumber"),
                    "gpioNumber": cmd.get("gpioNumber"),
                    "pinProtocol": cmd.get("pinProtocol"),
                    "value": cmd.get("value", 0),
                    "timestamp": 0,
                }
                reason = guard(synthetic_command) if guard is not None else None
                if reason:
                    # RBAC-01: the boot state lists every card that names this node; a card is
                    # applied only for a port it is linked to, and never behind the program.
                    logger.warning("Boot reconciliation: %s for asset %s: %s",
                                   "skipped" if reason.startswith("skipped") else "refused", cmd.get("assetId"), reason)
                    continue
                execute(config, mqtt_client, synthetic_command)
                applied += 1
            except Exception as e:
                logger.error(
                    "Boot reconciliation failed for asset %s: %s",
                    cmd.get("assetId"), e,
                )

        logger.info("Boot reconciliation: applied %d/%d commands", applied, len(commands))

    thread = threading.Thread(target=_run, name="boot-reconcile", daemon=True)
    thread.start()
