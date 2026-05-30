# Sensors

Sensor-specific drivers that sit ABOVE the generic bus handlers in `../handlers/`.

| File | What |
|---|---|
| [`ads1115.py`](./ads1115.py) | Low-level ADS1115 16-bit ADC driver (single-shot, configurable PGA). Used by the current sensor. |
| [`current_sensor.py`](./current_sensor.py) | ACS758 Hall-effect current sensor read via ADS1115. RMS over a window. Baseline learning. |
| [`ultrasonic_sensor.py`](./ultrasonic_sensor.py) | JSN-SR04T v2 waterproof ultrasonic distance sensor. Computes water-column depth from a calibrated total depth. |
| [`protection.py`](./protection.py) | Edge protection thread — dry-run pump cutoff + low-water-level warnings. Runs locally, no cloud round-trip required. |

## Bench-test wiring (v1 launch — single-phase pump)

```
                     ┌──────────────────────────────────────────────────────────────┐
                     │                          Raspberry Pi 4B                       │
                     │                                                                │
                     │   GPIO 23 (TRIG) ────────────┐                                  │
                     │   GPIO 24 (ECHO) ───┐        │                                  │
                     │   I2C  (GPIO 2 SDA, │        │                                  │
                     │         GPIO 3 SCL) │        │                                  │
                     │   GPIO 17 (RELAY) ─────────────────────┐                        │
                     └─────────────────────│────────│─────────│────────────────────────┘
                                           │        │         │
                       (2kΩ +1kΩ divider)  │        │         │
                                           │        │         │
                          ┌────────────────┴┐  ┌────┴───┐  ┌──┴──────────────────────┐
                          │ JSN-SR04T ECHO  │  │ TRIG   │  │ Opto-isolated relay HAT  │
                          │ (5 V → 3.3 V)   │  │ direct │  │ (Sequent 8-relay)         │
                          └─────────────────┘  └────────┘  └──┬──────────────────────┘
                                                              │ contactor coil
                                                              │
                                                              ▼
                                                       ┌────────────┐
                                                       │ Contactor  │──┐
                                                       └────────────┘  │ pump current (live phase)
                                                                       │
                                                      ┌────────────────┴─────────────┐
                                                      │ ACS758LCB-050B (Hall sensor) │
                                                      │ VCC=5V, GND=0V, OUT→ADS1115  │
                                                      └─────────────┬────────────────┘
                                                                    │ analog 0..5V
                                                            ┌───────┴───────┐
                                                            │ ADS1115 ADC   │
                                                            │ I2C addr 0x48 │
                                                            │ ch A0         │
                                                            └───────────────┘
                                                                    ▲
                                                                    │ I2C (SDA/SCL)
                                                                    │
                                                              back to RPi
```

## Hardware checklist

- [ ] **Voltage divider on ECHO** — the JSN-SR04T outputs 5 V on ECHO. RPi GPIO is 3.3 V tolerant only. Use a 1 kΩ (series) + 2 kΩ (to ground) divider, OR a 4-channel level shifter.
- [ ] **Opto-isolated relay** between RPi GPIO and the contactor coil. Direct GPIO-to-contactor is a fire risk and EMI source.
- [ ] **Contactor on the live phase, NOT neutral.** Match relay rating to motor inrush (typ. 5–8× rated).
- [ ] **Earth + leakage protection** on the pump side per local electrical code.
- [ ] **MOV + GDT surge protection** on the mains input.
- [ ] **PVC stilling tube** below the ultrasonic sensor in open wells to suppress wave reflections.

## YAML pin config example

```yaml
pins:
  # ── Pump 1: relay + current sensor ─────────────────────────────────────
  - physical_pin: 11
    gpio_number: 17
    protocol: gpio_output
    label: pump1.relay
    assigned_to: asset_pump1

  - physical_pin: 3
    gpio_number: 2
    protocol: i2c_sda
    bus_id: I2C1

  - physical_pin: 5
    gpio_number: 3
    protocol: i2c_scl
    bus_id: I2C1

  - physical_pin: 12
    protocol: analog_input
    label: pump1.current
    sensor_type: current_acs758
    sensor_params:
      ads1115_address: 0x48
      ads1115_channel: 0
      ads1115_bus: 1
      sensitivity_mv_per_a: 40.0   # ±50 A variant
      vcc_v: 5.0
      window_ms: 100
    protection:
      cut_pin_label: pump1.relay
      threshold_pct: 20.0
      confirm_window_s: 5.0
      inrush_ignore_s: 2.0
      baseline_learn_after_s: 30.0
      baseline_window_s: 10.0
      auto_restart_after_s: 0      # 0 = manual restart only

  # ── Open well water-level sensor ──────────────────────────────────────
  - physical_pin: 16
    protocol: ultrasonic
    label: well1.level
    sensor_type: ultrasonic_jsn_sr04t
    sensor_params:
      trig_gpio: 23
      echo_gpio: 24                # ECHO must come through a 2:1 voltage divider
      total_depth_m: 4.0
      median_of: 3
      low_level_threshold_pct: 15
      low_level_cooldown_s: 3600
```

## Bench test procedure (target: 2026-05-25)

1. **Power test pump on bench with the contactor + relay only** (no current sensor, no daemon). Verify the contactor closes/opens reliably under GPIO HIGH/LOW from the RPi. Use the existing `gpio_handler` via the existing `setAssetPower` flow.
2. **Add the ACS758 + ADS1115** in series with the live wire. Power the pump manually (bypass the relay), watch the I2C reading via `python -c "from agal_agent.sensors.ads1115 import Ads1115; import time; a = Ads1115(); [print(a.read_v(0)) for _ in range(50)] "`. Confirm voltage centres on ~2.5 V when off, deviates when on.
3. **Wire the daemon** with `sensor_type: current_acs758` and `protection:` block. Restart, watch logs: should see `baseline learned: X.XX A` after ~10 s of stable running.
4. **Simulate dry-run** by disconnecting the suction line while the pump runs. Watch logs: should see `DRY-RUN CUTOFF` within 5 s of the current dropping.
5. **Add the JSN-SR04T** with the voltage divider. Test in a bucket of water — confirm `well1.level.depth` matches a tape measurement to within 5 cm.
6. **Trigger a low-water event** by lowering the water below `low_level_threshold_pct`. Watch cloud event timeline for `low_water_level_warning`.

## Dev-machine behaviour (macOS, no Pi)

All hardware imports are guarded. On a non-Pi machine:

- `Ads1115.read_v()` returns `0.0`
- `CurrentSensorACS758.read_rms()` returns a `CurrentSensorReading` with `quality="uncertain"` and the configured baseline (or 0 A)
- `UltrasonicSensorJsnSr04t.read()` returns a reading equal to half the configured total depth

This is enough for the `ProtectionMonitor` to exercise its decision logic end-to-end against unit-test fixtures without any I2C/GPIO hardware. See `../tests/test_protection_no_hw.py` for the bench fixture suite.

## Adding a new sensor

1. Create `sensors/<my_sensor>.py` exposing a class with `.read()` returning a reading object and `.to_telemetry(reading)` returning the canonical wire shape.
2. Guard all hardware imports with try/except → `_HW_AVAILABLE` flag.
3. Register the new sensor type in `command_executor.py` (`_get_sensor_driver`) and in `telemetry_publisher.py`.
4. Add a YAML config example to this README.
5. Add a bench test step under "Bench test procedure" above.
