"""Boot reconciliation — fetches desired state from cloud and applies to hardware."""

import logging
import threading

from .config import AgentConfig
from .http_reporter import HttpReporter
from .command_executor import execute
from .mqtt_client import MenvayalMqttClient

logger = logging.getLogger(__name__)


def reconcile(
    config: AgentConfig,
    mqtt_client: MenvayalMqttClient,
    http_reporter: HttpReporter,
) -> None:
    """Fetch desired pin states from backend and apply them.

    Called on daemon startup (after MQTT connects) and on MQTT reconnection.
    Runs in a background thread to avoid blocking the main loop.
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
