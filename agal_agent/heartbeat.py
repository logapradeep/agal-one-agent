"""Periodic heartbeat/status publisher."""

import logging
import threading
import time
from importlib.metadata import version as pkg_version
from typing import Optional

from .config import AgentConfig
from .mqtt_client import MenvayalMqttClient
from .http_reporter import HttpReporter


def _get_agent_version() -> str:
    try:
        return pkg_version("agal-agent")
    except Exception:
        return "0.1.0"

logger = logging.getLogger(__name__)


class HeartbeatPublisher:
    """Publishes online status and uptime at regular intervals."""

    def __init__(self, config: AgentConfig, mqtt_client: MenvayalMqttClient,
                 http_reporter: Optional[HttpReporter] = None):
        self.config = config
        self.mqtt_client = mqtt_client
        self.http_reporter = http_reporter
        self._timer: Optional[threading.Timer] = None
        self._running = False
        self._start_time = time.time()

    def start(self) -> None:
        self._running = True
        self._start_time = time.time()
        self._send_heartbeat()
        logger.info(
            "Heartbeat publisher started (interval=%ds)",
            self.config.telemetry.heartbeat_seconds,
        )

    def stop(self) -> None:
        self._running = False
        if self._timer:
            self._timer.cancel()
            self._timer = None
        # Send offline status via both channels for immediate reflection
        self.mqtt_client.publish_status(online=False, uptime=self.uptime)
        if self.http_reporter:
            self.http_reporter.report_status(online=False, uptime=self.uptime)

    @property
    def uptime(self) -> int:
        return int(time.time() - self._start_time)

    def _send_heartbeat(self) -> None:
        try:
            self.mqtt_client.publish_status(
                online=True,
                uptime=self.uptime,
                firmware_version=_get_agent_version(),
            )
            if self.http_reporter:
                self.http_reporter.report_status(
                    online=True,
                    uptime=self.uptime,
                    firmware_version=_get_agent_version(),
                )
        except Exception as e:
            logger.error("Heartbeat error: %s", e)
        finally:
            if self._running:
                self._timer = threading.Timer(
                    self.config.telemetry.heartbeat_seconds,
                    self._send_heartbeat,
                )
                self._timer.daemon = True
                self._timer.start()
