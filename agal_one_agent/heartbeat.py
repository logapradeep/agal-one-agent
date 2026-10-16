"""Periodic heartbeat/status publisher."""

import logging
import threading
import time
from typing import Optional

from . import __version__
from .config import AgentConfig
from .mqtt_client import AgalOneMqttClient
from .http_reporter import HttpReporter

logger = logging.getLogger(__name__)


class HeartbeatPublisher:
    """Publishes online status and uptime at regular intervals.

    Every status it sends — the online heartbeats and the offline one at a clean stop, over HTTPS
    and over MQTT — carries ``agent_version``: the version of the code that is RUNNING
    (``agal_one_agent.__version__``, resolved once, when the package was imported at start; contracts
    v1.14.2 join safety, nodes/README.md §10.3). Never re-read from the installed package's
    metadata, which names the new version as soon as pip has replaced the files — during a join, or
    while an over-the-air update waits for a pump to stop — while this process still runs the old
    one; and never a placeholder: agents up to 0.3.3 sent "0.1.0" in their clean-stop status (the
    reporters' default) and whenever the metadata could not be read, and the app showed the node on
    agent 0.1.0 during every restart (H3)."""

    def __init__(self, config: AgentConfig, mqtt_client: AgalOneMqttClient,
                 http_reporter: Optional[HttpReporter] = None,
                 extra_provider=None, agent_version: Optional[str] = None):
        self.config = config
        self.mqtt_client = mqtt_client
        self.http_reporter = http_reporter
        # v0.2.0: callable returning the automation-block status the heartbeat
        # carries (programVersion, programStatus, capabilities) — ADR-017.
        self.extra_provider = extra_provider
        self.agent_version = agent_version or __version__
        self._timer: Optional[threading.Timer] = None
        self._running = False
        self._start_time = time.time()

    def _extra(self) -> Optional[dict]:
        if self.extra_provider is None:
            return None
        try:
            return self.extra_provider() or None
        except Exception as e:  # noqa: BLE001
            logger.debug("heartbeat extra failed: %s", e)
            return None

    def start(self) -> None:
        self._running = True
        self._start_time = time.time()
        self._send_heartbeat()
        logger.info(
            "Heartbeat publisher started (interval=%ds)",
            self.config.telemetry.heartbeat_seconds,
        )

    def pause(self) -> None:
        """Go silent (a simulated outage / offline period): no offline status is
        published — the cloud's heartbeat sweep must notice on its own (R-28)."""
        self._running = False
        if self._timer:
            self._timer.cancel()
            self._timer = None

    def resume(self) -> None:
        if self._running:
            return
        self._running = True
        self._send_heartbeat()

    def stop(self) -> None:
        self._running = False
        if self._timer:
            self._timer.cancel()
            self._timer = None
        # Send offline status via both channels for immediate reflection — with this agent's own
        # version (the backend stores no version from an offline status, contracts v1.14.2; the
        # agent never sends a placeholder either). Each channel on its own: a failing MQTT publish
        # must not keep the HTTPS one — the one the backend reads — from going out.
        try:
            self.mqtt_client.publish_status(online=False, uptime=self.uptime,
                                            firmware_version=self.agent_version)
        except Exception as e:  # noqa: BLE001
            logger.warning("offline status not published on MQTT: %s", e)
        if self.http_reporter:
            try:
                self.http_reporter.report_status(online=False, uptime=self.uptime,
                                                 firmware_version=self.agent_version)
            except Exception as e:  # noqa: BLE001
                logger.warning("offline status not posted: %s", e)

    @property
    def uptime(self) -> int:
        return int(time.time() - self._start_time)

    def _send_heartbeat(self) -> None:
        try:
            extra = self._extra()
            try:
                self.mqtt_client.publish_status(
                    online=True,
                    uptime=self.uptime,
                    firmware_version=self.agent_version,
                    extra=extra,
                )
            except Exception as e:  # noqa: BLE001 — the HTTPS heartbeat (the one the backend reads) still goes
                logger.error("Heartbeat error (MQTT): %s", e)
            if self.http_reporter:
                self.http_reporter.report_status(
                    online=True,
                    uptime=self.uptime,
                    firmware_version=self.agent_version,
                    extra=extra,
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
