"""Sentry error capture for the daemon agent.

Mirror of the Cloud Functions Sentry wiring in
`backend/functions/src/shared/sentry.ts` — same DSN policy, same environment
auto-detection, same defensive-init pattern.

Setup:
    1. Sentry project (Python platform).
    2. DSN exported as env var on each RPi:

           # In /etc/systemd/system/agal-one-agent.service (or balena env vars):
           Environment=SENTRY_DSN=https://abc@o0.ingest.sentry.io/0
           Environment=SENTRY_ENV=prod

       Or for dev:
           SENTRY_DSN=... python -m agal_one_agent.main

    3. Without SENTRY_DSN, init silently no-ops — no remote capture, no
       crash. Useful for dev / on-bench testing.

Auto-capture via LoggingIntegration: any `logger.error(...)` or
`logger.critical(...)` becomes a Sentry event. The protection module already
uses `logger.error("[protection:...] ...")` for dry-run cutoffs etc., so the
existing logs become structured Sentry events for free.

For unexpected exceptions (uncaught in MQTT command handlers, telemetry
publishers, etc.), the SDK's default exception integrations capture them
automatically when they bubble to a thread boundary.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# Module-level state so repeated init calls are idempotent.
_initialized: bool = False
_provider_available: Optional[bool] = None  # cached probe result


def is_sentry_available() -> bool:
    """True iff sentry-sdk is importable in this environment."""
    global _provider_available
    if _provider_available is not None:
        return _provider_available
    try:
        import sentry_sdk  # noqa: F401
        _provider_available = True
    except ImportError:
        _provider_available = False
    return _provider_available


def init_sentry(node_uid: Optional[str] = None, agent_version: str = "0.1.5") -> bool:
    """Initialize Sentry if SENTRY_DSN is set. Safe to call multiple times.

    Returns:
        True if Sentry was newly initialized this call (or already initialized);
        False if no DSN or sentry-sdk isn't installed.
    """
    global _initialized

    if _initialized:
        return True

    dsn = os.environ.get("SENTRY_DSN")
    if not dsn:
        return False

    if not is_sentry_available():
        logger.warning(
            "SENTRY_DSN is set but sentry-sdk is not installed. "
            "Run `pip install sentry-sdk>=2.0` to enable remote error capture."
        )
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.logging import LoggingIntegration
        from sentry_sdk.integrations.threading import ThreadingIntegration

        environment = os.environ.get("SENTRY_ENV") or (
            "dev" if os.environ.get("AGAL_ONE_AGENT_DEV") == "1" else "prod"
        )

        sentry_sdk.init(
            dsn=dsn,
            environment=environment,
            release=f"agal-one-agent@{agent_version}",
            # No perf tracing yet — keeps event volume bounded.
            traces_sample_rate=0.0,
            sample_rate=1.0,
            # Don't auto-attach request bodies, env vars, etc.
            send_default_pii=False,
            # Capture: WARNING+ as breadcrumbs, ERROR+ as events. Matches our
            # existing logging conventions (logger.error for genuine problems,
            # logger.warning for resilient-recoverable issues).
            integrations=[
                LoggingIntegration(level=logging.WARNING, event_level=logging.ERROR),
                ThreadingIntegration(propagate_hub=True),
            ],
        )

        if node_uid:
            sentry_sdk.set_tag("node_uid", node_uid)
            sentry_sdk.set_user({"id": node_uid})

        _initialized = True
        logger.info(
            "Sentry initialized (env=%s, release=agal-one-agent@%s)",
            environment, agent_version,
        )
        return True

    except Exception as e:  # noqa: BLE001 — Sentry init failures must not crash the daemon
        logger.error("Failed to initialize Sentry: %s", e)
        return False


def capture_exception(err: BaseException, **context) -> None:
    """Manually capture an exception with optional context tags.

    Used in spots where logger.error doesn't carry the exception object,
    or where we want extra structured context attached (e.g., the
    protection module's dry-run cutoff event includes baseline/observed
    values that aren't in the log message).

    No-op if Sentry is not initialized.
    """
    if not _initialized:
        return

    try:
        import sentry_sdk

        with sentry_sdk.push_scope() as scope:
            for key, value in context.items():
                scope.set_tag(key, str(value)) if isinstance(value, (str, int, float, bool)) else scope.set_extra(key, value)
            sentry_sdk.capture_exception(err)
    except Exception as sentry_err:  # noqa: BLE001
        logger.error("Sentry capture failed (non-fatal): %s", sentry_err)


def send_test_event() -> bool:
    """Send a synthetic event to confirm wiring. Returns True if sent."""
    if not _initialized:
        logger.warning("send_test_event: Sentry not initialized")
        return False
    try:
        import sentry_sdk
        sentry_sdk.capture_message(
            "[smoke-test] Agal One daemon Sentry wired up",
            level="info",
        )
        return True
    except Exception as e:  # noqa: BLE001
        logger.error("send_test_event failed: %s", e)
        return False
