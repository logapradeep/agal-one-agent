#!/usr/bin/env python3
"""Render /etc/agal-one-agent/config.yaml from balena environment variables.

Called by entrypoint.sh on every container start. Idempotent — overwrites
the file each boot, since per-device balena variables are the source of truth
for the static fields (node identity, MQTT, board model). Runtime-mutable
fields (`pins`) are preserved from the existing file if present so a daemon
restart doesn't lose pin assignments pushed via MQTT.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import yaml


def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v


def env_int(name: str, default: int) -> int:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    try:
        return int(v)
    except ValueError:
        print(f"[render_config] {name}={v!r} is not an int; using default {default}", file=sys.stderr)
        return default


def env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def main() -> int:
    out_path = Path(sys.argv[1] if len(sys.argv) > 1 else "/etc/agal-one-agent/config.yaml")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Preserve runtime-mutable fields (pins, lora) if the file already exists.
    existing: dict[str, Any] = {}
    if out_path.exists():
        try:
            with out_path.open() as f:
                existing = yaml.safe_load(f) or {}
        except Exception as e:  # noqa: BLE001
            print(f"[render_config] WARN: could not read existing config ({e}); regenerating", file=sys.stderr)

    node_uid = env("NODE_UID")
    auth_token = env("AUTH_TOKEN")
    mqtt_broker = env("MQTT_BROKER")

    config: dict[str, Any] = {
        "node": {
            "uid": node_uid,
            "name": env("NODE_NAME", node_uid or ""),
            "auth_token": auth_token,
            "type": env("NODE_TYPE", "link_rio"),
        },
        "mqtt": {
            "broker": mqtt_broker,
            "port": env_int("MQTT_PORT", 8883),
            "tls": env_bool("MQTT_TLS", True),
            "username": env("MQTT_USERNAME", node_uid or ""),
            "password": env("MQTT_PASSWORD", auth_token or ""),
            "topics": {
                "commands": env("MQTT_COMMANDS_TOPIC", f"agal/{node_uid}/commands"),
                "telemetry": env("MQTT_TELEMETRY_TOPIC", f"agal/{node_uid}/telemetry"),
                "status": env("MQTT_STATUS_TOPIC", f"agal/{node_uid}/status"),
            },
        },
        "telemetry": {
            "interval_seconds": env_int("TELEMETRY_INTERVAL_SECONDS", 10),
            "heartbeat_seconds": env_int("HEARTBEAT_INTERVAL_SECONDS", 30),
        },
        "board": {
            "model": env("BOARD_MODEL", "unknown"),
            "category": env("BOARD_CATEGORY", "raspberrypi"),
        },
        "connectivity": env("CONNECTIVITY", "WiFi"),
    }

    # Optional WiFi section — balena's host-OS handles real WiFi association,
    # but the agent still needs SSID/PSK for the install manual.
    if env("WIFI_SSID"):
        config["wifi"] = {
            "ssid": env("WIFI_SSID"),
            "password": env("WIFI_PASSWORD", ""),
            "country_code": env("WIFI_COUNTRY_CODE", "IN"),
        }

    # Optional cellular section.
    if env("CELLULAR_APN"):
        config["cellular"] = {
            "apn": env("CELLULAR_APN"),
            "pin": env("CELLULAR_PIN", ""),
        }

    # Optional LoRa section (gateway or end-device — defer to env override).
    if env("LORA_ROLE") in {"gateway", "end_device"}:
        lora: dict[str, Any] = {"role": env("LORA_ROLE")}
        if lora["role"] == "gateway":
            lora["gateway"] = {
                "gateway_eui": env("LORA_GATEWAY_EUI", ""),
                "region": env("LORA_REGION", "IN865"),
            }
        else:
            lora["device"] = {
                "dev_eui": env("LORA_DEV_EUI", ""),
                "app_key": env("LORA_APP_KEY", ""),
                "join_eui": env("LORA_JOIN_EUI", "0000000000000000"),
                "join_method": env("LORA_JOIN_METHOD", "OTAA"),
                "parent_gateway_uid": env("LORA_PARENT_GATEWAY_UID", ""),
            }
        config["lora"] = lora

    # Preserve runtime-mutable pin config from the existing file.
    if "pins" in existing:
        config["pins"] = existing["pins"]

    with out_path.open("w") as f:
        yaml.safe_dump(config, f, default_flow_style=False, sort_keys=False)

    print(f"[render_config] wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
