"""PWM support tests — SURFACE_PARITY S-118 / SYNC.md item 4:
"Daemon: honour `pwm` in `syncPinConfig` + percentage writes via `setPortValue`."

Covers:
- PinConfig round-trip of the pwm_* fields through update_pins() + from_yaml()
  (the snake_cased mirror of the contracted PortTransport `pwm` variant —
  asset.schema.json v3.1 frequencyHz / dutyCyclePct / polarity).
- PwmHandler frequency / duty / polarity semantics with a mocked RPi.GPIO
  (no hardware required, same approach as test_protection_no_hw.py).
- command_executor setPower semantics on a pwm pin (on = configured duty, not 1%).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import yaml

from agal_one_agent.config import AgentConfig, PinConfig
from agal_one_agent.handlers import pwm_handler
from agal_one_agent import command_executor


# ----- config round-trip ----------------------------------------------------


MINIMAL_CONFIG = {
    "node": {"uid": "node-test", "name": "Bench", "auth_token": "tok"},
    "mqtt": {"broker": "broker.example"},
}

PWM_PIN_PAYLOAD = {
    "physical_pin": 12,
    "gpio_number": 18,
    "protocol": "pwm",
    "label": "dimmer1",
    "pwm_frequency_hz": 50,
    "pwm_duty_cycle_pct": 7.5,
    "pwm_polarity": "inverted",
}


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump(MINIMAL_CONFIG))
    return str(path)


def test_update_pins_carries_pwm_fields(config_file):
    config = AgentConfig.from_yaml(config_file)
    config.update_pins([PWM_PIN_PAYLOAD], config_file)

    assert len(config.pins) == 1
    pin = config.pins[0]
    assert pin.protocol == "pwm"
    assert pin.pwm_frequency_hz == 50
    assert pin.pwm_duty_cycle_pct == 7.5
    assert pin.pwm_polarity == "inverted"


def test_pwm_fields_persist_to_yaml_and_reload(config_file):
    config = AgentConfig.from_yaml(config_file)
    config.update_pins([PWM_PIN_PAYLOAD], config_file)

    reloaded = AgentConfig.from_yaml(config_file)
    assert len(reloaded.pins) == 1
    pin = reloaded.pins[0]
    assert pin.pwm_frequency_hz == 50
    assert pin.pwm_duty_cycle_pct == 7.5
    assert pin.pwm_polarity == "inverted"


def test_non_pwm_pin_omits_pwm_keys_in_yaml(config_file):
    config = AgentConfig.from_yaml(config_file)
    config.update_pins(
        [{"physical_pin": 11, "gpio_number": 17, "protocol": "gpio_output", "label": "relay1"}],
        config_file,
    )
    data = yaml.safe_load(open(config_file))
    assert "pwm_frequency_hz" not in data["pins"][0]
    assert "pwm_duty_cycle_pct" not in data["pins"][0]
    assert "pwm_polarity" not in data["pins"][0]


# ----- PwmHandler (mocked RPi.GPIO) -----------------------------------------


@pytest.fixture
def gpio_mock(monkeypatch):
    """Pretend RPi.GPIO is available and capture PWM channel calls."""
    mock = MagicMock()
    monkeypatch.setattr(pwm_handler, "GPIO", mock, raising=False)
    monkeypatch.setattr(pwm_handler, "_GPIO_AVAILABLE", True)
    monkeypatch.setattr(pwm_handler, "_pwm_instances", {})
    return mock


def _pwm_pin(**overrides) -> PinConfig:
    base = dict(
        physical_pin=12,
        gpio_number=18,
        protocol="pwm",
        label="dimmer1",
        pwm_frequency_hz=2000.0,
        pwm_duty_cycle_pct=40.0,
        pwm_polarity="normal",
    )
    base.update(overrides)
    return PinConfig(**base)


def test_first_write_creates_channel_at_configured_frequency(gpio_mock):
    handler = pwm_handler.PwmHandler()
    applied = handler.write(_pwm_pin(), 25)

    gpio_mock.setup.assert_called_once_with(18, gpio_mock.OUT)
    gpio_mock.PWM.assert_called_once_with(18, 2000.0)
    gpio_mock.PWM.return_value.start.assert_called_once_with(25.0)
    assert applied == 25.0


def test_default_frequency_when_unconfigured(gpio_mock):
    handler = pwm_handler.PwmHandler()
    handler.write(_pwm_pin(pwm_frequency_hz=None), 10)
    gpio_mock.PWM.assert_called_once_with(18, pwm_handler.DEFAULT_FREQUENCY_HZ)


def test_second_write_changes_duty_not_recreate(gpio_mock):
    handler = pwm_handler.PwmHandler()
    handler.write(_pwm_pin(), 25)
    handler.write(_pwm_pin(), 75)

    channel = gpio_mock.PWM.return_value
    assert gpio_mock.PWM.call_count == 1
    channel.ChangeDutyCycle.assert_called_once_with(75.0)
    channel.ChangeFrequency.assert_not_called()


def test_frequency_change_applied_on_existing_channel(gpio_mock):
    handler = pwm_handler.PwmHandler()
    handler.write(_pwm_pin(pwm_frequency_hz=1000.0), 25)
    handler.write(_pwm_pin(pwm_frequency_hz=50.0), 25)

    channel = gpio_mock.PWM.return_value
    channel.ChangeFrequency.assert_called_once_with(50.0)


def test_inverted_polarity_flips_hardware_duty_only(gpio_mock):
    handler = pwm_handler.PwmHandler()
    applied = handler.write(_pwm_pin(pwm_polarity="inverted"), 30)

    gpio_mock.PWM.return_value.start.assert_called_once_with(70.0)
    assert applied == 30.0  # acked value stays the logical percentage


def test_duty_is_clamped_to_0_100(gpio_mock):
    handler = pwm_handler.PwmHandler()
    assert handler.write(_pwm_pin(), 250) == 100.0
    assert handler.write(_pwm_pin(), -5) == 0.0


def test_release_stops_channel_and_clears_registry(gpio_mock):
    handler = pwm_handler.PwmHandler()
    handler.write(_pwm_pin(), 25)
    assert pwm_handler.active_gpios() == {18}

    pwm_handler.release(18)
    gpio_mock.PWM.return_value.stop.assert_called_once()
    assert pwm_handler.active_gpios() == set()
    # Releasing again is a no-op
    pwm_handler.release(18)


def test_write_without_gpio_number_raises(gpio_mock):
    handler = pwm_handler.PwmHandler()
    with pytest.raises(ValueError):
        handler.write(_pwm_pin(gpio_number=None), 25)


# ----- command_executor setPower / setPortValue on a pwm pin ----------------


@pytest.fixture
def executor_env(monkeypatch, config_file):
    """Config with one pwm pin + a fake handler wired into the executor."""
    config = AgentConfig.from_yaml(config_file)
    config.update_pins([PWM_PIN_PAYLOAD], config_file)

    fake_handler = MagicMock()
    fake_handler.write.side_effect = lambda pin, value: float(value)
    monkeypatch.setitem(command_executor._handlers, "pwm", fake_handler)

    mqtt = MagicMock()
    return config, mqtt, fake_handler


def test_set_power_on_pwm_pin_uses_configured_duty(executor_env):
    config, mqtt, handler = executor_env
    command_executor.execute(config, mqtt, {
        "commandId": "cmd-1", "type": "setPower", "gpioNumber": 18, "value": True,
    })
    handler.write.assert_called_once()
    assert handler.write.call_args.args[1] == 7.5  # PWM_PIN_PAYLOAD duty
    mqtt.publish_command_ack.assert_called_with("cmd-1", "completed", applied_value=7.5)


def test_set_power_off_pwm_pin_writes_zero(executor_env):
    config, mqtt, handler = executor_env
    command_executor.execute(config, mqtt, {
        "commandId": "cmd-2", "type": "setPower", "gpioNumber": 18, "value": False,
    })
    assert handler.write.call_args.args[1] == 0.0


def test_set_power_on_pwm_defaults_to_full_duty_when_unconfigured(monkeypatch, config_file):
    config = AgentConfig.from_yaml(config_file)
    payload = dict(PWM_PIN_PAYLOAD)
    del payload["pwm_duty_cycle_pct"]
    config.update_pins([payload], config_file)

    fake_handler = MagicMock()
    fake_handler.write.side_effect = lambda pin, value: float(value)
    monkeypatch.setitem(command_executor._handlers, "pwm", fake_handler)

    command_executor.execute(config, MagicMock(), {
        "commandId": "cmd-3", "type": "setPower", "gpioNumber": 18, "value": True,
    })
    assert fake_handler.write.call_args.args[1] == 100.0


def test_set_port_value_passes_percentage_through(executor_env):
    config, mqtt, handler = executor_env
    command_executor.execute(config, mqtt, {
        "commandId": "cmd-4", "type": "setPortValue", "gpioNumber": 18, "value": 62.5,
    })
    assert handler.write.call_args.args[1] == 62.5
    mqtt.publish_command_ack.assert_called_with("cmd-4", "completed", applied_value=62.5)
