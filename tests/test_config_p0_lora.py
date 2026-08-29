"""Config-parsing tests for the ADR-013 batch block + ADR-011 raw-star LoRa."""

from __future__ import annotations

import textwrap

from agal_one_agent.config import AgentConfig


def _write(tmp_path, body: str) -> str:
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(body))
    return str(p)


_BASE = """
    node:
      uid: node-1
      auth_token: tok
    mqtt:
      broker: b.example
    """


def test_batch_defaults_when_absent(tmp_path):
    cfg = AgentConfig.from_yaml(_write(tmp_path, _BASE))
    b = cfg.telemetry.batch
    assert b.flush_interval_sec == 900
    assert b.max_readings == 500
    assert b.buffer_max_mb == 64
    assert b.retention_hours == 48
    assert cfg.telemetry.live_watch_default is False
    assert cfg.telemetry.live_idle_seconds == 60


def test_batch_block_parsed(tmp_path):
    cfg = AgentConfig.from_yaml(_write(tmp_path, _BASE + """
    telemetry:
      interval_seconds: 10
      live_watch_default: true
      live_idle_seconds: 45
      buffer_db_path: /var/lib/agal-one-agent/telemetry.db
      batch:
        flush_interval_sec: 300
        max_readings: 250
        buffer_max_mb: 32
        retention_hours: 72
    """))
    b = cfg.telemetry.batch
    assert b.flush_interval_sec == 300
    assert b.max_readings == 250
    assert b.buffer_max_mb == 32
    assert b.retention_hours == 72
    assert cfg.telemetry.live_watch_default is True
    assert cfg.telemetry.live_idle_seconds == 45
    assert cfg.telemetry.buffer_db_path.endswith("telemetry.db")
    # Bridges cleanly to the runtime buffer config.
    rc = b.to_batch_config()
    assert rc.buffer_max_bytes == 32 * 1024 * 1024


def test_lora_raw_star_dark_by_default(tmp_path):
    # A raw_star block WITHOUT enabled ⇒ configured but dark.
    cfg = AgentConfig.from_yaml(_write(tmp_path, _BASE + """
    lora:
      mode: raw_star
      region: IN865
      farm_key_id: k1
      radio:
        chipset: sx1276
        freq_mhz: 865.985
        sf: 9
    """))
    assert cfg.lora is not None
    assert cfg.lora.mode == "raw_star"
    assert cfg.lora.enabled is False
    assert cfg.is_lora_raw_star is False      # not enabled ⇒ dark
    assert cfg.lora.farm_key_id == "k1"
    assert cfg.lora.radio.chipset == "sx1276"
    assert cfg.lora.radio.freq_mhz == 865.985
    assert cfg.lora.radio.sf == 9


def test_lora_raw_star_enabled(tmp_path):
    cfg = AgentConfig.from_yaml(_write(tmp_path, _BASE + """
    lora:
      mode: raw_star
      enabled: true
    """))
    assert cfg.is_lora_raw_star is True


def test_legacy_lorawan_gateway_still_parses(tmp_path):
    # The dormant LoRaWAN bridge config path is untouched.
    cfg = AgentConfig.from_yaml(_write(tmp_path, _BASE + """
    lora:
      role: gateway
      gateway:
        gateway_eui: AABBCCDD
    """))
    assert cfg.is_lora_gateway is True
    assert cfg.lora.gateway.gateway_eui == "AABBCCDD"
    assert cfg.is_lora_raw_star is False
